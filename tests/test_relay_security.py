"""Safety regressions using temporary token files and local mock connections."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

import test_relay as protocol


relay = protocol.relay


class TokenFileTests(unittest.TestCase):
    def test_empty_token_file_is_rejected_instead_of_disabling_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "relay.token"
            with patch.object(relay, "HERE", Path(directory)):
                for contents in ("", " \t\r\n"):
                    with self.subTest(contents=repr(contents)):
                        token_file.write_text(contents, encoding="utf-8")
                        with self.assertRaises(ValueError):
                            relay.load_token(None)

    def test_blank_explicit_token_is_rejected(self):
        for token in ("", " \t\r\n"):
            with self.subTest(token=repr(token)):
                with self.assertRaises(ValueError):
                    relay.load_token(token)

    def test_generated_token_is_saved_and_reused_from_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(relay, "HERE", Path(directory)):
                generated = relay.load_token(None)
                self.assertTrue(generated)
                self.assertEqual(relay.load_token(None), generated)
                self.assertEqual(
                    (Path(directory) / "relay.token").read_text(encoding="utf-8"),
                    generated,
                )


class SecurityProtocolTests(protocol.RelayTestCase):
    async def test_non_ascii_wrong_token_returns_forbidden_on_every_endpoint(self):
        for endpoint in ("/", "/ws", "/agent"):
            with self.subTest(endpoint=endpoint):
                response = await self.client.get(
                    endpoint + "?token=" + quote("incorrect-\u53e3\u4ee4")
                )
                self.assertEqual(response.status, 403)
                await response.read()

    async def test_empty_configured_token_cannot_authorize_an_empty_request(self):
        client = TestClient(TestServer(protocol.make_app("")))
        await client.start_server()
        try:
            for endpoint in ("/", "/ws", "/agent"):
                with self.subTest(endpoint=endpoint):
                    response = await client.get(endpoint + "?token=")
                    self.assertEqual(response.status, 403)
                    await response.read()
        finally:
            await client.close()

    async def test_malformed_events_leave_authorized_viewer_connected(self):
        viewer = await self.connect_viewer()
        events = ["not json", "[]", "null", "42", '"text"', "true"]
        events.extend(
            json.dumps({"t": "pick", "pc": value})
            for value in (123, {}, [], True)
        )
        for sequence, event in enumerate(events):
            with self.subTest(event=event):
                await viewer.send_str(event)
                await viewer.send_json({"t": "ping", "ts": sequence})
                pong = await self.wait_json(
                    viewer,
                    lambda obj: obj.get("t") == "pong"
                    and obj.get("ts") == sequence,
                )
                self.assertEqual(pong, {"t": "pong", "ts": sequence})

    async def test_viewer_cannot_forge_relay_owned_viewer_counts(self):
        agent = await self.connect_agent("alpha")
        viewer = await self.connect_viewer("alpha")
        await self.settle_viewer(viewer, "alpha")
        await self.settle_agent(agent, 1)

        await viewer.send_json({"t": "viewers", "n": 0})
        input_event = {"t": "move", "x": 12, "y": 34}
        await viewer.send_json(input_event)
        message = await self.receive(agent)
        self.assertEqual(message.type, WSMsgType.TEXT)
        self.assertEqual(json.loads(message.data), input_event)
        await self.assert_no_message(agent)

    async def test_watchdog_releases_keys_and_updates_count_when_viewer_stalls(self):
        with patch.object(relay, "STALL_TIMEOUT", 0):
            agent = await self.connect_agent("alpha")
            viewer = await self.connect_viewer("alpha")
            await self.settle_viewer(viewer, "alpha")
            await self.settle_agent(agent, 1)
            await viewer.send_json({"t": "key", "key": "ShiftLeft", "down": True})
            await self.wait_json(agent, lambda obj: obj.get("t") == "key")

            frame = protocol.make_frame(b"watchdog-frame", key=True)
            await agent.send_bytes(frame)
            self.assertEqual(await self.receive_binary(viewer), frame)
            # Deliberately withhold the frame ACK. The normal watchdog tick must
            # release input and update the agent before closing this viewer.
            await self.wait_json(agent, lambda obj: obj.get("t") == "release")
            await self.wait_json(
                agent,
                lambda obj: obj.get("t") == "viewers" and obj.get("n") == 0,
            )
            closed = await self.receive(viewer)
            self.assertEqual(closed.type, WSMsgType.CLOSE)
            self.assertEqual(closed.data, 4001)
            hub = self.app["reg"].hubs["alpha"]
            self.assertFalse(hub.viewers)
            self.assertFalse(hub.frames)

    async def test_watchdog_prunes_offline_hub_after_last_stalled_viewer_leaves(self):
        with patch.object(relay, "STALL_TIMEOUT", 0):
            agent = await self.connect_agent("alpha")
            viewer = await self.connect_viewer("alpha")
            await self.settle_viewer(viewer, "alpha")
            await self.settle_agent(agent, 1)
            await agent.send_bytes(protocol.make_frame(b"last-frame", key=True))
            await self.receive_binary(viewer)
            await agent.close()
            await self.wait_json(
                viewer,
                lambda obj: obj.get("t") == "agent" and obj.get("online") is False,
            )
            for _ in range(8):
                closed = await self.receive(viewer)
                if closed.type == WSMsgType.CLOSE:
                    break
                self.assertEqual(closed.type, WSMsgType.TEXT)
            else:
                self.fail("stalled viewer was not closed")
            self.assertEqual(closed.data, 4001)
            self.assertNotIn("alpha", self.app["reg"].hubs)


if __name__ == "__main__":
    unittest.main()
