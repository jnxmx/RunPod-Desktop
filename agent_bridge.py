"""Local ComfyUI Agent bridge helpers (route detection, feature flags, catalog refresh)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, Callable, Optional

LOCAL_AGENT_ROUTE_PREFIXES: tuple[str, ...] = ("/api/agent", "/api/workflows")


def is_local_agent_path(path: str) -> bool:
    """Return True if path belongs to the local ComfyUI Agent API routes."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in LOCAL_AGENT_ROUTE_PREFIXES)


def _load_comfy_agent_host_module() -> Any:
    """Import ComfyUI's app.agent_host module if available in the runtime."""
    try:
        from app import agent_host

        return agent_host
    except ImportError:
        return None


def get_local_agent_feature_flag(agent_host_mod: Any = None) -> Optional[dict[str, Any]]:
    """Return the local ComfyUI Agent feature flag dict, or None when unavailable."""
    mod = agent_host_mod if agent_host_mod is not None else _load_comfy_agent_host_module()
    if mod is None:
        return None
    feature_flag_fn = getattr(mod, "feature_flag", None)
    if not callable(feature_flag_fn):
        return None
    try:
        flag = feature_flag_fn()
    except Exception:
        return None
    if isinstance(flag, dict) and isinstance(flag.get("state"), str):
        return dict(flag)
    return None


class LocalAgentCatalogRefresher:
    """Recycles the local comfy-agent child so it re-reads /object_info from the active pod."""

    def __init__(
        self,
        *,
        agent_host_getter: Callable[[], Any] = _load_comfy_agent_host_module,
        on_status_change: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self._agent_host_getter = agent_host_getter
        self._on_status_change = on_status_change
        self._lock = asyncio.Lock()
        self._synced_pod_id: Optional[str] = None
        self._watch_task: Optional[asyncio.Task[None]] = None

    def _start_watcher(self, mod: Any) -> None:
        if self._on_status_change is None:
            return
        if self._watch_task is not None and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = asyncio.create_task(self._watch_until_settled(mod))

    async def refresh_for_pod(self, pod_id: str, *, force: bool = False) -> dict[str, Any]:
        """Stop the local agent child once per pod (or on force) so AgentHost respawns it."""
        clean_pod_id = (pod_id or "").strip()
        if not clean_pod_id:
            return {"ok": False, "reason": "no_pod"}

        async with self._lock:
            if not force and self._synced_pod_id == clean_pod_id:
                return {"ok": True, "refreshed": False, "reason": "already_synced"}

            mod = self._agent_host_getter()
            host = getattr(mod, "_host", None) if mod is not None else None
            if host is None or not callable(getattr(host, "status", None)):
                return {"ok": False, "refreshed": False, "reason": "no_local_agent"}

            stopping = getattr(host, "_stopping", None)
            if stopping is not None and callable(getattr(stopping, "is_set", None)) and stopping.is_set():
                return {"ok": False, "refreshed": False, "reason": "agent_stopping"}

            try:
                st = host.status()
            except Exception:
                return {"ok": False, "refreshed": False, "reason": "status_error"}

            if not isinstance(st, dict):
                return {"ok": False, "refreshed": False, "reason": "not_ready"}

            state = st.get("state")
            if state == "starting":
                self._synced_pod_id = clean_pod_id
                self._start_watcher(mod)
                return {"ok": True, "refreshed": False, "reason": "already_starting"}
            if state != "ready":
                return {"ok": False, "refreshed": False, "reason": str(state or "not_ready")}

            # Guard against exhausting AgentHost._supervise's MAX_RESTARTS budget
            # if restarts happened within STABLE_S (60s).
            restarts = int(st.get("restarts") or 0)
            max_restarts = int(st.get("max_restarts") or 3)
            if restarts >= max(1, max_restarts - 1):
                return {"ok": False, "refreshed": False, "reason": "restart_budget_low"}

            handle = getattr(host, "_handle", None)
            if (
                handle is None
                or getattr(handle, "returncode", None) is not None
                or not callable(getattr(handle, "stop", None))
            ):
                return {"ok": False, "refreshed": False, "reason": "no_handle"}

            self._synced_pod_id = clean_pod_id
            await asyncio.to_thread(handle.stop, 5.0)
            self._start_watcher(mod)
            return {"ok": True, "refreshed": True}

    async def _watch_until_settled(self, mod: Any) -> None:
        """Poll local agent status and push changes to connected frontend WebSockets."""
        if self._on_status_change is None:
            return
        last_emitted: Optional[dict[str, Any]] = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30.0
        try:
            while True:
                flag = get_local_agent_feature_flag(mod)
                if flag is not None:
                    if flag != last_emitted:
                        last_emitted = dict(flag)
                        with contextlib.suppress(Exception):
                            await self._on_status_change(dict(flag))
                    if flag.get("state") in ("ready", "failed"):
                        break
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            pass
