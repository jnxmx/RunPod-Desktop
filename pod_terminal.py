"""Remote RunPod container Web Terminal execution bridge and secret guard."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import secrets
import shlex
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import WSMsgType, web

from .proxy import DEFAULT_UPSTREAM_USER_AGENT, build_pod_http_origin, build_pod_ws_origin
from .runpod_client import is_valid_pod_id

_PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP")
_MAX_OUTPUT_BYTES = 256 * 1024
_HALF_OUTPUT_BYTES = _MAX_OUTPUT_BYTES // 2
_CHUNK_CHARS = 900

_SAFETY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bprintenv\b", re.IGNORECASE),
        "Command refused: printing environment variables is blocked to protect secrets.",
    ),
    (
        re.compile(r"(?:^|[;&|\n])\s*(?:env|export\s+-p|declare\s+-x)\s*(?:$|[;&|\n>])", re.IGNORECASE),
        "Command refused: dumping environment variables is blocked to protect secrets.",
    ),
    (
        re.compile(r"/proc/[^/\s]+/environ", re.IGNORECASE),
        "Command refused: reading /proc/*/environ is blocked to protect secrets.",
    ),
    (
        re.compile(
            r"\$\{?(?:HF_TOKEN|CIVITAI_API_KEY|COMFYUI_BACKUP|API_KEY_COMFY_ORG|RUNPOD_API_KEY|AGENT_SESSION_TOKEN)\b",
            re.IGNORECASE,
        ),
        "Command refused: expanding secret environment variables is blocked.",
    ),
    (
        re.compile(r"RunPod-Desktop/config\.json", re.IGNORECASE),
        "Command refused: accessing local RunPod-Desktop/config.json is blocked.",
    ),
)

_SECRET_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"rpa_[A-Za-z0-9_]{12,}"), "[REDACTED_RUNPOD_KEY]"),
    (re.compile(r"hf_[A-Za-z0-9]{16,}"), "[REDACTED_HF_TOKEN]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED_API_KEY]"),
    (
        re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        re.compile(
            r"((?:HF_TOKEN|CIVITAI_API_KEY|COMFYUI_BACKUP|API_KEY_COMFY_ORG|RUNPOD_API_KEY)\s*=\s*)\S+"
        ),
        r"\1[REDACTED]",
    ),
)


def _is_loopback_host(host: Optional[str]) -> bool:
    if not host:
        return False
    clean = host.strip().lower()
    if clean == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(clean)
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return mapped.is_loopback
        return ip.is_loopback
    except ValueError:
        return False


def is_loopback_bridge_request(request: web.Request) -> bool:
    """Return True if request arrives directly from loopback without proxy/cross-origin headers."""
    if not _is_loopback_host(request.remote):
        return False
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    sec_fetch_site = request.headers.get("Sec-Fetch-Site")
    if sec_fetch_site is not None and sec_fetch_site.strip().lower() not in ("same-origin", "none"):
        return False
    origin = request.headers.get("Origin")
    if origin is not None:
        try:
            origin_host = urlparse(origin).hostname
        except ValueError:
            return False
        if not _is_loopback_host(origin_host):
            return False
    return True


def check_command_safety(command: str) -> Optional[str]:
    """Return a rejection reason if command attempts to read/dump secrets, else None."""
    if not isinstance(command, str):
        return "Invalid command."
    for pattern, reason in _SAFETY_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub known API keys, tokens, and secret assignments from terminal output."""
    if not text:
        return ""
    scrubbed = text
    for secret in extra_secrets:
        if not isinstance(secret, str):
            continue
        clean = secret.strip()
        if len(clean) >= 8:
            scrubbed = scrubbed.replace(clean, "[REDACTED]")
    for pattern, replacement in _SECRET_REDACTION_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return scrubbed


def _cap_output(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return text
    head = encoded[:_HALF_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    tail = encoded[-_HALF_OUTPUT_BYTES:].decode("utf-8", errors="ignore")
    return f"{head}\n...[truncated]...\n{tail}"


class PodTerminalExecutor:
    """Executes shell commands inside the active RunPod pod via ComfyUI-Web-Terminal."""

    def __init__(
        self,
        ws_origin_builder: Callable[[str], str] = build_pod_ws_origin,
        http_origin_builder: Callable[[str], str] = build_pod_http_origin,
    ) -> None:
        self._ws_origin_builder = ws_origin_builder
        self._http_origin_builder = http_origin_builder

    async def execute(
        self,
        pod_id: str,
        command: str,
        *,
        cwd: str = "/workspace/ComfyUI",
        timeout_s: float = 600.0,
        extra_secrets: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Run a shell command inside pod_id over /ws/terminal and return redacted output."""
        if not is_valid_pod_id(pod_id):
            return {"ok": False, "podId": pod_id, "exitCode": 1, "error": "Invalid podId.", "output": ""}
        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "podId": pod_id, "exitCode": 1, "error": "Command is required.", "output": ""}

        safety_error = check_command_safety(command)
        if safety_error is not None:
            return {
                "ok": False,
                "podId": pod_id,
                "exitCode": 126,
                "error": safety_error,
                "output": "",
            }

        try:
            clamped_timeout = max(5.0, min(1800.0, float(timeout_s)))
        except (TypeError, ValueError):
            clamped_timeout = 600.0

        clean_cwd = (cwd or "/workspace/ComfyUI").strip() or "/workspace/ComfyUI"
        http_origin = self._http_origin_builder(pod_id).rstrip("/")
        http_result = await self._try_http_exec(
            pod_id,
            http_origin,
            command,
            cwd=clean_cwd,
            timeout_s=clamped_timeout,
            extra_secrets=extra_secrets,
        )
        if http_result is not None:
            return http_result

        tok = secrets.token_hex(8)
        session_id = f"rp_exec_{tok}"

        ws_origin = self._ws_origin_builder(pod_id).rstrip("/")
        ws_url = f"{ws_origin}/ws/terminal?session={session_id}&cols=300&rows=50"
        headers = {
            "User-Agent": DEFAULT_UPSTREAM_USER_AGENT,
            "Origin": http_origin,
        }

        b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
        chunks = [b64[i : i + _CHUNK_CHARS] for i in range(0, len(b64), _CHUNK_CHARS)] or [""]

        b64_file = f"/tmp/.rp_cmd_{tok}.b64"
        script_lines = [f"stty -echo 2>/dev/null; rm -f {b64_file}\n"]
        for chunk in chunks:
            script_lines.append(f"printf '%s' '{chunk}' >> {b64_file}\n")
        script_lines.append(
            f"printf '__RP_S_%s__\\n' '{tok}'; "
            f"(cd {shlex.quote(clean_cwd)} && unset API_KEY_COMFY_ORG RUNPOD_API_KEY && base64 -d {b64_file} | bash) 2>&1; "
            f"__rp_ec=$?; rm -f {b64_file}; "
            f"printf '\\n__RP_E_%s:%d__\\n' '{tok}' \"$__rp_ec\"; exit\n"
        )

        start_marker = f"__RP_S_{tok}__"
        end_re = re.compile(rf"__RP_E_{tok}:(\d+)__")
        buf = ""
        exit_code: Optional[int] = None

        async def _run_session() -> None:
            nonlocal buf, exit_code
            client_timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.ws_connect(ws_url, headers=headers, heartbeat=25.0) as ws:
                    for line in script_lines:
                        await ws.send_str(json.dumps({"type": "input", "data": line}))
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                            except ValueError:
                                continue
                            if not isinstance(payload, dict):
                                continue
                            if payload.get("type") in ("output", "history") and isinstance(
                                payload.get("data"), str
                            ):
                                buf += payload["data"]
                                s_pos = buf.find(start_marker)
                                if s_pos != -1:
                                    m = end_re.search(buf, pos=s_pos + len(start_marker))
                                    if m is not None:
                                        exit_code = int(m.group(1))
                                        return
                        elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                            break

        try:
            await asyncio.wait_for(_run_session(), timeout=clamped_timeout)
        except asyncio.TimeoutError:
            partial = buf
            if start_marker in partial:
                partial = partial.split(start_marker, 1)[1]
            partial = redact_secrets(_cap_output(partial.replace("\r\n", "\n")), extra_secrets)
            return {
                "ok": False,
                "podId": pod_id,
                "cwd": clean_cwd,
                "exitCode": 124,
                "error": f"Command timed out after {int(clamped_timeout)}s.",
                "output": partial,
            }
        except Exception as exc:
            return {
                "ok": False,
                "podId": pod_id,
                "cwd": clean_cwd,
                "exitCode": 1,
                "error": redact_secrets(f"Pod terminal error: {exc}", extra_secrets),
                "output": "",
            }

        s_pos = buf.find(start_marker)
        if s_pos == -1 or exit_code is None:
            partial = redact_secrets(_cap_output(buf.replace("\r\n", "\n")), extra_secrets)
            return {
                "ok": False,
                "podId": pod_id,
                "cwd": clean_cwd,
                "exitCode": 1,
                "error": "Pod terminal session ended before command completion.",
                "output": partial,
            }

        after_start = buf[s_pos + len(start_marker) :]
        m = end_re.search(after_start)
        raw_out = after_start[: m.start()] if m is not None else after_start
        norm_out = raw_out.replace("\r\n", "\n")
        if norm_out.startswith("\n"):
            norm_out = norm_out[1:]
        if norm_out.endswith("\n"):
            norm_out = norm_out[:-1]

        redacted_out = redact_secrets(_cap_output(norm_out), extra_secrets)
        return {
            "ok": exit_code == 0,
            "podId": pod_id,
            "cwd": clean_cwd,
            "exitCode": exit_code,
            "output": redacted_out,
            "transport": "ws_pty",
        }

    async def _try_http_exec(
        self,
        pod_id: str,
        http_origin: str,
        command: str,
        *,
        cwd: str,
        timeout_s: float,
        extra_secrets: Iterable[str] = (),
    ) -> Optional[dict[str, Any]]:
        """Try ComfyUI-Web-Terminal's non-PTY POST /api/terminal/exec; return None to fall back to PTY."""
        url = f"{http_origin}/api/terminal/exec"
        headers = {
            "User-Agent": DEFAULT_UPSTREAM_USER_AGENT,
            "Origin": http_origin,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        client_timeout = aiohttp.ClientTimeout(total=timeout_s + 15.0, connect=15.0)
        try:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(
                    url,
                    headers=headers,
                    json={"command": command, "cwd": cwd, "timeout": timeout_s},
                ) as resp:
                    if resp.status in (404, 405, 501):
                        return None
                    data = await resp.json(content_type=None)
                    if not isinstance(data, dict) or "exitCode" not in data:
                        if resp.status >= 400 and isinstance(data, dict) and data.get("error"):
                            return {
                                "ok": False,
                                "podId": pod_id,
                                "cwd": cwd,
                                "exitCode": 1,
                                "stdout": "",
                                "stderr": "",
                                "output": "",
                                "error": redact_secrets(str(data["error"]), extra_secrets),
                                "transport": "http_exec",
                            }
                        return None

                    stdout = redact_secrets(_cap_output(str(data.get("stdout") or "")), extra_secrets)
                    stderr = redact_secrets(_cap_output(str(data.get("stderr") or "")), extra_secrets)
                    if stdout and stderr:
                        combined = f"{stdout.rstrip()}\n{stderr}"
                    else:
                        combined = stdout or stderr
                    exit_code = int(data.get("exitCode") or 0)
                    result: dict[str, Any] = {
                        "ok": bool(data.get("ok")) and exit_code == 0,
                        "podId": pod_id,
                        "cwd": str(data.get("cwd") or cwd),
                        "exitCode": exit_code,
                        "stdout": stdout,
                        "stderr": stderr,
                        "output": combined,
                        "transport": "http_exec",
                    }
                    if data.get("error"):
                        result["error"] = redact_secrets(str(data["error"]), extra_secrets)
                    return result
        except (aiohttp.ClientError, ValueError, asyncio.TimeoutError):
            return None
