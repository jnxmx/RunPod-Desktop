"""Local ComfyUI Agent bridge helpers (route detection, feature flags, catalog refresh)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

LOCAL_AGENT_ROUTE_PREFIXES: tuple[str, ...] = ("/api/agent", "/api/workflows")
AGENTS_BLOCK_BEGIN = "<!-- RUNPOD-DESKTOP:BEGIN -->"
AGENTS_BLOCK_END = "<!-- RUNPOD-DESKTOP:END -->"
_AGENTS_BLOCK_RE = re.compile(
    rf"\n?{re.escape(AGENTS_BLOCK_BEGIN)}.*?{re.escape(AGENTS_BLOCK_END)}\n?",
    re.DOTALL,
)


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


def resolve_agent_data_dir(agent_host_mod: Any = None) -> Optional[Path]:
    """Resolve the local comfy-agent data directory (containing AGENTS.md), if available."""
    mod = agent_host_mod if agent_host_mod is not None else _load_comfy_agent_host_module()
    host = getattr(mod, "_host", None) if mod is not None else None
    data_dir = str(getattr(host, "_data_dir", "") or "").strip() if host is not None else ""
    if data_dir:
        return Path(data_dir)
    try:
        import folder_paths  # type: ignore

        return Path(folder_paths.get_system_user_directory("agent"))
    except Exception:
        return None


def build_runpod_agents_block(active_pod: dict[str, Any], comfy_url: str) -> str:
    """Build the managed RunPod standing instructions block for comfy-agent's AGENTS.md."""
    pod_id = str(active_pod.get("podId") or "").strip()
    gpu_name = str(active_pod.get("gpuName") or "GPU").strip() or "GPU"
    base_url = (comfy_url or "http://127.0.0.1:8188").rstrip("/")
    return (
        f"{AGENTS_BLOCK_BEGIN}\n"
        "## Remote RunPod GPU Session (CRITICAL)\n"
        f"- Active ComfyUI target: RunPod Linux container (`{pod_id}`, GPU `{gpu_name}`, ComfyUI root `/workspace/ComfyUI`), proxied through `{base_url}`.\n"
        "- **Do NOT access the user's local computer:** NEVER download models into, install custom nodes into, read, or modify the local ComfyUI install, local `models/`, local `custom_nodes/`, or user home folders on this machine. Local `comfy model download` and `comfy node install` act on the wrong machine and MUST NOT be used.\n"
        "- **Never expose or inspect secrets:** NEVER print, echo, read, or dump secrets, API keys, tokens, `.env`, `config.json`, or environment variables (`env`, `printenv`, `$HF_TOKEN`, `$CIVITAI_API_KEY`, `$COMFYUI_BACKUP`, RunPod keys) locally or on the pod.\n"
        f"- **Use the RunPod Container Terminal bridge (`POST {base_url}/__rp_bridge/pod/exec`) for all server shell work:**\n"
        "  ```bash\n"
        f'  curl -sS --max-time 1800 -X POST "{base_url}/__rp_bridge/pod/exec" \\\n'
        '    -H "Content-Type: application/json" \\\n'
        '    -d \'{"command": "ls -la /workspace/ComfyUI/models", "cwd": "/workspace/ComfyUI", "timeout": 300}\'\n'
        "  ```\n"
        "  1. **Download models directly onto the pod:** Inspect `/workspace/ComfyUI/models/<folder>`, ask the user before large downloads per your normal rules, then download onto the pod using `aria2c -x 16 -s 16 -c -d /workspace/ComfyUI/models/<folder> -o <file> \"<url>\"` if `aria2c` is installed, otherwise `curl -fL --retry 3 -o /workspace/ComfyUI/models/<folder>/<file> \"<url>\"` or `hf download`.\n"
        "  2. **Search files, install Python packages & build/install custom nodes on the pod:** Perform all file searches, `pip` installs into the pod's ComfyUI Python environment, `git clone`, and custom node edits under `/workspace/ComfyUI/custom_nodes/` via the bridge.\n"
        f"  3. **Reload after changes:** After downloading models on the pod, call `refresh_catalog`. After creating, editing, or installing custom nodes on the pod, reboot the pod's ComfyUI via `curl -sS -X POST \"{base_url}/v2/manager/reboot\" -H \"Content-Type: application/json\" -d '{{}}'`, wait ~8s until `{base_url}/system_stats` responds, and then call `refresh_catalog`.\n"
        f"{AGENTS_BLOCK_END}\n"
    )


def sync_agent_standing_instructions(
    active_pod: Optional[dict[str, Any]],
    comfy_url: str = "http://127.0.0.1:8188",
    *,
    agent_data_dir: Optional[Path] = None,
    agent_host_mod: Any = None,
) -> bool:
    """Create, update, or clear the managed RunPod block inside <agent_data_dir>/AGENTS.md."""
    target_dir = agent_data_dir if agent_data_dir is not None else resolve_agent_data_dir(agent_host_mod)
    if target_dir is None:
        return False

    agents_path = Path(target_dir) / "AGENTS.md"
    existing = ""
    if agents_path.is_file():
        try:
            existing = agents_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    stripped = _AGENTS_BLOCK_RE.sub("\n", existing).strip()

    if active_pod is None:
        if not stripped:
            with contextlib.suppress(OSError):
                if agents_path.exists():
                    agents_path.unlink()
            return True
        new_content = stripped + "\n"
    else:
        block = build_runpod_agents_block(active_pod, comfy_url)
        new_content = f"{stripped}\n\n{block}" if stripped else block

    target_path = Path(target_dir)
    target_path.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".AGENTS.", suffix=".tmp", dir=str(target_path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        if os.name == "posix":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, agents_path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return True
