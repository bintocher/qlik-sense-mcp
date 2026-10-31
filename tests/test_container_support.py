"""Container gateway binding and credential-free healthcheck regressions."""

import os
import unittest
from io import BytesIO
from types import ModuleType
from unittest.mock import patch
from urllib.error import HTTPError

from qlik_sense_mcp_server import gateway_http, container_health


class BindingTests(unittest.TestCase):
    def test_default_remains_loopback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gateway_http.gateway_binding(), ("127.0.0.1",8000))

    def test_container_can_bind_all_interfaces(self):
        with patch.dict(os.environ, {"MCP_HOST":"0.0.0.0", "MCP_PORT":"8000"}):
            self.assertEqual(gateway_http.gateway_binding(), ("0.0.0.0",8000))

    def test_invalid_bind_configuration_is_rejected(self):
        for host, port in (("", "8000"), ("http://host", "8000"), ("0.0.0.0", "0"), ("0.0.0.0", "65536")):
            with patch.dict(os.environ, {"MCP_HOST":host,"MCP_PORT":port}), self.assertRaises(ValueError):
                gateway_http.gateway_binding()

    def test_public_bind_does_not_enable_trust_in_proxy_headers(self):
        import sys
        calls = []
        module = ModuleType("uvicorn")
        module.run = lambda app, **kwargs: calls.append(kwargs)
        with patch.dict(sys.modules, {"uvicorn":module}), patch.dict(os.environ,{"MCP_HOST":"0.0.0.0","MCP_PORT":"8000"}):
            gateway_http.serve_gateway(object())
        self.assertFalse(calls[0]["proxy_headers"])
        self.assertEqual(calls[0]["host"], "0.0.0.0")


class HealthTests(unittest.TestCase):
    def test_healthcheck_accepts_only_expected_unauthenticated_rejection(self):
        for status, expected in ((401,0),(403,1),(500,1)):
            class Opener:
                def open(self, request, timeout):
                    self_request = request
                    if request.get_header("Authorization") is not None:
                        raise AssertionError("Healthcheck must not carry credentials")
                    raise HTTPError(request.full_url,status,"probe",{},BytesIO())
            with patch.object(container_health,"build_opener",lambda *args:Opener()), patch.dict(os.environ,{"MCP_PORT":"8000"}):
                self.assertEqual(container_health.main(),expected)


if __name__ == "__main__": unittest.main()
