"""Bridge API endpoints (/ __rp_bridge/*) and runtime mode management."""

from __future__ import annotations

import asyncio
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from aiohttp import web

from .agent_bridge import sync_agent_standing_instructions
from .config import (
    CONFIG_PATH,
    clear_api_key,
    get_api_key,
    load_config,
    mask_api_key,
    save_config,
    set_api_key,
)
from .pod_terminal import PodTerminalExecutor, is_loopback_bridge_request
from .runpod_client import RunPodClient, is_valid_pod_id
from .updater import PluginUpdater

PLUGIN_VERSION = "0.1.1"
WEB_DIR = Path(__file__).resolve().parent / "web"
DEPLOYER_DIR = WEB_DIR / "deployer"


@dataclass
class BridgeState:
    """In-memory runtime state for the active Comfy Desktop bridge session."""

    mode: str = "deployer"
    active_pod: Optional[dict[str, Any]] = None
    clear_cache_next: bool = False


class BridgeAPI:
    """Serves /__rp_bridge/* endpoints with strict mode-based privilege isolation."""

    def __init__(
        self,
        runpod_client: Optional[RunPodClient] = None,
        config_path: Path = CONFIG_PATH,
        url_opener: Callable[[str], bool] = webbrowser.open,
        agent_refresher: Optional[Callable[[str, bool], Awaitable[dict[str, Any]]]] = None,
        updater: Optional[PluginUpdater] = None,
        pod_executor: Optional[PodTerminalExecutor] = None,
        agent_data_dir: Optional[Path] = None,
    ) -> None:
        self.client = runpod_client or RunPodClient()
        self.config_path = config_path
        self.url_opener = url_opener
        self._agent_refresher = agent_refresher
        self.updater = updater or PluginUpdater()
        self.pod_executor = pod_executor or PodTerminalExecutor()
        self._agent_data_dir = agent_data_dir
        self._local_comfy_url: str = "http://127.0.0.1:8188"
        cfg = load_config(self.config_path)
        self.state = BridgeState(
            mode="deployer",
            active_pod=cfg.get("active_pod"),
            clear_cache_next=False,
        )
        self._pods_summary_cache_ts: float = 0.0
        self._pods_summary_cache_data: Optional[dict[str, Any]] = None
        self._sync_agent_instructions(self.state.active_pod, self._local_comfy_url)

    def _sync_agent_instructions(
        self,
        active_pod: Optional[dict[str, Any]],
        comfy_url: Optional[str] = None,
    ) -> None:
        try:
            sync_agent_standing_instructions(
                active_pod,
                comfy_url or self._local_comfy_url,
                agent_data_dir=self._agent_data_dir,
            )
        except Exception:
            pass

    def consume_clear_cache_flag(self) -> bool:
        """Return and clear the one-shot Clear-Site-Data cache flag."""
        if self.state.clear_cache_next:
            self.state.clear_cache_next = False
            return True
        return False

    async def handle_bridge_request(self, request: web.Request) -> web.StreamResponse:
        """Dispatch /__rp_bridge/* routes."""
        path = request.path
        method = request.method

        if path == "/__rp_bridge/status" and method == "GET":
            return await self._handle_status(request)

        if path == "/__rp_bridge/plugin_update" and method == "GET":
            return await self._handle_plugin_update(request)

        if path == "/__rp_bridge/pods_summary" and method == "GET":
            return await self._handle_pods_summary(request)

        if path == "/__rp_bridge/config" and method == "POST":
            return await self._handle_config(request)

        if path == "/__rp_bridge/probe_key" and method == "POST":
            return await self._handle_probe_key(request)

        if path == "/__rp_bridge/balance" and method == "GET":
            return await self._handle_balance(request)

        if path.startswith("/__rp_bridge/runpod/v2/"):
            subpath = path[len("/__rp_bridge/runpod/v2/"):]
            return await self._handle_runpod_v2(request, subpath)

        if path.startswith("/__rp_bridge/pod_health/") and method == "GET":
            pod_id = path[len("/__rp_bridge/pod_health/"):]
            return await self._handle_pod_health(pod_id)

        if (
            path.startswith("/__rp_bridge/pods/")
            and path.endswith("/logs_ws")
            and method == "GET"
        ):
            pod_id = path[len("/__rp_bridge/pods/"):-len("/logs_ws")].strip("/")
            return await self._handle_pod_logs_ws(request, pod_id)

        if path == "/__rp_bridge/connect" and method == "POST":
            return await self._handle_connect(request)

        if path == "/__rp_bridge/disconnect" and method == "POST":
            return await self._handle_disconnect(request)

        if path == "/__rp_bridge/pod/terminate" and method == "POST":
            return await self._handle_pod_terminate(request)

        if path == "/__rp_bridge/pod/restart" and method == "POST":
            return await self._handle_pod_restart(request)

        if path == "/__rp_bridge/pod/exec" and method == "POST":
            return await self._handle_pod_exec(request)

        if path == "/__rp_bridge/open_url" and method == "POST":
            return await self._handle_open_url(request)

        if path == "/__rp_bridge/auto_download" and method == "POST":
            return await self._handle_auto_download(request)

        if path == "/__rp_bridge/agent/refresh" and method == "POST":
            return await self._handle_agent_refresh(request)

        if path == "/__rp_bridge/topbar.js" and method == "GET":
            return self._serve_static(WEB_DIR / "topbar.js", "application/javascript")

        if path == "/__rp_bridge/reconnecting.html" and method == "GET":
            return self._serve_static(WEB_DIR / "reconnecting.html", "text/html")

        return web.json_response({"error": "Not found"}, status=404)

    def serve_deployer_file(self, rel_path: str, *, clear_cache: bool = False) -> web.Response:
        """Serve a file from web/deployer with no-store headers for HTML/JS."""
        safe_rel = rel_path.lstrip("/") or "index.html"
        target = (DEPLOYER_DIR / safe_rel).resolve()
        try:
            target.relative_to(DEPLOYER_DIR.resolve())
        except ValueError:
            return web.Response(status=403, text="Forbidden")

        if not target.is_file():
            return web.Response(status=404, text="Not found")

        suffix = target.suffix.lower()
        content_type = {
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".json": "application/json",
            ".svg": "image/svg+xml",
        }.get(suffix, "application/octet-stream")

        return self._serve_static(target, content_type, clear_cache=clear_cache)

    def _serve_static(
        self,
        file_path: Path,
        content_type: str,
        *,
        clear_cache: bool = False,
    ) -> web.Response:
        if not file_path.is_file():
            return web.Response(status=404, text="Not found")
        headers = {"Cache-Control": "no-store"}
        if clear_cache:
            headers["Clear-Site-Data"] = '"cache"'
        return web.Response(
            text=file_path.read_text(encoding="utf-8"),
            content_type=content_type,
            charset="utf-8",
            headers=headers,
        )

    def _require_deployer_mode(self) -> Optional[web.Response]:
        if self.state.mode != "deployer":
            return web.json_response(
                {"error": "Endpoint disabled while connected to remote pod."},
                status=403,
            )
        return None

    async def _handle_status(self, _request: web.Request) -> web.Response:
        cfg = load_config(self.config_path)
        api_key = str(cfg.get("api_key") or "")
        return web.json_response(
            {
                "mode": self.state.mode,
                "hasApiKey": bool(api_key),
                "maskedKey": mask_api_key(api_key),
                "customTemplateId": cfg.get("custom_template_id"),
                "activePod": self.state.active_pod,
                "autoDownloadOutputs": bool(cfg.get("auto_download_outputs", True)),
                "version": PLUGIN_VERSION,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_plugin_update(self, request: web.Request) -> web.Response:
        denied = self._require_deployer_mode()
        if denied is not None:
            return denied

        force = str(request.query.get("force") or "").strip().lower() in ("1", "true", "yes")
        data = await self.updater.check_for_updates(PLUGIN_VERSION, force=force)
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    async def _handle_config(self, request: web.Request) -> web.Response:
        denied = self._require_deployer_mode()
        if denied is not None:
            return denied

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "Invalid payload"}, status=400)

        if body.get("clearApiKey") is True:
            clear_api_key(self.config_path)
        elif isinstance(body.get("apiKey"), str):
            entered = body["apiKey"].strip()
            if entered:
                set_api_key(entered, self.config_path)
            else:
                clear_api_key(self.config_path)

        if "customTemplateId" in body:
            tpl = body.get("customTemplateId")
            clean_tpl = tpl.strip() if isinstance(tpl, str) and tpl.strip() else None
            save_config({"custom_template_id": clean_tpl}, self.config_path)

        cfg = load_config(self.config_path)
        api_key = str(cfg.get("api_key") or "")
        return web.json_response(
            {
                "ok": True,
                "hasApiKey": bool(api_key),
                "maskedKey": mask_api_key(api_key),
                "customTemplateId": cfg.get("custom_template_id"),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_probe_key(self, request: web.Request) -> web.Response:
        denied = self._require_deployer_mode()
        if denied is not None:
            return denied

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "authValid": False, "error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "authValid": False, "error": "Invalid payload"}, status=400)

        api_key = str(body.get("apiKey") or "").strip()
        tpl_id = str(body.get("templateId") or "casls01eko").strip() or "casls01eko"
        status, data = await self.client.probe_api_key(api_key, template_id=tpl_id)
        return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})

    async def _handle_balance(self, _request: web.Request) -> web.Response:
        denied = self._require_deployer_mode()
        if denied is not None:
            return denied

        api_key = get_api_key(self.config_path)
        (status, data), key_meta = await asyncio.gather(
            self.client.fetch_balance(api_key),
            self.client.fetch_api_key_metadata(api_key),
        )
        if status == 200 and isinstance(data, dict) and key_meta is not None:
            data["keyInfo"] = key_meta
        return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})

    async def _handle_runpod_v2(self, request: web.Request, subpath: str) -> web.StreamResponse:
        denied = self._require_deployer_mode()
        if denied is not None:
            return denied

        api_key = get_api_key(self.config_path)
        return await self.client.proxy_rest_v2(request, subpath, api_key)

    async def _handle_pod_health(self, pod_id: str) -> web.Response:
        denied = self._require_deployer_mode()
        if denied is not None:
            return denied

        status, data = await self.client.check_pod_health(pod_id)
        return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})

    async def _handle_pod_logs_ws(self, request: web.Request, pod_id: str) -> web.StreamResponse:
        denied = self._require_deployer_mode()
        if denied is not None:
            return denied

        api_key = get_api_key(self.config_path)
        return await self.client.stream_pod_logs_ws(request, pod_id, api_key)

    async def _handle_pod_exec(self, request: web.Request) -> web.Response:
        if not is_loopback_bridge_request(request):
            return web.json_response({"ok": False, "error": "Loopback access only."}, status=403)

        pod_id = str((self.state.active_pod or {}).get("podId") or "").strip()
        if self.state.mode != "remote" or not is_valid_pod_id(pod_id):
            return web.json_response({"ok": False, "error": "No active RunPod pod connected."}, status=400)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON body."}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "Invalid payload."}, status=400)

        command = body.get("command")
        if not isinstance(command, str) or not command.strip():
            return web.json_response({"ok": False, "error": "Field 'command' must be a non-empty string."}, status=400)

        cwd_raw = body.get("cwd")
        cwd = cwd_raw.strip() if isinstance(cwd_raw, str) and cwd_raw.strip() else "/workspace/ComfyUI"
        timeout_raw = body.get("timeout", 600.0)
        try:
            timeout_s = float(timeout_raw) if timeout_raw is not None else 600.0
        except (TypeError, ValueError):
            timeout_s = 600.0

        result = await self.pod_executor.execute(
            pod_id,
            command,
            cwd=cwd,
            timeout_s=timeout_s,
            extra_secrets=[get_api_key(self.config_path)],
        )
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def _handle_connect(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "Invalid payload"}, status=400)

        pod_id = str(body.get("podId") or "").strip()
        if not is_valid_pod_id(pod_id):
            return web.json_response({"error": "Invalid podId"}, status=400)

        price_val = body.get("price")
        try:
            price = float(price_val) if price_val is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0

        started_raw = body.get("startedAtMs")
        try:
            started_at_ms = int(float(started_raw)) if started_raw is not None else int(time.time() * 1000)
            if started_at_ms <= 0:
                started_at_ms = int(time.time() * 1000)
        except (TypeError, ValueError):
            started_at_ms = int(time.time() * 1000)

        try:
            active_pods_count = int(body.get("activePodsCount")) if body.get("activePodsCount") is not None else 1
            if active_pods_count < 0:
                active_pods_count = 1
        except (TypeError, ValueError):
            active_pods_count = 1

        try:
            active_pods_hourly = float(body.get("activePodsHourly")) if body.get("activePodsHourly") is not None else price
            if active_pods_hourly < 0:
                active_pods_hourly = price
        except (TypeError, ValueError):
            active_pods_hourly = price

        active_pod = {
            "podId": pod_id,
            "gpuName": str(body.get("gpuName") or "GPU").strip() or "GPU",
            "price": price,
            "cloudType": str(body.get("cloudType") or "COMMUNITY").strip().upper(),
            "startedAtMs": started_at_ms,
            "activePodsCount": active_pods_count,
            "activePodsHourly": active_pods_hourly,
        }
        self.state.active_pod = active_pod
        self.state.mode = "remote"
        self.state.clear_cache_next = True
        self._pods_summary_cache_ts = 0.0
        self._pods_summary_cache_data = None
        save_config({"active_pod": active_pod}, self.config_path)
        port = request.url.port or 8188
        self._local_comfy_url = f"http://127.0.0.1:{port}"
        self._sync_agent_instructions(active_pod, self._local_comfy_url)
        if self._agent_refresher is not None:
            asyncio.create_task(self._safe_refresh_agent(pod_id, force=False))
        return web.json_response({"ok": True, "mode": "remote", "activePod": active_pod})

    async def _handle_disconnect(self, _request: web.Request) -> web.Response:
        self.state.mode = "deployer"
        self.state.clear_cache_next = True
        self._pods_summary_cache_ts = 0.0
        self._pods_summary_cache_data = None
        self._sync_agent_instructions(None, self._local_comfy_url)
        return web.json_response({"ok": True, "mode": "deployer"})

    async def _handle_pod_terminate(self, _request: web.Request) -> web.Response:
        active = self.state.active_pod
        pod_id = str((active or {}).get("podId") or "").strip()
        if not is_valid_pod_id(pod_id):
            self.state.mode = "deployer"
            self.state.clear_cache_next = True
            self._pods_summary_cache_ts = 0.0
            self._pods_summary_cache_data = None
            self._sync_agent_instructions(None, self._local_comfy_url)
            return web.json_response({"ok": False, "error": "Нет активного пода."}, status=400)

        api_key = get_api_key(self.config_path)
        status, result = await self.client.terminate_pod(pod_id, api_key)
        if status == 200 and result.get("ok"):
            self.state.active_pod = None
            self.state.mode = "deployer"
            self.state.clear_cache_next = True
            self._pods_summary_cache_ts = 0.0
            self._pods_summary_cache_data = None
            save_config({"active_pod": None}, self.config_path)
            self._sync_agent_instructions(None, self._local_comfy_url)
        return web.json_response(result, status=status)

    async def _handle_pods_summary(self, _request: web.Request) -> web.Response:
        now = time.monotonic()
        if (
            self._pods_summary_cache_data is not None
            and (now - self._pods_summary_cache_ts) < 15.0
        ):
            return web.json_response(
                self._pods_summary_cache_data,
                headers={"Cache-Control": "no-store"},
            )

        api_key = get_api_key(self.config_path)
        active = self.state.active_pod
        active_pod_id = str((active or {}).get("podId") or "").strip()
        summary = await self.client.fetch_active_pods_summary(api_key, active_pod_id=active_pod_id)

        if summary is None:
            fallback_count = 1 if isinstance(active, dict) else 0
            fallback_hourly = 0.0
            if isinstance(active, dict):
                try:
                    fallback_count = max(0, int(active.get("activePodsCount") or 1))
                except (TypeError, ValueError):
                    fallback_count = 1
                try:
                    fallback_hourly = float(
                        active.get("activePodsHourly")
                        if active.get("activePodsHourly") is not None
                        else (active.get("price") or 0.0)
                    )
                except (TypeError, ValueError):
                    fallback_hourly = 0.0
            return web.json_response(
                {
                    "ok": False,
                    "activePodsCount": fallback_count,
                    "activePodsHourly": fallback_hourly,
                    "activePodHourly": None,
                },
                headers={"Cache-Control": "no-store"},
            )

        payload = {"ok": True, **summary}
        self._pods_summary_cache_ts = time.monotonic()
        self._pods_summary_cache_data = payload

        if isinstance(self.state.active_pod, dict):
            self.state.active_pod["activePodsCount"] = summary["activePodsCount"]
            self.state.active_pod["activePodsHourly"] = summary["activePodsHourly"]
            active_pod_hourly = summary.get("activePodHourly")
            if isinstance(active_pod_hourly, (int, float)) and active_pod_hourly > 0:
                self.state.active_pod["price"] = float(active_pod_hourly)

        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def _handle_pod_restart(self, _request: web.Request) -> web.Response:
        active = self.state.active_pod
        pod_id = str((active or {}).get("podId") or "").strip()
        if not is_valid_pod_id(pod_id):
            return web.json_response({"ok": False, "error": "Нет активного пода."}, status=400)

        api_key = get_api_key(self.config_path)
        status, result = await self.client.restart_pod(pod_id, api_key)
        return web.json_response(result, status=status)

    async def _handle_open_url(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        url = str((body or {}).get("url") or "").strip() if isinstance(body, dict) else ""
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            return web.json_response({"error": "Only valid http(s) URLs are allowed"}, status=400)

        opened = bool(self.url_opener(url))
        return web.json_response({"ok": opened, "url": url})

    async def _handle_auto_download(self, request: web.Request) -> web.Response:
        cfg = load_config(self.config_path)
        current = bool(cfg.get("auto_download_outputs", True))
        enabled = not current
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("enabled"), bool):
                enabled = body["enabled"]
        except Exception:
            pass
        save_config({"auto_download_outputs": enabled}, self.config_path)
        return web.json_response(
            {"ok": True, "autoDownloadOutputs": enabled},
            headers={"Cache-Control": "no-store"},
        )

    async def _safe_refresh_agent(self, pod_id: str, *, force: bool = False) -> dict[str, Any]:
        if self._agent_refresher is None:
            return {"ok": False, "reason": "unconfigured"}
        try:
            return await self._agent_refresher(pod_id, force)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _handle_agent_refresh(self, _request: web.Request) -> web.Response:
        pod_id = str((self.state.active_pod or {}).get("podId") or "").strip()
        if self.state.mode != "remote" or not is_valid_pod_id(pod_id):
            return web.json_response(
                {"ok": False, "reason": "not_connected"},
                status=400,
                headers={"Cache-Control": "no-store"},
            )
        result = await self._safe_refresh_agent(pod_id, force=True)
        return web.json_response(result, headers={"Cache-Control": "no-store"})
