"""Tests for RunPod Web Terminal bridge (/ __rp_bridge/pod/exec) and AGENTS.md sync."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
import tempfile
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


_load_package()
agent_bridge_mod = sys.modules["runpod_desktop_pkg.agent_bridge"]
bridge_api_mod = sys.modules["runpod_desktop_pkg.bridge_api"]
config_mod = sys.modules["runpod_desktop_pkg.config"]
pod_terminal_mod = sys.modules["runpod_desktop_pkg.pod_terminal"]


class PodTerminalUnitTests(unittest.TestCase):
    def test_check_command_safety(self) -> None:
        self.assertIsNotNone(pod_terminal_mod.check_command_safety("printenv"))
        self.assertIsNotNone(pod_terminal_mod.check_command_safety("ls && env"))
        self.assertIsNotNone(pod_terminal_mod.check_command_safety("export -p"))
        self.assertIsNotNone(pod_terminal_mod.check_command_safety("cat /proc/1/environ"))
        self.assertIsNotNone(pod_terminal_mod.check_command_safety("echo $HF_TOKEN"))
        self.assertIsNotNone(pod_terminal_mod.check_command_safety("echo ${RUNPOD_API_KEY}"))
        self.assertIsNotNone(
            pod_terminal_mod.check_command_safety("cat /path/RunPod-Desktop/config.json")
        )

        self.assertIsNone(
            pod_terminal_mod.check_command_safety(
                "aria2c -x 16 -s 16 -c -d /workspace/ComfyUI/models/vae -o vae.safetensors https://huggingface.co/example/vae.safetensors"
            )
        )
        self.assertIsNone(
            pod_terminal_mod.check_command_safety(
                "curl -fL --retry 3 -o /workspace/ComfyUI/models/loras/a.safetensors https://example.com/a.safetensors"
            )
        )
        self.assertIsNone(
            pod_terminal_mod.check_command_safety("hf download Comfy-Org/MOSS-Image --local-dir /workspace/tmp")
        )
        self.assertIsNone(
            pod_terminal_mod.check_command_safety(
                "git clone https://github.com/example/node /workspace/ComfyUI/custom_nodes/node"
            )
        )
        self.assertIsNone(
            pod_terminal_mod.check_command_safety('python3 -c "import torch; print(torch.cuda.is_available())"')
        )

    def test_redact_secrets(self) -> None:
        raw = (
            "runpod=rpa_1234567890abcdef "
            "hf=hf_AbCdEfGhIjKlMnOpQrStUv "
            "openai=sk-proj_1234567890abcdef "
            "gh=ghp_1234567890abcdefghij1234 "
            "HF_TOKEN=supersecretvalue123 "
            "custom=my_custom_secret_value"
        )
        redacted = pod_terminal_mod.redact_secrets(raw, extra_secrets=["my_custom_secret_value"])
        self.assertNotIn("rpa_1234567890abcdef", redacted)
        self.assertNotIn("hf_AbCdEfGhIjKlMnOpQrStUv", redacted)
        self.assertNotIn("sk-proj_1234567890abcdef", redacted)
        self.assertNotIn("ghp_1234567890abcdefghij1234", redacted)
        self.assertNotIn("supersecretvalue123", redacted)
        self.assertNotIn("my_custom_secret_value", redacted)
        self.assertIn("[REDACTED_RUNPOD_KEY]", redacted)
        self.assertIn("[REDACTED_HF_TOKEN]", redacted)
        self.assertIn("[REDACTED_API_KEY]", redacted)
        self.assertIn("[REDACTED_GITHUB_TOKEN]", redacted)
        self.assertIn("HF_TOKEN=[REDACTED]", redacted)
        self.assertIn("custom=[REDACTED]", redacted)

    def test_sync_agent_standing_instructions_preserves_user_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp)
            agents_file = agent_dir / "AGENTS.md"
            user_note = "# My Custom Rules\nAlways reply concisely."
            agents_file.write_text(user_note + "\n", encoding="utf-8")

            pod = {"podId": "abc123xyz99", "gpuName": "RTX 4090"}
            ok = agent_bridge_mod.sync_agent_standing_instructions(
                pod,
                "http://127.0.0.1:8199",
                agent_data_dir=agent_dir,
            )
            self.assertTrue(ok)
            content = agents_file.read_text(encoding="utf-8")
            self.assertIn(user_note, content)
            self.assertIn(agent_bridge_mod.AGENTS_BLOCK_BEGIN, content)
            self.assertIn("abc123xyz99", content)
            self.assertIn("http://127.0.0.1:8199/__rp_bridge/pod/exec", content)

            # Updating with a new pod replaces the managed block cleanly without duplicating
            ok2 = agent_bridge_mod.sync_agent_standing_instructions(
                {"podId": "pod999new11", "gpuName": "RTX 5090"},
                "http://127.0.0.1:8200",
                agent_data_dir=agent_dir,
            )
            self.assertTrue(ok2)
            updated = agents_file.read_text(encoding="utf-8")
            self.assertEqual(updated.count(agent_bridge_mod.AGENTS_BLOCK_BEGIN), 1)
            self.assertNotIn("abc123xyz99", updated)
            self.assertIn("pod999new11", updated)

            # Clearing restores user notes only
            ok3 = agent_bridge_mod.sync_agent_standing_instructions(
                None,
                agent_data_dir=agent_dir,
            )
            self.assertTrue(ok3)
            cleared = agents_file.read_text(encoding="utf-8")
            self.assertEqual(cleared.strip(), user_note)
            self.assertNotIn(agent_bridge_mod.AGENTS_BLOCK_BEGIN, cleared)

            # When no user notes exist, clearing unlinks AGENTS.md
            agents_file.write_text(
                agent_bridge_mod.build_runpod_agents_block(pod, "http://127.0.0.1:8188"),
                encoding="utf-8",
            )
            agent_bridge_mod.sync_agent_standing_instructions(None, agent_data_dir=agent_dir)
            self.assertFalse(agents_file.exists())


class PodTerminalBridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp_dir.name)
        self.config_path = self.tmp_path / "config.json"
        self.agent_dir = self.tmp_path / "__agent"
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.saved_api_key = "rpa_TestSavedSecretKey987654321"
        config_mod.set_api_key(self.saved_api_key, self.config_path)

        self.received_chunk_counts: list[int] = []
        self.decoded_commands: list[str] = []
        self.next_stdout = "hello from pod"
        self.next_exit_code = 0

        pod_app = web.Application()

        async def ws_terminal(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            b64_parts: list[str] = []
            chunk_re = re.compile(r"printf '%s' '([^']*)' >> /tmp/\.rp_cmd_[0-9a-f]+\.b64")
            marker_re = re.compile(r"printf '__RP_S_%s__\\n' '([0-9a-f]+)'")
            tok = ""
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(msg.data)
                if payload.get("type") != "input":
                    continue
                line = payload.get("data", "")
                for cm in chunk_re.finditer(line):
                    b64_parts.append(cm.group(1))
                mm = marker_re.search(line)
                if mm:
                    tok = mm.group(1)
                    self.received_chunk_counts.append(len(b64_parts))
                    cmd = base64.b64decode("".join(b64_parts)).decode("utf-8")
                    self.decoded_commands.append(cmd)
                    out_payload = (
                        f"__RP_S_{tok}__\r\n"
                        f"{self.next_stdout}\r\n"
                        f"__RP_E_{tok}:{self.next_exit_code}__\r\n"
                    )
                    await ws.send_str(json.dumps({"type": "output", "data": out_payload}))
                    await ws.close()
                    break
            return ws

        pod_app.router.add_get("/ws/terminal", ws_terminal)
        self.pod_server = TestServer(pod_app)
        await self.pod_server.start_server()

        pod_ws_origin = f"ws://127.0.0.1:{self.pod_server.port}"
        pod_http_origin = f"http://127.0.0.1:{self.pod_server.port}"

        executor = pod_terminal_mod.PodTerminalExecutor(
            ws_origin_builder=lambda _pid: pod_ws_origin,
            http_origin_builder=lambda _pid: pod_http_origin,
        )
        self.bridge = bridge_api_mod.BridgeAPI(
            config_path=self.config_path,
            pod_executor=executor,
            agent_data_dir=self.agent_dir,
        )

        bridge_app = web.Application()
        bridge_app.router.add_route("*", "/__rp_bridge/{tail:.*}", self.bridge.handle_bridge_request)
        self.bridge_server = TestServer(bridge_app)
        await self.bridge_server.start_server()
        self.client = TestClient(self.bridge_server)

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.bridge_server.close()
        await self.pod_server.close()
        self._tmp_dir.cleanup()

    async def test_exec_blocked_in_deployer_mode_and_for_non_loopback_headers(self) -> None:
        # Deployer mode returns 400
        res = await self.client.post("/__rp_bridge/pod/exec", json={"command": "pwd"})
        self.assertEqual(res.status, 400)

        # Connect to pod
        conn = await self.client.post(
            "/__rp_bridge/connect",
            json={"podId": "9m22ly94ns203s", "gpuName": "RTX 4090"},
        )
        self.assertEqual(conn.status, 200)

        # Cross-origin or proxied request returns 403
        res_proxy = await self.client.post(
            "/__rp_bridge/pod/exec",
            json={"command": "pwd"},
            headers={"X-Forwarded-For": "10.0.0.5"},
        )
        self.assertEqual(res_proxy.status, 403)

        res_origin = await self.client.post(
            "/__rp_bridge/pod/exec",
            json={"command": "pwd"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(res_origin.status, 403)

    async def test_connect_exec_chunked_redaction_and_disconnect_lifecycle(self) -> None:
        conn = await self.client.post(
            "/__rp_bridge/connect",
            json={"podId": "9m22ly94ns203s", "gpuName": "RTX 4090"},
        )
        self.assertEqual(conn.status, 200)

        agents_path = self.agent_dir / "AGENTS.md"
        self.assertTrue(agents_path.is_file())
        agents_text = agents_path.read_text(encoding="utf-8")
        self.assertIn(f"http://127.0.0.1:{self.bridge_server.port}/__rp_bridge/pod/exec", agents_text)
        self.assertIn("9m22ly94ns203s", agents_text)

        # Normal command + secret redaction in output
        self.next_stdout = f"done key={self.saved_api_key} hf=hf_TestSecretToken123456"
        res1 = await self.client.post("/__rp_bridge/pod/exec", json={"command": "echo hi"})
        self.assertEqual(res1.status, 200)
        data1 = await res1.json()
        self.assertTrue(data1["ok"])
        self.assertEqual(data1["exitCode"], 0)
        self.assertNotIn(self.saved_api_key, data1["output"])
        self.assertNotIn("hf_TestSecretToken123456", data1["output"])
        self.assertIn("[REDACTED]", data1["output"])
        self.assertIn("[REDACTED_HF_TOKEN]", data1["output"])
        self.assertEqual(self.decoded_commands[-1], "echo hi")

        # Long command (>2500 chars) uses multiple base64 chunks
        long_cmd = "echo " + ("x" * 2600)
        self.next_stdout = "chunked ok"
        self.next_exit_code = 0
        res2 = await self.client.post(
            "/__rp_bridge/pod/exec",
            json={"command": long_cmd, "cwd": "/workspace"},
        )
        self.assertEqual(res2.status, 200)
        data2 = await res2.json()
        self.assertTrue(data2["ok"])
        self.assertEqual(data2["output"], "chunked ok")
        self.assertEqual(self.decoded_commands[-1], long_cmd)
        self.assertGreaterEqual(self.received_chunk_counts[-1], 3)

        # Disconnect clears managed block
        disc = await self.client.post("/__rp_bridge/disconnect", json={})
        self.assertEqual(disc.status, 200)
        self.assertFalse(agents_path.exists())

    async def test_prefers_http_exec_when_available_with_clean_stdout_stderr(self) -> None:
        http_app = web.Application()

        async def api_terminal_exec(request: web.Request) -> web.Response:
            body = await request.json()
            return web.json_response(
                {
                    "ok": True,
                    "exitCode": 0,
                    "cwd": body.get("cwd", "/workspace/ComfyUI"),
                    "stdout": f"clean out {self.saved_api_key}\n",
                    "stderr": "minor warning hf_TestSecretToken123456\n",
                    "timedOut": False,
                }
            )

        http_app.router.add_post("/api/terminal/exec", api_terminal_exec)
        http_server = TestServer(http_app)
        await http_server.start_server()
        try:
            executor = pod_terminal_mod.PodTerminalExecutor(
                ws_origin_builder=lambda _pid: f"ws://127.0.0.1:{http_server.port}",
                http_origin_builder=lambda _pid: f"http://127.0.0.1:{http_server.port}",
            )
            res = await executor.execute(
                "9m22ly94ns203s",
                "echo hi",
                extra_secrets=[self.saved_api_key],
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["transport"], "http_exec")
            self.assertEqual(res["exitCode"], 0)
            self.assertIn("[REDACTED]", res["stdout"])
            self.assertNotIn(self.saved_api_key, res["stdout"])
            self.assertIn("[REDACTED_HF_TOKEN]", res["stderr"])
            self.assertNotIn("hf_TestSecretToken123456", res["stderr"])
        finally:
            await http_server.close()


if __name__ == "__main__":
    unittest.main()
