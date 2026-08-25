"""
WS_ORIGIN: what a WebSocket upgrade must check, and why each case is in the list.

The origin check closes a same-origin attack vector when a listener is bound
to 0.0.0.0 or some other non-loopback address without a token. A browser
connecting from a malicious page will send an Origin header; non-browser
clients (scripts, curl, the desktop panel) send none and must not be blocked.

This test pins the list of cases the check must reject, so that someone
"hardening" it later (by rejecting absent Origin, for instance) will have
to argue with these tests.
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wsserver as wsserver_mod


class FakeWriter:
    """Fake asyncio StreamWriter for testing."""

    def __init__(self):
        self.data = b""

    def write(self, data):
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.data += data

    async def drain(self):
        pass

    def get_extra_info(self, name):
        if name == "peername":
            return ("127.0.0.1", 54321)
        return None


class TheOriginCheck(unittest.TestCase):
    """Tests for WebSocket origin header validation."""

    def setUp(self):
        self.server = wsserver_mod.WSServer(
            "127.0.0.1", 8770, "web", token=""
        )

    async def _call_upgrade(self, headers):
        """Call _upgrade with a fake writer and return (result, written_data)."""
        reader = None  # Not used by _upgrade in these cases
        writer = FakeWriter()
        result = await self.server._upgrade(
            reader, writer, headers, "127.0.0.1:54321")
        return result, writer.data

    def test_a_same_origin_browser_handshake_is_accepted(self):
        """Same origin (127.0.0.1:8770) must be allowed."""
        headers = {
            "host": "127.0.0.1:8770",
            "origin": "http://127.0.0.1:8770",
            "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="
        }
        result, written = asyncio.run(self._call_upgrade(headers))

        self.assertIsNotNone(
            result, "same-origin handshake should not return None")
        self.assertNotIn(
            b"403", written,
            "same-origin handshake should not send 403")

    def test_a_cross_origin_handshake_is_refused_with_403(self):
        """Cross origin (evil.example vs 127.0.0.1) must be rejected."""
        headers = {
            "host": "127.0.0.1:8770",
            "origin": "http://evil.example:8770",
            "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="
        }
        result, written = asyncio.run(self._call_upgrade(headers))

        self.assertIsNone(result, "cross-origin handshake should return None")
        self.assertIn(b"403", written,
                      "cross-origin handshake should send 403")
        self.assertIn(b"origin mismatch", written,
                      "403 should mention origin mismatch")

    def test_an_absent_origin_is_accepted(self):
        """Absent Origin must be allowed (non-browser clients like curl, scripts, desktop panel)."""
        # Non-browser clients do not send an Origin header. This is deliberate
        # and must not be "hardened" to reject them, because that would break
        # the desktop panel, command-line tools, and any script-based client.
        headers = {
            "host": "127.0.0.1:8770",
            "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="
        }
        result, written = asyncio.run(self._call_upgrade(headers))

        self.assertIsNotNone(
            result, "absent origin should not return None")
        self.assertNotIn(b"403", written,
                         "absent origin should not send 403")

    def test_a_port_mismatch_on_the_same_host_is_refused(self):
        """Port mismatch (9999 vs 8770 on same host) must be rejected."""
        headers = {
            "host": "127.0.0.1:8770",
            "origin": "http://127.0.0.1:9999",
            "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="
        }
        result, written = asyncio.run(self._call_upgrade(headers))

        self.assertIsNone(result, "port mismatch should return None")
        self.assertIn(b"403", written,
                      "port mismatch should send 403")

    def test_a_malformed_origin_is_refused(self):
        """Malformed Origin (not a valid URL) must be rejected."""
        headers = {
            "host": "127.0.0.1:8770",
            "origin": "not a valid origin url at all",
            "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="
        }
        result, written = asyncio.run(self._call_upgrade(headers))

        self.assertIsNone(result, "malformed origin should return None")
        self.assertIn(b"403", written,
                      "malformed origin should send 403")


if __name__ == "__main__":
    unittest.main()
