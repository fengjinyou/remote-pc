"""Test the actual log formatter without importing Windows capture/input code."""

import ast
import logging
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

from aiohttp import RequestInfo, WSServerHandshakeError
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL


def load_formatter():
    source_path = Path(__file__).resolve().parents[1] / "agent.py"
    source = ast.parse(source_path.read_text(encoding="utf-8"), str(source_path))
    classes = [
        node
        for node in source.body
        if isinstance(node, ast.ClassDef) and node.name == "TokenRedactingFormatter"
    ]
    if len(classes) != 1:
        raise AssertionError("agent.py must define one TokenRedactingFormatter")
    namespace = {"logging": logging, "re": re, "quote": quote}
    # Execute only the production formatter class. Importing agent.py would run
    # DPI setup and import the Windows capture/input libraries during discovery.
    isolated = ast.Module(body=classes, type_ignores=[])
    exec(compile(isolated, str(source_path), "exec"), namespace)
    return namespace["TokenRedactingFormatter"]


Formatter = load_formatter()
TOKENS = ("safe_test_123-Token", "space \u4e2d\u6587/slash", "\u5ba1\u67e5+/ ?&=%")


class AgentLoggingTests(unittest.TestCase):
    def assert_redacted(self, formatted, token):
        self.assertNotIn(token, formatted)
        self.assertNotIn(quote(token, safe=""), formatted)
        self.assertIn("token=[redacted]", formatted)
        self.assertIn("name=test", formatted)

    def test_message_redacts_raw_encoded_and_url_token_values(self):
        for token in TOKENS:
            with self.subTest(token=token):
                encoded = quote(token, safe="")
                message = (
                    f"raw credential: {token}; encoded credential: {encoded}; "
                    f"URL: wss://relay.invalid/agent?token={encoded}&name=test"
                )
                record = logging.LogRecord(
                    "agent", logging.ERROR, __file__, 1, message, (), None
                )
                self.assert_redacted(Formatter(token).format(record), token)

    def test_aiohttp_handshake_error_and_traceback_do_not_leak_token(self):
        for token in TOKENS:
            with self.subTest(token=token):
                url = URL(
                    "wss://relay.invalid/agent?token="
                    + quote(token, safe="")
                    + "&name=test"
                )
                request = RequestInfo(
                    url,
                    "GET",
                    CIMultiDictProxy(CIMultiDict()),
                    real_url=url,
                )
                try:
                    raise WSServerHandshakeError(
                        request, (), status=403, message="Forbidden"
                    )
                except WSServerHandshakeError as error:
                    record = logging.LogRecord(
                        "agent",
                        logging.ERROR,
                        __file__,
                        1,
                        "connection failed: %s",
                        (error,),
                        sys.exc_info(),
                    )
                    formatted = Formatter(token).format(record)
                self.assert_redacted(formatted, token)
                self.assertIn("Traceback", formatted)
                self.assertIn("403", formatted)
                self.assertIn("Forbidden", formatted)


if __name__ == "__main__":
    unittest.main()
