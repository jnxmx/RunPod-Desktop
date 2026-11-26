"""Async RunPod REST v2, GraphQL, and pod health-check client."""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any, Optional

import aiohttp
from aiohttp import web

RUNPOD_REST_V2_BASE = "https://api.runpod.io/v2"
RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"
POD_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "content-encoding",
    }
)


def is_valid_pod_id(pod_id: str) -> bool:
    """Validate RunPod pod identifier before constructing upstream URLs."""
    return bool(pod_id and POD_ID_RE.fullmatch(pod_id))


def _to_pos_float(value: Any) -> float:
    """Convert numeric input to a positive finite float, or 0.0 otherwise."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return num if math.isfinite(num) and num > 0 else 0.0


def compute_pod_hourly_cost(pod: dict[str, Any]) -> float:
    """Compute a single pod instance's hourly USD rate (compute + container/persistent disk)."""
    if not isinstance(pod, dict):
        return 0.0
    raw_cost = pod.get("cost") if pod.get("cost") is not None else pod.get("costPerHr")
    compute = _to_pos_float(raw_cost)
    if compute <= 0.0:
        return 0.0

    raw_container = pod.get("disk")
    if raw_container is None:
        raw_container = pod.get("containerDiskInGb")
    if raw_container is None:
        raw_container = pod.get("containerDiskGb")

    mounts = pod.get("mounts")
    persistent = mounts.get("persistent") if isinstance(mounts, dict) else None
    raw_volume = persistent.get("size") if isinstance(persistent, dict) else None
    if raw_volume is None:
        raw_volume = pod.get("volumeInGb")
    if raw_volume is None:
        raw_volume = pod.get("volumeDiskGb")

    total_gb = _to_pos_float(raw_container) + _to_pos_float(raw_volume)
    # RunPod bills container + persistent volume storage at $0.10/GB/month (~730 hours/month)
    disk_hourly = (total_gb * 0.10) / 730.0
    return compute + disk_hourly


class RunPodClient:
    """Shared async HTTP client for RunPod API calls and SSE log streaming."""

    def __init__(
        self,
        rest_base: str = RUNPOD_REST_V2_BASE,
        graphql_url: str = RUNPOD_GRAPHQL_URL,
    ) -> None:
        self.rest_base = rest_base.rstrip("/")
        self.graphql_url = graphql_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        """Lazily create a shared aiohttp ClientSession on the active event loop."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=50, ssl=True)
            timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15)
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close the shared ClientSession cleanly on shutdown."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch_balance(self, api_key: str) -> tuple[int, dict]:
        """Query RunPod GraphQL API for the user's balance and current hourly spend."""
        if not api_key:
            return 401, {"error": "API-ключ RunPod не настроен."}

        query = """
        query MyselfBalance {
          myself {
            email
            clientBalance
            currentSpendPerHr
            spendLimit
          }
        }
        """
        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with session.post(
                self.graphql_url,
                json={"query": query},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                text = await resp.text()
                try:
                    payload = await resp.json(content_type=None)
                except Exception:
                    payload = None

                if resp.status in (401, 403):
                    return resp.status, {
                        "error": f"Нет доступа к GraphQL API (HTTP {resp.status}). Проверьте разрешение GraphQL Read.",
                    }
                if not resp.ok:
                    detail = (payload or {}).get("error") if isinstance(payload, dict) else text[:300]
                    return resp.status, {"error": f"Ошибка GraphQL ({resp.status}): {detail}"}
                if not isinstance(payload, dict):
                    return 502, {"error": "Некорректный ответ RunPod GraphQL API."}

                errors = payload.get("errors")
                if isinstance(errors, list) and errors:
                    msg = errors[0].get("message", "GraphQL error") if isinstance(errors[0], dict) else str(errors[0])
                    status = 403 if "unauthorized" in msg.lower() or "forbidden" in msg.lower() else 502
                    return status, {"error": msg}

                myself = (payload.get("data") or {}).get("myself")
                if not isinstance(myself, dict):
                    return 502, {"error": "RunPod GraphQL не вернул данные аккаунта."}

                return 200, {
                    "email": myself.get("email"),
                    "clientBalance": myself.get("clientBalance"),
                    "currentSpendPerHr": myself.get("currentSpendPerHr"),
                    "spendLimit": myself.get("spendLimit"),
                }
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return 502, {"error": f"Ошибка сети при запросе баланса: {exc}"}

    async def fetch_api_key_metadata(self, api_key: str) -> Optional[dict]:
        """Fetch matching API key metadata (name, createdAt, expiresAt, permissions, scopes) via GraphQL."""
        if not api_key:
            return None

        query = """
        query MyselfApiKeys {
          myself {
            apiKeys {
              id
              name
              createdAt
              expiresAt
              permissions
              scopes
            }
          }
        }
        """
        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with session.post(
                self.graphql_url,
                json={"query": query},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if not resp.ok:
                    return None
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

        if not isinstance(payload, dict):
            return None
        api_keys = ((payload.get("data") or {}).get("myself") or {}).get("apiKeys")
        if not isinstance(api_keys, list):
            return None

        matched = None
        for entry in api_keys:
            if not isinstance(entry, dict):
                continue
            key_id = str(entry.get("id") or "").strip()
            if key_id and api_key.startswith(key_id):
                matched = entry
                break
        if matched is None:
            return None

        return {
            "id": matched.get("id"),
            "name": matched.get("name"),
            "createdAt": matched.get("createdAt"),
            "expiresAt": matched.get("expiresAt"),
            "permissions": matched.get("permissions"),
            "scopes": matched.get("scopes") if isinstance(matched.get("scopes"), list) else None,
        }

    async def _probe_rest_get(self, api_key: str, subpath: str) -> tuple[int, object]:
        """Issue a lightweight GET against REST v2 to verify token validity and scope access."""
        session = await self.get_session()
        target_url = f"{self.rest_base}/{subpath.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        try:
            async with session.get(
                target_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                return resp.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return 502, None

    async def probe_api_key(self, api_key: str, template_id: str = "casls01eko") -> tuple[int, dict]:
        """Verify an unpersisted RunPod API key's balance, email, permissions, and expiration."""
        cleaned_key = (api_key or "").strip()
        if not cleaned_key:
            return 400, {"ok": False, "authValid": False, "error": "Укажите API-ключ RunPod."}

        clean_tpl = (template_id or "casls01eko").strip() or "casls01eko"
        (
            (gql_status, gql_data),
            key_meta,
            (pods_status, _pods_data),
            (cat_status, _cat_data),
            (tpl_status, _tpl_data),
            (sec_status, sec_data),
            (acc_status, acc_data),
        ) = await asyncio.gather(
            self.fetch_balance(cleaned_key),
            self.fetch_api_key_metadata(cleaned_key),
            self._probe_rest_get(cleaned_key, "pods"),
            self._probe_rest_get(cleaned_key, "catalog/gpus"),
            self._probe_rest_get(cleaned_key, f"templates/{clean_tpl}"),
            self._probe_rest_get(cleaned_key, "account/secrets"),
            self._probe_rest_get(cleaned_key, "account"),
        )

        statuses = (gql_status, pods_status, cat_status, tpl_status, sec_status, acc_status)
        auth_valid = any(st == 200 for st in statuses)
        all_network_err = all(st == 502 for st in statuses)

        error_msg = None
        if not auth_valid:
            if all_network_err:
                error_msg = "Нет ответа от api.runpod.io — проверьте подключение к интернету."
            elif 401 in statuses or (isinstance(gql_data, dict) and "401" in str(gql_data.get("error") or "")):
                error_msg = "Неверный или отозванный API-ключ (HTTP 401)."
            else:
                error_msg = "Ключ отклонён сервером RunPod или не имеет ни одного разрешённого метода."

        email = None
        client_balance = None
        spend_per_hr = None
        if gql_status == 200 and isinstance(gql_data, dict):
            email = gql_data.get("email")
            client_balance = gql_data.get("clientBalance")
            spend_per_hr = gql_data.get("currentSpendPerHr")
        if not email and acc_status == 200 and isinstance(acc_data, dict):
            email = acc_data.get("email")

        missing_rest: list[str] = []
        if pods_status != 200:
            missing_rest.append("Pods (Read)")
        if cat_status != 200:
            missing_rest.append("Catalog (Read)")
        if tpl_status != 200 and tpl_status != 404:
            missing_rest.append("Templates (Read)")

        scopes = key_meta.get("scopes") if isinstance(key_meta, dict) else None
        if isinstance(scopes, list):
            if "pods:write" not in scopes:
                missing_rest.append("Pods (Write)")
            if "secrets:read" not in scopes and sec_status != 200:
                missing_rest.append("Secrets (Read)")

        rest_ok = auth_valid and len(missing_rest) == 0
        graphql_ok = gql_status == 200
        graphql_error = None
        if not graphql_ok and isinstance(gql_data, dict):
            graphql_error = gql_data.get("error")

        secrets_ok = sec_status == 200
        raw_secrets = []
        if isinstance(sec_data, dict) and isinstance(sec_data.get("secrets"), list):
            raw_secrets = sec_data["secrets"]
        elif isinstance(sec_data, list):
            raw_secrets = sec_data

        found_names = {
            str(item.get("name") if isinstance(item, dict) else item).strip().upper()
            for item in raw_secrets
            if item
        }
        configured_secrets = [
            canonical
            for canonical in ("HF_TOKEN", "CIVITAI_API_KEY", "COMFYUI_BACKUP", "API_KEY_COMFY_ORG")
            if canonical in found_names
        ]

        return 200, {
            "ok": bool(auth_valid and rest_ok and graphql_ok),
            "authValid": auth_valid,
            "networkError": all_network_err,
            "error": error_msg,
            "email": email,
            "clientBalance": client_balance,
            "currentSpendPerHr": spend_per_hr,
            "keyInfo": key_meta,
            "checks": {
                "rest": {
                    "ok": rest_ok,
                    "missing": missing_rest,
                    "writeConfirmed": isinstance(scopes, list) and "pods:write" in scopes,
                    "allResources": isinstance(scopes, list) and "pods:write" in scopes and "catalog:read" in scopes and "templates:read" in scopes,
                },
                "graphql": {
                    "ok": graphql_ok,
                    "status": gql_status,
                    "permissions": key_meta.get("permissions") if isinstance(key_meta, dict) else None,
                    "error": graphql_error,
                },
                "secrets": {
                    "ok": secrets_ok,
                    "status": sec_status,
                    "configured": configured_secrets,
                },
            },
        }

    async def proxy_rest_v2(
        self,
        request: web.Request,
        subpath: str,
        api_key: str,
    ) -> web.StreamResponse:
        """Forward a REST v2 request (including SSE logs) to api.runpod.io/v2."""
        if not api_key:
            return web.json_response({"error": "Не указан RunPod API Secret Key."}, status=401)

        clean_subpath = subpath.lstrip("/")
        target_url = f"{self.rest_base}/{clean_subpath}"
        if request.query_string:
            target_url = f"{target_url}?{request.query_string}"

        headers: dict[str, str] = {}
        for k, v in request.headers.items():
            if k.lower() in HOP_BY_HOP_HEADERS or k.lower() == "authorization":
                continue
            headers[k] = v
        headers["Authorization"] = f"Bearer {api_key}"

        body_bytes: Optional[bytes] = None
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            body_bytes = await request.read()

        is_sse = clean_subpath.endswith("/logs") or "text/event-stream" in request.headers.get("Accept", "")
        timeout = (
            aiohttp.ClientTimeout(total=None, connect=15, sock_read=None)
            if is_sse
            else aiohttp.ClientTimeout(total=30, connect=15)
        )

        session = await self.get_session()
        try:
            async with session.request(
                request.method,
                target_url,
                headers=headers,
                data=body_bytes,
                timeout=timeout,
            ) as upstream_resp:
                resp_headers: dict[str, str] = {}
                for k, v in upstream_resp.headers.items():
                    if k.lower() in HOP_BY_HOP_HEADERS:
                        continue
                    resp_headers[k] = v
                resp_headers["Cache-Control"] = "no-store"

                if is_sse or "text/event-stream" in upstream_resp.headers.get("Content-Type", "").lower():
                    stream_resp = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
                    await stream_resp.prepare(request)
                    try:
                        async for chunk in upstream_resp.content.iter_any():
                            if not chunk:
                                continue
                            await stream_resp.write(chunk)
                    except (ConnectionResetError, asyncio.CancelledError):
                        pass
                    return stream_resp

                raw_body = await upstream_resp.read()
                return web.Response(
                    status=upstream_resp.status,
                    headers=resp_headers,
                    body=raw_body,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.json_response({"error": f"Ошибка сети RunPod API: {exc}"}, status=502)

    async def check_pod_health(self, pod_id: str) -> tuple[int, dict]:
        """Check whether the ComfyUI server on port 7860 of a pod is responding."""
        if not is_valid_pod_id(pod_id):
            return 400, {"ok": False, "error": "Invalid pod ID"}

        url = f"https://{pod_id}-7860.proxy.runpod.net/system_stats"
        session = await self.get_session()
        try:
            async with session.get(
                url,
                headers={"Cache-Control": "no-store"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if not resp.ok:
                    return resp.status, {"ok": False, "status": resp.status}
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {}
                return 200, {"ok": True, "stats": data}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return 503, {"ok": False}

    async def terminate_pod(self, pod_id: str, api_key: str) -> tuple[int, dict]:
        """Terminate a RunPod pod via REST v2 DELETE /pods/{id}."""
        if not api_key:
            return 401, {"ok": False, "error": "API-ключ RunPod не настроен."}
        if not is_valid_pod_id(pod_id):
            return 400, {"ok": False, "error": "Некорректный ID пода."}

        url = f"{self.rest_base}/pods/{pod_id}"
        session = await self.get_session()
        try:
            async with session.delete(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 204 or resp.ok:
                    return 200, {"ok": True}
                detail = await resp.text()
                return resp.status, {"ok": False, "error": detail[:300]}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return 502, {"ok": False, "error": str(exc)}

    async def restart_pod(self, pod_id: str, api_key: str) -> tuple[int, dict]:
        """Restart a RunPod pod via REST v2 POST /pods/{id}/action."""
        if not api_key:
            return 401, {"ok": False, "error": "API-ключ RunPod не настроен."}
        if not is_valid_pod_id(pod_id):
            return 400, {"ok": False, "error": "Некорректный ID пода."}

        url = f"{self.rest_base}/pods/{pod_id}/action"
        session = await self.get_session()
        try:
            async with session.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"action": "restart"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.ok:
                    return 200, {"ok": True}
                detail = await resp.text()
                return resp.status, {"ok": False, "error": detail[:300]}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return 502, {"ok": False, "error": str(exc)}

    async def fetch_active_pods_summary(
        self,
        api_key: str,
        active_pod_id: str = "",
    ) -> Optional[dict[str, Any]]:
        """Return aggregate active pod count and hourly spend without leaking pod details."""
        if not api_key:
            return None

        status, data = await self._probe_rest_get(api_key, "pods?limit=100")
        if status != 200 or data is None:
            return None

        if isinstance(data, list):
            pods = data
        elif isinstance(data, dict):
            pods = data.get("pods")
            if pods is None:
                pods = data.get("data")
        else:
            return None

        if not isinstance(pods, list):
            return None

        active_pods = [
            p
            for p in pods
            if isinstance(p, dict)
            and str(p.get("status") or "").strip().upper() not in ("EXITED", "TERMINATED")
        ]

        total_hourly = 0.0
        active_pod_hourly: Optional[float] = None
        for p in active_pods:
            hourly = compute_pod_hourly_cost(p)
            total_hourly += hourly
            if active_pod_id and str(p.get("id") or "").strip() == active_pod_id:
                active_pod_hourly = hourly

        return {
            "activePodsCount": len(active_pods),
            "activePodsHourly": round(total_hourly, 4),
            "activePodHourly": (
                round(active_pod_hourly, 4)
                if active_pod_hourly is not None and active_pod_hourly > 0
                else None
            ),
        }
