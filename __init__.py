"""Comfy Course Deployer — RunPod Desktop bridge custom node for ComfyUI."""

from __future__ import annotations

import os
from typing import Optional

from aiohttp import web

from .agent_bridge import (
    LocalAgentCatalogRefresher,
    get_local_agent_feature_flag,
    is_local_agent_path,
)
from .bridge_api import BridgeAPI
from .proxy import PodProxy
from .runpod_client import RunPodClient

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

LOCAL_HEALTHCHECK_PATHS = frozenset(
    {
        "/system_stats",
        "/queue",
        "/api/system_stats",
        "/api/queue",
    }
)

DEPLOYER_ASSET_PATHS = {
    "/styles.css": "styles.css",
    "/app.js": "app.js",
    "/gpu_data.js": "gpu_data.js",
}


def create_bridge_middleware(
    bridge_api: Optional[BridgeAPI] = None,
    pod_proxy: Optional[PodProxy] = None,
):
    """Build the aiohttp middleware that routes between deployer, remote pod, and healthchecks."""
    proxy = pod_proxy or PodProxy(agent_feature_provider=get_local_agent_feature_flag)
    if bridge_api is not None:
        api = bridge_api
    else:
        refresher = LocalAgentCatalogRefresher(on_status_change=proxy.broadcast_agent_status)
        api = BridgeAPI(
            agent_refresher=lambda pod_id, force=False: refresher.refresh_for_pod(pod_id, force=force)
        )

    @web.middleware
    async def runpod_desktop_middleware(request: web.Request, handler):
        path = request.path

        if path.startswith("/__rp_bridge/"):
            return await api.handle_bridge_request(request)

        if is_local_agent_path(path):
            return await handler(request)

        if api.state.mode == "remote":
            pod_id = str((api.state.active_pod or {}).get("podId") or "").strip()
            if not pod_id:
                api.state.mode = "deployer"
            else:
                upgrade = request.headers.get("Upgrade", "").lower()
                if path == "/ws" or upgrade == "websocket":
                    return await proxy.handle_ws_proxy(request, pod_id)
                clear_cache = api.consume_clear_cache_flag() if (request.method == "GET" and path == "/") else False
                return await proxy.handle_http_proxy(request, pod_id, clear_cache=clear_cache)

        # Deployer mode
        if path in LOCAL_HEALTHCHECK_PATHS:
            return await handler(request)

        if request.method == "GET" and path == "/":
            clear_cache = api.consume_clear_cache_flag()
            return api.serve_deployer_file("index.html", clear_cache=clear_cache)

        if request.method == "GET" and path in DEPLOYER_ASSET_PATHS:
            return api.serve_deployer_file(DEPLOYER_ASSET_PATHS[path])

        return await handler(request)

    return runpod_desktop_middleware, api, proxy


def _install_on_comfy_server() -> None:
    """Attach the bridge middleware to ComfyUI PromptServer when running inside ComfyUI."""
    if os.environ.get("RUNPOD_DESKTOP_DISABLE") == "1":
        return

    try:
        from server import PromptServer
    except ImportError:
        return

    server_instance = getattr(PromptServer, "instance", None)
    app = getattr(server_instance, "app", None)
    if app is None:
        return

    if getattr(app, "_runpod_desktop_installed", False):
        return

    client = RunPodClient()
    proxy = PodProxy(agent_feature_provider=get_local_agent_feature_flag)
    refresher = LocalAgentCatalogRefresher(on_status_change=proxy.broadcast_agent_status)
    api = BridgeAPI(
        runpod_client=client,
        agent_refresher=lambda pod_id, force=False: refresher.refresh_for_pod(pod_id, force=force),
    )
    middleware, _, _ = create_bridge_middleware(bridge_api=api, pod_proxy=proxy)

    app.middlewares.insert(0, middleware)
    app._runpod_desktop_installed = True

    async def _cleanup(_app: web.Application) -> None:
        await client.close()
        await proxy.close()

    app.on_shutdown.append(_cleanup)


_install_on_comfy_server()

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "create_bridge_middleware",
]
