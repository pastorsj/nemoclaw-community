# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]


def _load_helper():
    path = EXAMPLE_ROOT / "deploy" / "lib" / "complete_gsf_oauth.py"
    spec = importlib.util.spec_from_file_location("complete_gsf_oauth", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OAUTH = _load_helper()
ORIGIN = "https://query-claw.example.test"
REDIRECT_URI = "http://127.0.0.1:39871/callback"
STATE = "state-value-that-must-match"
CHALLENGE = "A" * 43


def authorization_url(*, origin: str = ORIGIN, state: str = STATE) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": "dynamic-client-id",
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "state": state,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        }
    )
    return f"{origin}/api/auth/mcp/authorize?{query}"


def callback_url(*, state: str = STATE) -> str:
    return f"{REDIRECT_URI}?code=authorization-code&state={state}"


class FakeResponse:
    def __init__(self, status: int, location: str | None = None):
        self.status = status
        self.headers = {"Location": location} if location is not None else {}
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class ScriptedOpener:
    def __init__(self, *responses: FakeResponse):
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, *, timeout: float):
        del timeout
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


class RecordingInput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.written = ""

    def write(self, value: str) -> int:
        self.written += value
        return super().write(value)


class FakeProcess:
    def __init__(self, authorization: str):
        self.stdin = RecordingInput()
        self.stdout = io.StringIO(
            "MCP OAuth: authorization required.\n"
            f"    {authorization}\n"
            "Or paste the redirect URL here.\n"
            "  \N{CHECK MARK} Authenticated \N{EM DASH} 7 tool(s) available\n"
        )
        self.returncode: int | None = 0
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        del timeout
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class AuthorizationParsingTests(unittest.TestCase):
    def test_accepts_pinned_hermes_pkce_authorization_url(self) -> None:
        parsed = OAUTH.parse_authorization_url(authorization_url(), ORIGIN)
        self.assertEqual(ORIGIN, parsed.origin)
        self.assertEqual(REDIRECT_URI, parsed.redirect_uri)
        self.assertEqual(STATE, parsed.state)

    def test_rejects_authorization_endpoint_on_another_origin(self) -> None:
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "expected GSF"):
            OAUTH.parse_authorization_url(
                authorization_url(origin="https://attacker.example"), ORIGIN
            )

    def test_rejects_non_s256_authorization_request(self) -> None:
        value = authorization_url().replace("S256", "plain")
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "S256"):
            OAUTH.parse_authorization_url(value, ORIGIN)

    def test_rejects_non_loopback_redirect_uri(self) -> None:
        value = authorization_url().replace(
            urllib.parse.quote(REDIRECT_URI, safe=""),
            urllib.parse.quote("https://attacker.example/callback", safe=""),
        )
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "redirect URI"):
            OAUTH.parse_authorization_url(value, ORIGIN)


class CallbackParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = OAUTH.parse_authorization_url(authorization_url(), ORIGIN)

    def test_accepts_matching_loopback_callback(self) -> None:
        self.assertEqual(
            callback_url(), OAUTH.parse_callback_url(callback_url(), self.request)
        )

    def test_rejects_callback_state_mismatch(self) -> None:
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "wrong state"):
            OAUTH.parse_callback_url(callback_url(state="wrong-state"), self.request)

    def test_rejects_callback_on_a_different_port(self) -> None:
        value = callback_url().replace(":39871/", ":39872/")
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "unexpected endpoint"):
            OAUTH.parse_callback_url(value, self.request)

    def test_rejects_duplicate_code(self) -> None:
        value = f"{callback_url()}&code=second-code"
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "invalid code"):
            OAUTH.parse_callback_url(value, self.request)


class CredentialFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = OAUTH.parse_authorization_url(authorization_url(), ORIGIN)

    def test_credential_sign_in_resumes_authorize_with_same_cookie_client(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(302, f"{ORIGIN}/login?oauth=request"),
            FakeResponse(200),
            FakeResponse(302, callback_url()),
        )
        result = OAUTH.credential_callback(
            self.request, "admin@example.test", "password-canary", opener=opener
        )

        self.assertEqual(callback_url(), result)
        self.assertEqual(
            ["GET", "POST", "GET"],
            [request.get_method() for request in opener.requests],
        )
        sign_in = opener.requests[1]
        self.assertEqual(f"{ORIGIN}/api/auth/sign-in/email", sign_in.full_url)
        self.assertEqual(
            {"email": "admin@example.test", "password": "password-canary"},
            json.loads(sign_in.data),
        )
        self.assertEqual(ORIGIN, sign_in.get_header("Origin"))

    def test_after_hook_redirect_can_return_callback_from_sign_in(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(302, "/login?oauth=request"),
            FakeResponse(302, callback_url()),
        )
        result = OAUTH.credential_callback(
            self.request, "admin@example.test", "password-canary", opener=opener
        )
        self.assertEqual(callback_url(), result)
        self.assertEqual(2, len(opener.requests))

    def test_login_redirect_must_stay_on_gsf_origin(self) -> None:
        opener = ScriptedOpener(FakeResponse(302, "https://attacker.example/login"))
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "unexpected login"):
            OAUTH.credential_callback(
                self.request, "admin@example.test", "password-canary", opener=opener
            )

    def test_malformed_callback_location_is_a_bounded_failure(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(302, "/login?oauth=request"),
            FakeResponse(302, "http://127.0.0.1:not-a-port/callback"),
        )
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "invalid redirect"):
            OAUTH.credential_callback(
                self.request, "admin@example.test", "password-canary", opener=opener
            )

    def test_default_opener_preserves_the_openshell_proxy(self) -> None:
        proxy_url = "http://127.0.0.1:3128"
        with patch.object(
            urllib.request,
            "getproxies",
            return_value={"https": proxy_url},
        ):
            opener = OAUTH._new_opener()
        proxy_handlers = [
            handler
            for handler in opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler)
        ]
        self.assertEqual(1, len(proxy_handlers))
        self.assertEqual(proxy_url, proxy_handlers[0].proxies["https"])


class HermesDriverTests(unittest.TestCase):
    def test_runs_fixed_login_and_feeds_only_validated_callback(self) -> None:
        process = FakeProcess(authorization_url())
        invocation: dict[str, object] = {}

        def popen(arguments, **kwargs):
            invocation["arguments"] = arguments
            invocation["kwargs"] = kwargs
            return process

        opener = ScriptedOpener(
            FakeResponse(302, "/login?oauth=request"),
            FakeResponse(302, callback_url()),
        )
        with patch.dict(
            os.environ,
            {
                "GSF_ADMIN_EMAIL": "email-canary",
                "GSF_ADMIN_PASSWORD": "password-canary",
                "DISPLAY": ":99",
            },
        ):
            OAUTH.complete_login(
                ORIGIN,
                "admin@example.test",
                "password-canary",
                opener=opener,
                popen=popen,
                authorization_timeout=1,
                completion_timeout=1,
            )

        self.assertEqual(
            ["hermes", "mcp", "login", "gsf"], invocation["arguments"]
        )
        child_env = invocation["kwargs"]["env"]
        self.assertNotIn("GSF_ADMIN_EMAIL", child_env)
        self.assertNotIn("GSF_ADMIN_PASSWORD", child_env)
        self.assertNotIn("DISPLAY", child_env)
        self.assertEqual("/bin/false", child_env["BROWSER"])
        self.assertEqual(f"{callback_url()}\n", process.stdin.written)

    def test_credentials_are_read_only_from_two_stdin_fields(self) -> None:
        self.assertEqual(
            ("admin@example.test", "password-canary"),
            OAUTH._read_credentials(
                io.StringIO("admin@example.test\npassword-canary\n")
            ),
        )
        with self.assertRaisesRegex(OAUTH.OAuthLoginError, "unexpected data"):
            OAUTH._read_credentials(
                io.StringIO("admin@example.test\npassword-canary\nextra\n")
            )

    def test_main_does_not_print_credentials_on_failure(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(sys, "stdin", io.StringIO("email-canary\npassword-canary\n")),
            patch.object(sys, "stderr", stderr),
            patch.object(
                OAUTH,
                "complete_login",
                side_effect=OAUTH.OAuthLoginError("bounded failure"),
            ),
        ):
            self.assertEqual(1, OAUTH.main(["--gsf-origin", ORIGIN]))
        output = stderr.getvalue()
        self.assertIn("bounded failure", output)
        self.assertNotIn("email-canary", output)
        self.assertNotIn("password-canary", output)


if __name__ == "__main__":
    unittest.main()
