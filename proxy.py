"""Transparent HTTP and WebSocket reverse proxy to the active RunPod pod."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import re
import zlib
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp import WSMsgType, web

from .runpod_client import is_valid_pod_id

WEB_DIR = Path(__file__).resolve().parent / "web"
RECONNECTING_HTML_PATH = WEB_DIR / "reconnecting.html"

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
    }
)

TOPBAR_SCRIPT_TAG = b'<script src="/__rp_bridge/topbar.js"></script>'
BODY_CLOSE_RE = re.compile(rb"</body>", re.IGNORECASE)


def build_pod_http_origin(pod_id: str) -> str:
    """Return the RunPod proxy HTTPS origin for a pod's ComfyUI port 7860."""
    return f"https://{pod_id}-7860.proxy.runpod.net"


def build_pod_ws_origin(pod_id: str) -> str:
    """Return the RunPod proxy WSS origin for a pod's ComfyUI port 7860."""
    return f"wss://{pod_id}-7860.proxy.runpod.net"


def _rewrite_origin_or_referer(value: str, target_origin: str) -> str:
    """Rewrite local Origin/Referer URLs so Cloudflare/RunPod ingress accepts them."""
    try:
        parsed = urlparse(value)
        if parsed.path or parsed.query:
            suffix = parsed.path or ""
            if parsed.query:
                suffix += f"?{parsed.query}"
            return f"{target_origin}{suffix}"
    except Exception:
        pass
    return target_origin


def _inject_topbar_script(html_bytes: bytes) -> bytes:
    """Inject topbar.js right before </body> (or append if no closing body tag)."""
    if TOPBAR_SCRIPT_TAG in html_bytes:
        return html_bytes
    if BODY_CLOSE_RE.search(html_bytes):
        return BODY_CLOSE_RE.sub(TOPBAR_SCRIPT_TAG + b"\n</body>", html_bytes, count=1)
    return html_bytes + b"\n" + TOPBAR_SCRIPT_TAG


def _decode_compressed_body(raw: bytes, encoding: str) -> tuple[bytes, bool]:
    """Decode gzip/deflate HTML body when upstream ignores Accept-Encoding: identity."""
    enc = encoding.strip().lower()
    if not enc or enc == "identity":
        return raw, True
    if enc == "gzip":
        return gzip.decompress(raw), True
    if enc == "deflate":
        try:
            return zlib.decompress(raw), True
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS), True
    return raw, False


def _load_reconnecting_html() -> str:
    """Read the fallback reconnecting HTML page from disk."""
    if RECONNECTING_HTML_PATH.exists():
        return RECONNECTING_HTML_PATH.read_text(encoding="utf-8")
    return (
        "<!DOCTYPE html><html lang='ru'><body style='background:#18181b;color:#f4f4f5;"
        "font-family:sans-serif;padding:40px;text-align:center'>"
        "<h2>Под недоступен или перезагружается</h2>"
        "<button onclick=\"fetch('/__rp_bridge/disconnect',{method:'POST'}).then(()=>location.reload())\">"
        "← Вернуться в деплойер</button></body></html>"
    )


class PodProxy:
    """Reusable aiohttp client session proxy for HTTP streaming and WebSockets."""

    def __init__(
        self,
        http_origin_builder: Callable[[str], str] = build_pod_http_origin,
        ws_origin_builder: Callable[[str], str] = build_pod_ws_origin,
        on_ws_text: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._http_origin_builder = http_origin_builder
        self._ws_origin_builder = ws_origin_builder
        self._on_ws_text = on_ws_text
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        """Return a shared ClientSession with auto_decompress=False for raw pass-through."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=100, ssl=True)
            timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                auto_decompress=False,
            )
        return self._session

    async def close(self) -> None:
        """Close the shared proxy ClientSession."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _build_upstream_headers(self, request: web.Request, target_origin: str, force_identity: bool = False) -> dict[str, str]:
        parsed_target = urlparse(target_origin)
        target_host = parsed_target.netloc
        headers: dict[str, str] = {}
        for k, v in request.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP_HEADERS:
                continue
            if lk == "origin":
                headers[k] = target_origin
                continue
            if lk == "referer":
                headers[k] = _rewrite_origin_or_referer(v, target_origin)
                continue
            if force_identity and lk == "accept-encoding":
                continue
            headers[k] = v

        headers["Host"] = target_host
        if force_identity:
            headers["Accept-Encoding"] = "identity"
        return headers

    def _build_response_headers(
        self,
        upstream_headers: "aiohttp.typedefs.CIMultiDictProxy[str]",
        target_origin: str,
        *,
        drop_encoding: bool = False,
        path: str = "",
        clear_cache: bool = False,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        for k, v in upstream_headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP_HEADERS:
                continue
            if drop_encoding and lk in ("content-encoding", "content-length"):
                continue
            if lk == "location" and v.startswith(target_origin):
                headers[k] = v[len(target_origin):] or "/"
                continue
            headers[k] = v

        if path == "/":
            headers["Cache-Control"] = "no-store"
        elif path.startswith("/extensions/"):
            headers["Cache-Control"] = "no-cache, no-store, must-revalidate"

        if clear_cache:
            headers["Clear-Site-Data"] = '"cache"'
        return headers

    def _reconnecting_response(self, status: int = 503) -> web.Response:
        return web.Response(
            status=status,
            text=_load_reconnecting_html(),
            content_type="text/html",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-RP-Reconnecting": "1",
            },
        )

    async def handle_http_proxy(
        self,
        request: web.Request,
        pod_id: str,
        *,
        clear_cache: bool = False,
    ) -> web.StreamResponse:
        """Proxy an HTTP request to the active pod, injecting topbar.js on GET /."""
        if not is_valid_pod_id(pod_id):
            return web.Response(status=400, text="Invalid pod ID")

        target_origin = self._http_origin_builder(pod_id)
        target_url = f"{target_origin}{request.rel_url}"
        is_root_html = request.method == "GET" and request.path == "/"
        headers = self._build_upstream_headers(request, target_origin, force_identity=is_root_html)

        session = await self.get_session()

        data_arg = None
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.body_exists:
            # aiohttp handles chunked streaming cleanly when Transfer-Encoding/Content-Length
            # are managed by the client request for an async iterable.
            headers.pop("Content-Length", None)
            headers.pop("content-length", None)

            async def _stream_request_body():
                async for chunk in request.content.iter_chunked(65536):
                    yield chunk

            data_arg = _stream_request_body()

        try:
            async with session.request(
                request.method,
                target_url,
                headers=headers,
                data=data_arg,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15 if is_root_html else None, connect=15),
            ) as upstream_resp:
                if is_root_html:
                    if upstream_resp.status in (502, 503, 504):
                        return self._reconnecting_response(status=503)

                    raw_body = await upstream_resp.read()
                    content_type = upstream_resp.headers.get("Content-Type", "").lower()
                    content_encoding = upstream_resp.headers.get("Content-Encoding", "")

                    if "text/html" in content_type or not content_type:
                        decoded_body, ok = _decode_compressed_body(raw_body, content_encoding)
                        if ok:
                            modified = _inject_topbar_script(decoded_body)
                            resp_headers = self._build_response_headers(
                                upstream_resp.headers,
                                target_origin,
                                drop_encoding=True,
                                path=request.path,
                                clear_cache=clear_cache,
                            )
                            return web.Response(
                                status=upstream_resp.status,
                                headers=resp_headers,
                                body=modified,
                            )

                    resp_headers = self._build_response_headers(
                        upstream_resp.headers,
                        target_origin,
                        drop_encoding=False,
                        path=request.path,
                        clear_cache=clear_cache,
                    )
                    return web.Response(
                        status=upstream_resp.status,
                        headers=resp_headers,
                        body=raw_body,
                    )

                resp_headers = self._build_response_headers(
                    upstream_resp.headers,
                    target_origin,
                    drop_encoding=False,
                    path=request.path,
                    clear_cache=clear_cache,
                )
                stream_resp = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
                await stream_resp.prepare(request)
                try:
                    async for chunk in upstream_resp.content.iter_chunked(65536):
                        if chunk:
                            await stream_resp.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError):
                    pass
                return stream_resp
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if is_root_html:
                return self._reconnecting_response(status=503)
            return web.Response(status=502, text=f"Upstream pod error: {exc}")

    async def handle_ws_proxy(self, request: web.Request, pod_id: str) -> web.StreamResponse:
        """Proxy a bidirectional WebSocket (/ws) preserving TEXT and BINARY frames."""
        if not is_valid_pod_id(pod_id):
            return web.Response(status=400, text="Invalid pod ID")

        http_origin = self._http_origin_builder(pod_id)
        ws_origin = self._ws_origin_builder(pod_id)
        target_ws_url = f"{ws_origin}{request.rel_url}"
        parsed_http = urlparse(http_origin)

        ws_headers = {
            "Host": parsed_http.netloc,
            "Origin": http_origin,
        }
        if "Cookie" in request.headers:
            ws_headers["Cookie"] = request.headers["Cookie"]
        if "User-Agent" in request.headers:
            ws_headers["User-Agent"] = request.headers["User-Agent"]

        session = await self.get_session()
        client_ws: Optional[web.WebSocketResponse] = None
        try:
            async with session.ws_connect(
                target_ws_url,
                headers=ws_headers,
                max_msg_size=0,
                heartbeat=30,
                timeout=aiohttp.ClientTimeout(total=None, connect=20),
            ) as upstream_ws:
                client_ws = web.WebSocketResponse(max_msg_size=0, heartbeat=30)
                await client_ws.prepare(request)

                async def _forward_client_to_upstream() -> None:
                    async for msg in client_ws:
                        if msg.type == WSMsgType.TEXT:
                            await upstream_ws.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await upstream_ws.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                            break
                        elif msg.type == WSMsgType.ERROR:
                            break

                async def _forward_upstream_to_client() -> None:
                    async for msg in upstream_ws:
                        if msg.type == WSMsgType.TEXT:
                            await client_ws.send_str(msg.data)
                            if self._on_ws_text is not None:
                                self._on_ws_text(pod_id, msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await client_ws.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                            break
                        elif msg.type == WSMsgType.ERROR:
                            break

                t1 = asyncio.create_task(_forward_client_to_upstream())
                t2 = asyncio.create_task(_forward_upstream_to_client())
                done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                for task in done:
                    with contextlib.suppress(Exception):
                        await task

                if not upstream_ws.closed:
                    await upstream_ws.close()
                if not client_ws.closed:
                    await client_ws.close()
                return client_ws
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if client_ws is not None and not client_ws.closed:
                await client_ws.close(code=1013, message=b"Upstream WebSocket unavailable")
                return client_ws
            return web.Response(status=503, text="Upstream WebSocket unavailable")
