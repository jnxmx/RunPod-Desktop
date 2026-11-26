"""Persistent configuration storage for ComfyUI-RunPod-Desktop.

Stores the RunPod API key and deployment settings exclusively on the local
Python backend (never in browser localStorage) so remote ComfyUI extensions
sharing the 127.0.0.1 origin cannot read the API secret.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLUGIN_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "api_key": "",
    "custom_template_id": None,
    "active_pod": None,
    "mode": "deployer",
    "auto_download_outputs": True,
}


def load_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load configuration from disk, returning defaults if missing or invalid."""
    data = dict(DEFAULT_CONFIG)
    if not config_path.exists():
        return data
    try:
        raw = config_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            if isinstance(parsed.get("api_key"), str):
                data["api_key"] = parsed["api_key"].strip()
            tpl = parsed.get("custom_template_id")
            if tpl is None or isinstance(tpl, str):
                data["custom_template_id"] = tpl.strip() if isinstance(tpl, str) and tpl.strip() else None
            pod = parsed.get("active_pod")
            if isinstance(pod, dict) and isinstance(pod.get("podId"), str):
                data["active_pod"] = pod
            mode = parsed.get("mode")
            if mode in ("deployer", "remote"):
                data["mode"] = mode
            if isinstance(parsed.get("auto_download_outputs"), bool):
                data["auto_download_outputs"] = parsed["auto_download_outputs"]
    except (OSError, ValueError):
        pass
    return data


def save_config(updates: dict[str, Any], config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Atomically merge and persist configuration updates with restrictive permissions."""
    current = load_config(config_path)
    for key in ("api_key", "custom_template_id", "active_pod", "mode", "auto_download_outputs"):
        if key in updates:
            current[key] = updates[key]

    if current.get("mode") not in ("deployer", "remote"):
        current["mode"] = "deployer"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config.",
        suffix=".tmp",
        dir=str(config_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if os.name == "posix":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, config_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return current


def get_api_key(config_path: Path = CONFIG_PATH) -> str:
    """Return the saved RunPod API key or empty string."""
    return str(load_config(config_path).get("api_key") or "").strip()


def set_api_key(api_key: str, config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Persist a trimmed RunPod API key."""
    return save_config({"api_key": api_key.strip()}, config_path=config_path)


def clear_api_key(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Remove the saved RunPod API key."""
    return save_config({"api_key": ""}, config_path=config_path)


def mask_api_key(api_key: str) -> str:
    """Return a UI-safe masked representation of the API key."""
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"
