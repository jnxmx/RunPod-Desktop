"""Comfy Registry update checker and ComfyUI-Manager v4 task helpers."""

from __future__ import annotations

import re
import time
from typing import Any, Awaitable, Callable, Optional, Union

import aiohttp

CNR_NODE_ID = "comfyui-runpod-desktop"
REPO_URL = "https://github.com/jnxmx/RunPod-Desktop"
REGISTRY_VERSIONS_URL = f"https://api.comfy.org/nodes/{CNR_NODE_ID}/versions"
UPDATE_CACHE_TTL_SECONDS = 300.0
ALLOWED_VERSION_STATUSES = frozenset(
    {
        "NodeVersionStatusActive",
        "NodeVersionStatusPending",
        "NodeVersionStatusFlagged",
    }
)

_SEMVER_RE = re.compile(
    r"^[vV]?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

PreSegment = Union[int, str]
SemVerTuple = tuple[int, int, int, Optional[tuple[PreSegment, ...]]]


def parse_semver(version: str) -> Optional[SemVerTuple]:
    """Parse a SemVer string into (major, minor, patch, prerelease_tuple)."""
    if not isinstance(version, str):
        return None
    raw = version.strip()
    if not raw:
        return None
    match = _SEMVER_RE.match(raw)
    if match is None:
        return None

    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    pre_raw = match.group("pre")
    if pre_raw is None:
        return (major, minor, patch, None)

    segments: list[PreSegment] = []
    for part in pre_raw.split("."):
        if part.isdigit():
            segments.append(int(part))
        else:
            segments.append(part.lower())
    return (major, minor, patch, tuple(segments))


def is_prerelease_version(version: str) -> bool:
    """Return True if version parses as a SemVer string with a prerelease tag."""
    parsed = parse_semver(version)
    return bool(parsed is not None and parsed[3] is not None)


def compare_versions(a: str, b: str) -> int:
    """Compare two SemVer strings, returning -1, 0, or 1 per SemVer 2.0.0."""
    pa = parse_semver(a)
    pb = parse_semver(b)
    if pa is None and pb is None:
        return 0
    if pa is None:
        return -1
    if pb is None:
        return 1

    base_a = pa[:3]
    base_b = pb[:3]
    if base_a < base_b:
        return -1
    if base_a > base_b:
        return 1

    pre_a = pa[3]
    pre_b = pb[3]
    if pre_a is None and pre_b is None:
        return 0
    if pre_a is None:
        return 1
    if pre_b is None:
        return -1

    for seg_a, seg_b in zip(pre_a, pre_b):
        if seg_a == seg_b:
            continue
        is_int_a = isinstance(seg_a, int)
        is_int_b = isinstance(seg_b, int)
        if is_int_a and not is_int_b:
            return -1
        if not is_int_a and is_int_b:
            return 1
        return -1 if seg_a < seg_b else 1  # type: ignore[operator]

    if len(pre_a) < len(pre_b):
        return -1
    if len(pre_a) > len(pre_b):
        return 1
    return 0


def is_installable_registry_version(entry: dict[str, Any]) -> bool:
    """Return True if a Comfy Registry version entry is valid and downloadable."""
    if not isinstance(entry, dict):
        return False
    version = str(entry.get("version") or "").strip()
    if parse_semver(version) is None:
        return False
    download_url = entry.get("downloadUrl")
    if not isinstance(download_url, str) or not download_url.strip():
        return False
    if entry.get("deprecated") is True:
        return False
    status = str(entry.get("status") or "").strip()
    if status and status not in ALLOWED_VERSION_STATUSES:
        return False
    return True


def select_latest_registry_version(
    entries: list[dict[str, Any]],
    *,
    include_prerelease: bool = True,
) -> Optional[dict[str, Any]]:
    """Select the highest installable version entry from a Comfy Registry versions list."""
    best: Optional[dict[str, Any]] = None
    for entry in entries:
        if not is_installable_registry_version(entry):
            continue
        version = str(entry.get("version") or "").strip()
        if not include_prerelease and is_prerelease_version(version):
            continue
        if best is None:
            best = entry
            continue
        cmp = compare_versions(version, str(best.get("version") or ""))
        if cmp > 0:
            best = entry
        elif cmp == 0:
            created = str(entry.get("createdAt") or "")
            best_created = str(best.get("createdAt") or "")
            if created > best_created:
                best = entry
    return best


def build_manager_update_task(
    target_version: str,
    current_version: str,
    *,
    installed_entry: Optional[dict[str, Any]] = None,
    ui_id: str,
    client_id: str = "runpod-deployer-updater",
) -> dict[str, Any]:
    """Build a ComfyUI-Manager v4 QueueTaskItem payload for updating this plugin."""
    installed_ver = str((installed_entry or {}).get("ver") or "").strip()
    is_git_install = installed_ver == "nightly" or bool(_GIT_SHA_RE.match(installed_ver))

    if target_version == "nightly" and is_git_install:
        return {
            "ui_id": ui_id,
            "client_id": client_id,
            "kind": "update",
            "params": {
                "node_name": CNR_NODE_ID,
                "node_ver": "nightly",
            },
        }

    return {
        "ui_id": ui_id,
        "client_id": client_id,
        "kind": "install",
        "params": {
            "id": CNR_NODE_ID,
            "version": current_version,
            "selected_version": target_version,
            "repository": REPO_URL,
            "channel": "default",
            "mode": "remote",
        },
    }


async def _default_json_fetcher(url: str) -> Any:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.get(url, headers={"Accept": "application/json"}) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


class PluginUpdater:
    """Queries Comfy Registry for plugin releases including pending/nightly hotfixes."""

    def __init__(
        self,
        versions_url: str = REGISTRY_VERSIONS_URL,
        cache_ttl_seconds: float = UPDATE_CACHE_TTL_SECONDS,
        fetcher: Optional[Callable[[str], Awaitable[Any]]] = None,
    ) -> None:
        self.versions_url = versions_url
        self.cache_ttl_seconds = cache_ttl_seconds
        self._fetcher = fetcher or _default_json_fetcher
        self._cache_ts: float = 0.0
        self._cache_version: Optional[str] = None
        self._cache_data: Optional[dict[str, Any]] = None

    async def check_for_updates(
        self,
        current_version: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Fetch and compare available versions against current_version."""
        now = time.monotonic()
        if (
            not force
            and self._cache_data is not None
            and self._cache_version == current_version
            and (now - self._cache_ts) < self.cache_ttl_seconds
        ):
            return dict(self._cache_data)

        try:
            raw = await self._fetcher(self.versions_url)
            if isinstance(raw, list):
                entries = raw
            elif isinstance(raw, dict) and isinstance(raw.get("versions"), list):
                entries = raw["versions"]
            else:
                raise ValueError("Unexpected Comfy Registry response format")

            latest = select_latest_registry_version(entries, include_prerelease=True)
            latest_stable = select_latest_registry_version(entries, include_prerelease=False)

            latest_ver = str(latest.get("version") or "").strip() if latest else None
            update_available = bool(
                latest_ver and compare_versions(latest_ver, current_version) > 0
            )

            result: dict[str, Any] = {
                "ok": True,
                "nodeId": CNR_NODE_ID,
                "repository": REPO_URL,
                "currentVersion": current_version,
                "latestVersion": latest_ver,
                "latestStatus": str(latest.get("status") or "") if latest else "",
                "latestCreatedAt": str(latest.get("createdAt") or "") if latest else "",
                "latestChangelog": str(latest.get("changelog") or "") if latest else "",
                "isPrerelease": is_prerelease_version(latest_ver) if latest_ver else False,
                "latestStableVersion": (
                    str(latest_stable.get("version") or "").strip() if latest_stable else None
                ),
                "updateAvailable": update_available,
                "recommendedSelectedVersion": latest_ver if update_available else None,
                "checkedAtMs": int(time.time() * 1000),
            }
            self._cache_ts = time.monotonic()
            self._cache_version = current_version
            self._cache_data = result
            return dict(result)
        except Exception as exc:
            err_msg = str(exc).strip() or type(exc).__name__
            return {
                "ok": False,
                "nodeId": CNR_NODE_ID,
                "repository": REPO_URL,
                "currentVersion": current_version,
                "updateAvailable": False,
                "error": err_msg,
            }
