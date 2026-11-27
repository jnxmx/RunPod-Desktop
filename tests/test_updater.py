"""Tests for Comfy Registry update checker and BridgeAPI plugin update endpoint."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_package():
    pkg_name = "runpod_desktop_pkg"
    if pkg_name in sys.modules:
        return sys.modules[pkg_name]
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
bridge_api_mod = sys.modules["runpod_desktop_pkg.bridge_api"]
updater_mod = sys.modules["runpod_desktop_pkg.updater"]


class UpdaterUnitTests(unittest.TestCase):
    def test_parse_semver_and_compare_versions(self) -> None:
        self.assertEqual(updater_mod.parse_semver("0.1.1"), (0, 1, 1, None))
        self.assertEqual(
            updater_mod.parse_semver("v0.1.2-nightly.1+build.4"),
            (0, 1, 2, ("nightly", 1)),
        )
        self.assertIsNone(updater_mod.parse_semver("not-a-version"))

        self.assertGreater(updater_mod.compare_versions("0.1.2-nightly.1", "0.1.1"), 0)
        self.assertGreater(updater_mod.compare_versions("0.1.2-nightly.2", "0.1.2-nightly.1"), 0)
        self.assertGreater(updater_mod.compare_versions("0.1.2", "0.1.2-nightly.2"), 0)
        self.assertGreater(updater_mod.compare_versions("0.1.2-hotfix.1", "0.1.1"), 0)
        self.assertEqual(updater_mod.compare_versions("v0.1.2+abc", "0.1.2"), 0)

    def test_registry_filtering_allows_pending_and_flagged_hotfixes_and_rejects_banned(self) -> None:
        entries = [
            {
                "version": "0.1.0",
                "status": "NodeVersionStatusFlagged",
                "downloadUrl": "https://cdn.comfy.org/jnx/comfyui-runpod-desktop/0.1.0/node.zip",
            },
            {
                "version": "0.1.1",
                "status": "NodeVersionStatusActive",
                "downloadUrl": "https://cdn.comfy.org/jnx/comfyui-runpod-desktop/0.1.1/node.zip",
            },
            {
                "version": "0.1.2-nightly.1",
                "status": "NodeVersionStatusPending",
                "downloadUrl": "https://cdn.comfy.org/jnx/comfyui-runpod-desktop/0.1.2-nightly.1/node.zip",
                "createdAt": "2026-11-26T22:00:00Z",
            },
            {
                "version": "0.1.2-nightly.2",
                "status": "NodeVersionStatusFlagged",
                "downloadUrl": "https://cdn.comfy.org/jnx/comfyui-runpod-desktop/0.1.2-nightly.2/node.zip",
                "createdAt": "2026-11-26T22:10:00Z",
            },
            {
                "version": "0.9.0",
                "status": "NodeVersionStatusBanned",
                "downloadUrl": "https://cdn.comfy.org/jnx/comfyui-runpod-desktop/0.9.0/node.zip",
            },
            {
                "version": "0.9.1",
                "status": "NodeVersionStatusActive",
                "deprecated": True,
                "downloadUrl": "https://cdn.comfy.org/jnx/comfyui-runpod-desktop/0.9.1/node.zip",
            },
            {
                "version": "0.9.2",
                "status": "NodeVersionStatusActive",
                "downloadUrl": "",
            },
        ]

        latest = updater_mod.select_latest_registry_version(entries, include_prerelease=True)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest["version"], "0.1.2-nightly.2")

        latest_stable = updater_mod.select_latest_registry_version(entries, include_prerelease=False)
        self.assertIsNotNone(latest_stable)
        assert latest_stable is not None
        self.assertEqual(latest_stable["version"], "0.1.1")

    def test_build_manager_update_task(self) -> None:
        cnr_task = updater_mod.build_manager_update_task(
            "0.1.2-nightly.1",
            "0.1.1",
            installed_entry={"ver": "0.1.1", "cnr_id": "comfyui-runpod-desktop"},
            ui_id="task-1",
        )
        self.assertEqual(cnr_task["kind"], "install")
        self.assertEqual(cnr_task["params"]["id"], updater_mod.CNR_NODE_ID)
        self.assertEqual(cnr_task["params"]["selected_version"], "0.1.2-nightly.1")
        self.assertEqual(cnr_task["params"]["mode"], "remote")
        self.assertEqual(cnr_task["params"]["channel"], "default")

        git_task = updater_mod.build_manager_update_task(
            "nightly",
            "0.1.1",
            installed_entry={"ver": "a" * 40, "cnr_id": "comfyui-runpod-desktop"},
            ui_id="task-2",
        )
        self.assertEqual(git_task["kind"], "update")
        self.assertEqual(git_task["params"]["node_name"], updater_mod.CNR_NODE_ID)
        self.assertEqual(git_task["params"]["node_ver"], "nightly")


class UpdaterBridgeHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp_dir.name) / "config.json"
        self.fetch_calls = 0
        self.fail_next_fetch = False
        self.versions_payload: list[dict[str, Any]] = [
            {
                "version": "0.1.2",
                "status": "NodeVersionStatusActive",
                "downloadUrl": "https://cdn.comfy.org/jnx/comfyui-runpod-desktop/0.1.2/node.zip",
            },
            {
                "version": "9.9.9-nightly.1",
                "status": "NodeVersionStatusPending",
                "downloadUrl": "https://cdn.comfy.org/jnx/comfyui-runpod-desktop/9.9.9-nightly.1/node.zip",
                "createdAt": "2026-11-26T22:15:00Z",
                "changelog": "Hotfix build",
            },
        ]

        async def fake_fetcher(_url: str) -> Any:
            self.fetch_calls += 1
            if self.fail_next_fetch:
                raise RuntimeError("registry unavailable")
            return list(self.versions_payload)

        self.updater = updater_mod.PluginUpdater(fetcher=fake_fetcher)
        self.bridge = bridge_api_mod.BridgeAPI(
            config_path=self.config_path,
            updater=self.updater,
        )

        app = web.Application()
        app.router.add_route("*", "/__rp_bridge/{tail:.*}", self.bridge.handle_bridge_request)
        self.server = TestServer(app)
        await self.server.start_server()
        self.client = TestClient(self.server)

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self._tmp_dir.cleanup()

    async def test_plugin_update_endpoint_cache_and_force(self) -> None:
        res1 = await self.client.get("/__rp_bridge/plugin_update")
        self.assertEqual(res1.status, 200)
        data1 = await res1.json()
        self.assertTrue(data1["ok"])
        self.assertTrue(data1["updateAvailable"])
        self.assertEqual(data1["latestVersion"], "9.9.9-nightly.1")
        self.assertEqual(data1["latestStatus"], "NodeVersionStatusPending")
        self.assertTrue(data1["isPrerelease"])
        self.assertEqual(self.fetch_calls, 1)

        # Second call hits cache
        res2 = await self.client.get("/__rp_bridge/plugin_update")
        self.assertEqual(res2.status, 200)
        data2 = await res2.json()
        self.assertEqual(data2["latestVersion"], "9.9.9-nightly.1")
        self.assertEqual(self.fetch_calls, 1)

        # ?force=1 bypasses cache
        res3 = await self.client.get("/__rp_bridge/plugin_update?force=1")
        self.assertEqual(res3.status, 200)
        self.assertEqual(self.fetch_calls, 2)

    async def test_plugin_update_blocked_in_remote_mode(self) -> None:
        self.bridge.state.mode = "remote"
        res = await self.client.get("/__rp_bridge/plugin_update")
        self.assertEqual(res.status, 403)

    async def test_plugin_update_fetcher_error_returns_ok_false(self) -> None:
        self.fail_next_fetch = True
        res = await self.client.get("/__rp_bridge/plugin_update?force=1")
        self.assertEqual(res.status, 200)
        data = await res.json()
        self.assertFalse(data["ok"])
        self.assertFalse(data["updateAvailable"])
        self.assertIn("registry unavailable", data["error"])


if __name__ == "__main__":
    unittest.main()
