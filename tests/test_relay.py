"""Regression tests for the webrc relay protocol.

These tests intentionally exercise relay.py through real HTTP and WebSocket
connections.  agent.py is not imported because it has Windows-only capture and
input-injection imports and side effects.
"""

import asyncio
import json
import struct
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import relay


TOKEN = "unit-test-relay-token"


def make_app(token):
    app = web.Application()
    app["token"] = token
    app["reg"] = relay.Registry()
    app.add_routes(
        [
            web.get("/", relay.index),
            web.get("/ws", relay.viewer_ws),
            web.get("/agent", relay.agent_ws),
        ]
    )
    app.on_startup.append(relay.start_watchdog)
    app.on_cleanup.append(relay.stop_watchdog)
    return app


def make_frame(payload, key=False):
    """Build a minimal binary frame with the flags byte relay inspects."""
    # <HHhhHH>: width, height, cursor_x, cursor_y, flags, strip_count
    flags = 1 if key else 0
    return struct.pack("<HHhhHH", 320, 240, 1, 2, flags, 1) + payload


class RelayTestCase(unittest.IsolatedAsyncioTestCase):
    TIMEOUT = 2.0

    async def asyncSetUp(self):
        self.app = make_app(TOKEN)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.sockets = []

    async def asyncTearDown(self):
        for ws in reversed(self.sockets):
            try:
                await asyncio.wait_for(ws.close(), 1.0)
            except Exception:
                pass
        if self.client is not None:
            await self.client.close()

    async def connect_agent(self, name):
        url = f"/agent?token={quote(TOKEN)}&name={quote(name)}"
        ws = await self.client.ws_connect(url)
        self.sockets.append(ws)
        return ws

    async def connect_viewer(self, pc=None):
        url = f"/ws?token={quote(TOKEN)}"
        if pc is not None:
            url += f"&pc={quote(pc)}"
        ws = await self.client.ws_connect(url)
        self.sockets.append(ws)
        return ws

    async def receive(self, ws, timeout=None):
        return await asyncio.wait_for(ws.receive(), timeout or self.TIMEOUT)

    async def wait_json(self, ws, predicate=lambda obj: True):
        """Read text messages until one matches; unrelated control messages are ignored."""
        for _ in range(64):
            msg = await self.receive(ws)
            if msg.type == WSMsgType.TEXT:
                obj = json.loads(msg.data)
                if predicate(obj):
                    return obj
                continue
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                self.fail(f"WebSocket closed while waiting for JSON: {msg!r}")
                return None
        self.fail("too many unrelated messages while waiting for JSON")
        return None

    async def receive_binary(self, ws):
        for _ in range(64):
            msg = await self.receive(ws)
            if msg.type == WSMsgType.BINARY:
                return msg.data
            if msg.type == WSMsgType.TEXT:
                continue
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                self.fail(f"WebSocket closed while waiting for binary frame: {msg!r}")
                return b""
        self.fail("too many text messages while waiting for binary frame")
        return b""

    async def expect_close(self, ws, code):
        msg = await self.receive(ws)
        self.assertEqual(msg.type, WSMsgType.CLOSE)
        self.assertEqual(msg.data, code)
        self.assertEqual(msg.extra, "replaced")

    async def assert_no_message(self, ws, timeout=0.50):
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout)
        except asyncio.TimeoutError:
            return
        self.fail(f"unexpected WebSocket message: {msg!r}")

    async def settle_viewer(self, ws, pc):
        """Consume the pc/agent/pcs burst sent when a viewer joins."""
        obj = await self.wait_json(
            ws,
            lambda obj: obj.get("t") == "pcs"
            and any(item.get("name") == pc for item in obj.get("list", [])),
        )
        return obj

    async def settle_agent(self, ws, viewers):
        """Consume the viewer count and initial keyframe request for an empty hub."""
        await self.wait_json(
            ws, lambda obj: obj.get("t") == "viewers" and obj.get("n") == viewers
        )
        await self.wait_json(ws, lambda obj: obj.get("t") == "kf")


class AuthenticationTests(RelayTestCase):
    async def test_missing_and_wrong_token_reject_homepage_and_both_websockets(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 403)
        await response.read()

        response = await self.client.get("/?token=wrong-token")
        self.assertEqual(response.status, 403)
        await response.read()

        for path in ("/ws", "/agent"):
            for query in ("", "?token=wrong-token"):
                with self.assertRaises(WSServerHandshakeError) as raised:
                    await self.client.ws_connect(path + query)
                self.assertEqual(raised.exception.status, 403)

    async def test_valid_token_serves_homepage_without_caching(self):
        response = await self.client.get(f"/?token={quote(TOKEN)}")
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn("<title>webrc</title>", body)
        self.assertIn("new WebSocket", body)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")


class MultiAgentRoutingTests(RelayTestCase):
    async def test_agents_and_viewers_are_isolated_for_input_and_frames(self):
        alpha = await self.connect_agent("alpha")
        beta = await self.connect_agent("beta")
        viewer_alpha = await self.connect_viewer("alpha")
        viewer_beta = await self.connect_viewer("beta")

        await self.settle_viewer(viewer_alpha, "alpha")
        await self.settle_viewer(viewer_beta, "beta")
        await self.settle_agent(alpha, 1)
        await self.settle_agent(beta, 1)

        alpha_input = json.dumps({"t": "move", "x": 11, "y": 22, "test": "alpha"})
        beta_input = json.dumps({"t": "move", "x": 33, "y": 44, "test": "beta"})
        await viewer_alpha.send_str(alpha_input)
        received_alpha = await self.wait_json(
            alpha, lambda obj: obj.get("test") == "alpha"
        )
        self.assertEqual(received_alpha["t"], "move")
        await self.assert_no_message(beta)

        await viewer_beta.send_str(beta_input)
        received_beta = await self.wait_json(
            beta, lambda obj: obj.get("test") == "beta"
        )
        self.assertEqual(received_beta["t"], "move")
        await self.assert_no_message(alpha)

        alpha_text = json.dumps({"t": "desk", "locked": True, "test": "alpha-state"})
        await alpha.send_str(alpha_text)
        received = await self.wait_json(
            viewer_alpha, lambda obj: obj.get("test") == "alpha-state"
        )
        self.assertTrue(received["locked"])
        await self.assert_no_message(viewer_beta)

        alpha_frame = make_frame(b"alpha-frame")
        await alpha.send_bytes(alpha_frame)
        frame = await self.receive_binary(viewer_alpha)
        self.assertEqual(frame, alpha_frame)
        await self.assert_no_message(viewer_beta)

        beta_frame = make_frame(b"beta-frame")
        await beta.send_bytes(beta_frame)
        self.assertEqual(await self.receive_binary(viewer_beta), beta_frame)
        await self.assert_no_message(viewer_alpha)

    async def test_switching_pc_detaches_old_agent_and_routes_later_input(self):
        alpha = await self.connect_agent("alpha")
        beta = await self.connect_agent("beta")
        viewer = await self.connect_viewer("alpha")
        await self.settle_viewer(viewer, "alpha")
        await self.settle_agent(alpha, 1)

        await viewer.send_str(json.dumps({"t": "move", "test": "before-switch"}))
        before = await self.wait_json(
            alpha, lambda obj: obj.get("test") == "before-switch"
        )
        self.assertEqual(before["t"], "move")

        await viewer.send_str(json.dumps({"t": "pick", "pc": "beta"}))
        await self.wait_json(viewer, lambda obj: obj.get("t") == "pc" and obj.get("name") == "beta")
        release = await self.wait_json(alpha, lambda obj: obj.get("t") == "release")
        self.assertEqual(release["t"], "release")
        await self.wait_json(
            alpha, lambda obj: obj.get("t") == "viewers" and obj.get("n") == 0
        )
        await self.settle_agent(beta, 1)

        await viewer.send_str(json.dumps({"t": "click", "test": "after-switch"}))
        after = await self.wait_json(
            beta, lambda obj: obj.get("test") == "after-switch"
        )
        self.assertEqual(after["t"], "click")
        await self.assert_no_message(alpha)


class FlowControlTests(RelayTestCase):
    async def test_agent_ack_waits_for_every_viewer_ack(self):
        agent = await self.connect_agent("alpha")
        viewer_one = await self.connect_viewer("alpha")
        await self.settle_viewer(viewer_one, "alpha")
        await self.settle_agent(agent, 1)

        viewer_two = await self.connect_viewer("alpha")
        await self.settle_viewer(viewer_two, "alpha")
        await self.wait_json(
            agent, lambda obj: obj.get("t") == "viewers" and obj.get("n") == 2
        )
        await self.wait_json(agent, lambda obj: obj.get("t") == "kf")

        payload = b"backpressure-frame"
        await agent.send_bytes(make_frame(payload))
        frame_one = await self.receive_binary(viewer_one)
        frame_two = await self.receive_binary(viewer_two)
        self.assertTrue(frame_one.endswith(payload))
        self.assertTrue(frame_two.endswith(payload))

        # Neither nor only one viewer has acknowledged: no relay ack to the agent.
        await self.assert_no_message(agent)
        await viewer_one.send_str(json.dumps({"t": "ack"}))
        await self.assert_no_message(agent)

        await viewer_two.send_str(json.dumps({"t": "ack"}))
        ack = await self.wait_json(agent, lambda obj: obj.get("t") == "ack")
        self.assertEqual(ack, {"t": "ack"})


class DisconnectAndReplacementTests(RelayTestCase):
    async def test_viewer_disconnect_after_key_input_releases_agent_keys(self):
        agent = await self.connect_agent("alpha")
        viewer = await self.connect_viewer("alpha")
        await self.settle_viewer(viewer, "alpha")
        await self.settle_agent(agent, 1)

        key_event = json.dumps({"t": "key", "key": "ShiftLeft", "down": True})
        await viewer.send_str(key_event)
        received = await self.wait_json(
            agent, lambda obj: obj.get("t") == "key"
        )
        self.assertTrue(received["down"])

        await viewer.close()
        release = await self.wait_json(
            agent, lambda obj: obj.get("t") == "release"
        )
        self.assertEqual(release, {"t": "release"})
        await self.wait_json(
            agent, lambda obj: obj.get("t") == "viewers" and obj.get("n") == 0
        )

    async def test_same_name_agent_replaces_old_and_takes_over_routes(self):
        old_agent = await self.connect_agent("alpha")
        viewer = await self.connect_viewer("alpha")
        await self.settle_viewer(viewer, "alpha")
        await self.settle_agent(old_agent, 1)

        new_agent = await self.connect_agent("alpha")
        await self.expect_close(old_agent, 4000)
        await self.wait_json(
            viewer, lambda obj: obj.get("t") == "agent" and obj.get("online") is True
        )
        await self.settle_agent(new_agent, 1)

        await viewer.send_str(json.dumps({"t": "move", "test": "replacement-input"}))
        received = await self.wait_json(
            new_agent, lambda obj: obj.get("test") == "replacement-input"
        )
        self.assertEqual(received["t"], "move")

        await new_agent.send_bytes(make_frame(b"replacement-frame"))
        frame = await self.receive_binary(viewer)
        self.assertTrue(frame.endswith(b"replacement-frame"))


class CachedFrameTests(RelayTestCase):
    async def test_late_viewer_receives_cached_keyframe_and_delta_without_agent_kf(self):
        agent = await self.connect_agent("alpha")

        key_payload = b"cached-keyframe"
        delta_payload = b"cached-delta"
        await agent.send_bytes(make_frame(key_payload, key=True))
        first_ack = await self.wait_json(agent, lambda obj: obj.get("t") == "ack")
        self.assertEqual(first_ack, {"t": "ack"})
        await agent.send_bytes(make_frame(delta_payload, key=False))
        second_ack = await self.wait_json(agent, lambda obj: obj.get("t") == "ack")
        self.assertEqual(second_ack, {"t": "ack"})

        viewer = await self.connect_viewer("alpha")
        replay_key = await self.receive_binary(viewer)
        self.assertTrue(replay_key.endswith(key_payload))
        replay_delta = await self.receive_binary(viewer)
        self.assertTrue(replay_delta.endswith(delta_payload))

        await self.wait_json(
            agent, lambda obj: obj.get("t") == "viewers" and obj.get("n") == 1
        )
        # The replay came from cache, so the relay must not ask the agent for a kf.
        await self.assert_no_message(agent)
        await self.settle_viewer(viewer, "alpha")

        # A decoder failure immediately after replay is within the replay cooldown,
        # so this kf must be forwarded to the real agent.
        await viewer.send_str(json.dumps({"t": "kf"}))
        forwarded_kf = await self.wait_json(
            agent, lambda obj: obj.get("t") == "kf"
        )
        self.assertEqual(forwarded_kf, {"t": "kf"})


if __name__ == "__main__":
    unittest.main()
