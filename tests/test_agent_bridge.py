"""Tests for local ComfyUI Agent routing, feature flag overlay, and catalog refresh."""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
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


PKG = _load_package()
agent_bridge = sys.modules["runpod_desktop_pkg.agent_bridge"]
bridge_api_mod = sys.modules["runpod_desktop_pkg.bridge_api"]
proxy_mod = sys.modules["runpod_desktop_pkg.proxy"]


class _FakeHandle:
    def __init__(self, on_stop=None) -> None:
        self.returncode = None
        self.stop_calls: list[float] = []
        self._on_stop = on_stop

    def stop(self, timeout: float = 10.0) -> int:
        self.stop_calls.append(timeout)
        self.returncode = 0
        if self._on_stop is not None:
            self._on_stop()
        return 0


class _FakeAgentHost:
    def __init__(self, status_dict: dict[str, Any], handle: _FakeHandle) -> None:
        self._status = dict(status_dict)
        self._handle = handle
        self._stopping = threading.Event()

    def status(self) -> dict[str, Any]:
        return dict(self._status)


class _FakeAgentHostModule:
    def __init__(self, host: _FakeAgentHost) -> None:
        self._host = host
        self.flags_sequence: list[dict[str, Any]] = []

    def feature_flag(self) -> dict[str, Any]:
        if self.flags_sequence:
            if len(self.flags_sequence) > 1:
                return self.flags_sequence.pop(0)
            return self.flags_sequence[0]
        st = self._host.status()
        flag: dict[str, Any] = {"state": st["state"]}
        if st.get("reason"):
            flag["reason"] = st["reason"]
        return flag


class AgentBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_is_local_agent_path(self) -> None:
        self.assertTrue(agent_bridge.is_local_agent_path("/api/agent"))
        self.assertTrue(agent_bridge.is_local_agent_path("/api/agent/run-mode"))
        self.assertTrue(agent_bridge.is_local_agent_path("/api/agent/events"))
        self.assertTrue(agent_bridge.is_local_agent_path("/api/workflows"))
        self.assertTrue(agent_bridge.is_local_agent_path("/api/workflows/123"))

        self.assertFalse(agent_bridge.is_local_agent_path("/api/userdata/workflows/a.json"))
        self.assertFalse(agent_bridge.is_local_agent_path("/api/workflows_extra"))
        self.assertFalse(agent_bridge.is_local_agent_path("/api/object_info"))
        self.assertFalse(agent_bridge.is_local_agent_path("/ws"))

    async def asyncSetUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp_dir.name) / "config.json"
        self.upstream_requests: list[tuple[str, str]] = []
        self.upstream_ws_paths: list[str] = []
        self.upstream_features_mode = "missing_agent"
        self.upstream_features_gzip = False
        self.upstream_ws_queue: asyncio.Queue[str] = asyncio.Queue()
        self.local_requests: list[tuple[str, str]] = []
        self.local_agent_flag: dict[str, Any] = {"state": "ready"}

        # Build mock upstream RunPod pod server
        upstream_app = web.Application()

        async def upstream_http(request: web.Request) -> web.Response:
            self.upstream_requests.append((request.method, request.path_qs))
            if request.path in ("/features", "/api/features"):
                payload: dict[str, Any] = {"assets": False, "deploy_to_cloud": False}
                if self.upstream_features_mode == "failed_agent":
                    payload["agent"] = {"state": "failed", "reason": "package_missing"}
                raw = json.dumps(payload).encode("utf-8")
                if self.upstream_features_gzip:
                    return web.Response(
                        body=gzip.compress(raw),
                        headers={
                            "Content-Type": "application/json; charset=utf-8",
                            "Content-Encoding": "gzip",
                        },
                    )
                return web.json_response(payload)
            if request.path == "/api/object_info":
                return web.json_response({"RemotePodNode": {"name": "RemotePodNode"}})
            if request.path == "/prompt":
                body = await request.text()
                return web.json_response({"proxied": True, "body": body})
            if request.path == "/api/userdata/workflows/test.json":
                return web.json_response({"id": "remote-userdata"})
            return web.json_response({"proxied": True, "path": request.path_qs})

        async def upstream_ws(request: web.Request) -> web.WebSocketResponse:
            self.upstream_ws_paths.append(request.path_qs)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            if request.path == "/ws":
                await ws.send_str(
                    json.dumps({"type": "feature_flags", "data": {"assets": False}})
                )
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "agent_status",
                            "data": {"state": "failed", "reason": "package_missing"},
                        }
                    )
                )
            elif request.path == "/ws/terminal":
                await ws.send_str(
                    json.dumps({"type": "agent_status", "data": {"state": "failed"}})
                )
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await ws.send_str(msg.data)
            return ws

        upstream_app.router.add_route("GET", "/ws", upstream_ws)
        upstream_app.router.add_route("GET", "/ws/terminal", upstream_ws)
        upstream_app.router.add_route("*", "/{tail:.*}", upstream_http)

        self.upstream_server = TestServer(upstream_app)
        await self.upstream_server.start_server()

        upstream_http_origin = f"http://{self.upstream_server.host}:{self.upstream_server.port}"
        upstream_ws_origin = f"ws://{self.upstream_server.host}:{self.upstream_server.port}"

        self.proxy = proxy_mod.PodProxy(
            http_origin_builder=lambda _pod_id: upstream_http_origin,
            ws_origin_builder=lambda _pod_id: upstream_ws_origin,
            agent_feature_provider=lambda: dict(self.local_agent_flag),
        )
        self.api = bridge_api_mod.BridgeAPI(config_path=self.config_path)
        middleware, _, _ = PKG.create_bridge_middleware(
            bridge_api=self.api,
            pod_proxy=self.proxy,
        )

        local_app = web.Application(middlewares=[middleware])

        async def local_agent_http(request: web.Request) -> web.Response:
            self.local_requests.append((request.method, request.path_qs))
            return web.json_response({"local": True, "path": request.path_qs})

        async def local_agent_events(request: web.Request) -> web.WebSocketResponse:
            self.local_requests.append((request.method, request.path_qs))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str(json.dumps({"type": "local_agent_hello"}))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await ws.send_str(msg.data)
            return ws

        # Mount subapps matching comfy_agent/host.py registration
        local_app.router.add_route("*", "/api/agent", local_agent_http)
        agent_sub = web.Application()
        agent_sub.router.add_route("GET", "/events", local_agent_events)
        agent_sub.router.add_route("*", "/{tail:.*}", local_agent_http)
        local_app.add_subapp("/api/agent", agent_sub)

        local_app.router.add_route("*", "/api/workflows", local_agent_http)
        wf_sub = web.Application()
        wf_sub.router.add_route("*", "/{tail:.*}", local_agent_http)
        local_app.add_subapp("/api/workflows", wf_sub)

        self.local_server = TestServer(local_app)
        self.client = TestClient(self.local_server)
        await self.client.start_server()

        # Switch bridge into remote mode
        resp = await self.client.post(
            "/__rp_bridge/connect",
            json={"podId": "testpod1234", "gpuName": "RTX 4090", "price": 0.5},
        )
        self.assertEqual(resp.status, 200)

    async def asyncTearDown(self) -> None:
        await self.proxy.close()
        await self.api.client.close()
        await self.client.close()
        await self.upstream_server.close()
        self._tmp_dir.cleanup()

    async def test_local_agent_routes_bypass_pod_proxy(self) -> None:
        r1 = await self.client.get("/api/agent/identity")
        self.assertEqual(r1.status, 200)
        self.assertEqual((await r1.json())["local"], True)

        r2 = await self.client.post("/api/agent/threads/new/messages", json={"content": "hi"})
        self.assertEqual(r2.status, 200)
        self.assertEqual((await r2.json())["local"], True)

        r3 = await self.client.get("/api/workflows?limit=100")
        self.assertEqual(r3.status, 200)
        self.assertEqual((await r3.json())["local"], True)

        async with self.client.ws_connect("/api/agent/events?token=abc") as ws:
            msg = await ws.receive_json()
            self.assertEqual(msg, {"type": "local_agent_hello"})

        self.assertEqual(self.upstream_requests, [])
        self.assertEqual(self.upstream_ws_paths, [])
        self.assertIn(("GET", "/api/agent/identity"), self.local_requests)
        self.assertIn(("POST", "/api/agent/threads/new/messages"), self.local_requests)
        self.assertIn(("GET", "/api/workflows?limit=100"), self.local_requests)
        self.assertIn(("GET", "/api/agent/events?token=abc"), self.local_requests)

    async def test_comfy_routes_still_proxy_to_pod(self) -> None:
        r1 = await self.client.get("/api/object_info")
        self.assertEqual(r1.status, 200)
        self.assertIn("RemotePodNode", await r1.json())

        r2 = await self.client.post("/prompt", json={"prompt": {}})
        self.assertEqual(r2.status, 200)
        self.assertTrue((await r2.json())["proxied"])

        r3 = await self.client.get("/api/userdata/workflows/test.json")
        self.assertEqual(r3.status, 200)
        self.assertEqual((await r3.json())["id"], "remote-userdata")

        self.assertEqual(self.local_requests, [])
        self.assertIn(("GET", "/api/object_info"), self.upstream_requests)
        self.assertIn(("POST", "/prompt"), self.upstream_requests)
        self.assertIn(("GET", "/api/userdata/workflows/test.json"), self.upstream_requests)

    async def test_features_overlay_local_agent_flag(self) -> None:
        for mode in ("missing_agent", "failed_agent"):
            self.upstream_features_mode = mode
            self.upstream_features_gzip = False
            for path in ("/features", "/api/features"):
                resp = await self.client.get(path)
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertEqual(data["agent"], {"state": "ready"})

        # Also verify gzipped upstream /features is handled cleanly without Content-Type conflicts
        self.upstream_features_mode = "failed_agent"
        self.upstream_features_gzip = True
        resp = await self.client.get("/api/features")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["agent"], {"state": "ready"})

    async def test_ws_rewrites_and_broadcasts_agent_status_only_on_main_ws(self) -> None:
        async with self.client.ws_connect("/ws") as ws:
            msg1 = await ws.receive_json()
            self.assertEqual(msg1["type"], "feature_flags")
            self.assertEqual(msg1["data"]["agent"], {"state": "ready"})

            msg2 = await ws.receive_json()
            self.assertEqual(msg2["type"], "agent_status")
            self.assertEqual(msg2["data"], {"state": "ready"})

            await self.proxy.broadcast_agent_status({"state": "starting"})
            msg3 = await ws.receive_json()
            self.assertEqual(msg3, {"type": "agent_status", "data": {"state": "starting"}})

        async with self.client.ws_connect("/ws/terminal") as term_ws:
            term_msg = await term_ws.receive_json()
            self.assertEqual(
                term_msg,
                {"type": "agent_status", "data": {"state": "failed"}},
            )

    async def test_catalog_refresher_connect_force_and_budget_guard(self) -> None:
        events: list[dict[str, Any]] = []
        handle = _FakeHandle()
        host = _FakeAgentHost({"state": "ready", "restarts": 0, "max_restarts": 3}, handle)
        mod = _FakeAgentHostModule(host)

        def _on_stop() -> None:
            host._status["state"] = "starting"
            mod.flags_sequence = [{"state": "starting"}, {"state": "ready"}]

        handle._on_stop = _on_stop

        async def _record_event(flag: dict[str, Any]) -> None:
            events.append(flag)

        refresher = agent_bridge.LocalAgentCatalogRefresher(
            agent_host_getter=lambda: mod,
            on_status_change=_record_event,
        )

        tmp_cfg = Path(self._tmp_dir.name) / "refresher_cfg.json"
        api = bridge_api_mod.BridgeAPI(
            config_path=tmp_cfg,
            agent_refresher=lambda pod_id, force=False: refresher.refresh_for_pod(pod_id, force=force),
        )
        app = web.Application()
        app.router.add_route("*", "/__rp_bridge/{tail:.*}", api.handle_bridge_request)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # First connect triggers background refresh
            res = await client.post("/__rp_bridge/connect", json={"podId": "podA1234"})
            self.assertEqual(res.status, 200)
            for _ in range(30):
                if len(handle.stop_calls) == 1 and len(events) >= 2:
                    break
                await asyncio.sleep(0.05)

            self.assertEqual(len(handle.stop_calls), 1)
            self.assertEqual(events, [{"state": "starting"}, {"state": "ready"}])

            # Same pod without force is skipped
            host._status["state"] = "ready"
            handle.returncode = None
            dup = await refresher.refresh_for_pod("podA1234", force=False)
            self.assertEqual(dup, {"ok": True, "refreshed": False, "reason": "already_synced"})
            self.assertEqual(len(handle.stop_calls), 1)

            # POST /__rp_bridge/agent/refresh forces a refresh
            mod.flags_sequence = [{"state": "ready"}]
            ref_res = await client.post("/__rp_bridge/agent/refresh")
            self.assertEqual(ref_res.status, 200)
            self.assertEqual(await ref_res.json(), {"ok": True, "refreshed": True})
            self.assertEqual(len(handle.stop_calls), 2)

            # Restart budget guard blocks stop when restarts >= max_restarts - 1
            host._status = {"state": "ready", "restarts": 2, "max_restarts": 3}
            handle.returncode = None
            guarded = await refresher.refresh_for_pod("podB5678", force=False)
            self.assertEqual(guarded, {"ok": False, "refreshed": False, "reason": "restart_budget_low"})
            self.assertEqual(len(handle.stop_calls), 2)
        finally:
            await client.close()
            await api.client.close()


if __name__ == "__main__":
    unittest.main()
