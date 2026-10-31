"""Guard registration preserves published schemas and rejects stale tool output."""

import inspect
import unittest
from types import SimpleNamespace

from qlik_sense_mcp_server.worker_tools import install_tool_guard


class ToolGuardTests(unittest.TestCase):
    def test_sync_tool_body_is_guarded_without_exposing_auth_arguments(self):
        seen = []
        helpers = SimpleNamespace(_bounded=lambda function: function,
            _err=lambda message, **kwargs: {"error": message, **kwargs})
        class Guard:
            def execute(self, function, *args, **kwargs):
                seen.append(function.__name__)
                return function(*args, **kwargs)
        install_tool_guard(helpers, Guard())
        def query(app_id: str, limit: int = 10) -> str:
            return app_id
        wrapped = helpers._bounded(query)
        self.assertEqual(inspect.signature(wrapped), inspect.signature(query))
        self.assertEqual(wrapped("app"), "app")
        self.assertEqual(seen, ["query"])
        self.assertFalse(wrapped.__engine_serialised__)

    def test_guard_errors_are_sanitized(self):
        helpers = SimpleNamespace(_bounded=lambda function: function,
            _err=lambda message, **kwargs: {"error": message, **kwargs})
        class Guard:
            def execute(self, function, *args, **kwargs):
                raise RuntimeError("secret cookie body")
        install_tool_guard(helpers, Guard())
        result = helpers._bounded(lambda: None)()
        self.assertNotIn("secret", str(result))
        self.assertEqual(result["error_category"], "authentication_or_session")


if __name__ == "__main__": unittest.main()
