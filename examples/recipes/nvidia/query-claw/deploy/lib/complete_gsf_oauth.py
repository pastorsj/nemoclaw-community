# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Complete Hermes' official GSF MCP OAuth flow with GSF credentials.

The caller supplies the GSF public origin as the only command-line value and
writes the administrator email and password as two newline-terminated fields
on stdin.  Credentials, session cookies, authorization codes, and tokens are
never printed.  Hermes remains responsible for client registration, PKCE,
token exchange, validation, and token persistence.
"""

from __future__ import annotations

import argparse
import hmac
import http.cookiejar
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import IO, Callable
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit


AUTHORIZE_PATH = "/api/auth/mcp/authorize"
LOGIN_PATH = "/login"
SIGN_IN_PATH = "/api/auth/sign-in/email"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
AUTHORIZATION_URL_TIMEOUT = 60.0
HTTP_TIMEOUT = 15.0
COMPLETION_TIMEOUT = 330.0
MAX_URL_LENGTH = 16_384

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_HTTPS_URL = re.compile(r"https://[^\s]+")
_PKCE_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43,128}")


class OAuthLoginError(RuntimeError):
    """The bounded, credential-assisted OAuth flow could not be completed."""


@dataclass(frozen=True)
class AuthorizationRequest:
    """Security-relevant fields from the URL produced by Hermes."""

    url: str
    origin: str
    redirect_uri: str
    state: str


@dataclass(frozen=True)
class HttpResult:
    """Non-sensitive response fields needed to drive the redirect flow."""

    status: int
    location: str | None


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _canonical_origin(value: str, *, label: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OAuthLoginError(f"{label} is not a valid URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise OAuthLoginError(f"{label} must be an HTTPS origin")
    host = parsed.hostname.lower().rstrip(".")
    if not host:
        raise OAuthLoginError(f"{label} has an invalid hostname")
    authority = f"[{host}]" if ":" in host else host
    if port not in {None, 443}:
        authority = f"{authority}:{port}"
    return urlunsplit(("https", authority, "", "", ""))


def _single_parameter(parameters: dict[str, list[str]], name: str) -> str:
    values = parameters.get(name, [])
    if len(values) != 1 or not values[0]:
        raise OAuthLoginError(f"OAuth authorization URL has invalid {name}")
    return values[0]


def _validate_loopback_redirect(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OAuthLoginError("Hermes supplied an invalid OAuth redirect URI") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/callback"
        or parsed.query
        or parsed.fragment
    ):
        raise OAuthLoginError("Hermes supplied an unexpected OAuth redirect URI")
    return value


def parse_authorization_url(value: str, expected_origin: str) -> AuthorizationRequest:
    """Validate the GSF authorize URL before credentials may be transmitted."""

    if len(value) > MAX_URL_LENGTH:
        raise OAuthLoginError("Hermes supplied an oversized OAuth authorization URL")
    origin = _canonical_origin(expected_origin, label="GSF public URL")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise OAuthLoginError(
            "Hermes supplied an invalid OAuth authorization URL"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != AUTHORIZE_PATH
        or parsed.fragment
        or _canonical_origin(
            urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
            label="OAuth authorization URL",
        )
        != origin
    ):
        raise OAuthLoginError(
            "Hermes did not select the expected GSF authorization endpoint"
        )

    parameters = parse_qs(parsed.query, keep_blank_values=True)
    if _single_parameter(parameters, "response_type") != "code":
        raise OAuthLoginError(
            "OAuth authorization URL did not request an authorization code"
        )
    state = _single_parameter(parameters, "state")
    _single_parameter(parameters, "client_id")
    redirect_uri = _validate_loopback_redirect(
        _single_parameter(parameters, "redirect_uri")
    )
    challenge = _single_parameter(parameters, "code_challenge")
    if not _PKCE_CHALLENGE.fullmatch(challenge):
        raise OAuthLoginError("OAuth authorization URL has an invalid PKCE challenge")
    if _single_parameter(parameters, "code_challenge_method") != "S256":
        raise OAuthLoginError("OAuth authorization URL did not require S256 PKCE")
    return AuthorizationRequest(value, origin, redirect_uri, state)


def parse_callback_url(value: str, request: AuthorizationRequest) -> str:
    """Validate a GSF callback before giving it to Hermes' stdin reader."""

    if len(value) > MAX_URL_LENGTH:
        raise OAuthLoginError("GSF returned an oversized OAuth callback")
    try:
        callback = urlsplit(value)
        callback.port
        expected = urlsplit(request.redirect_uri)
    except ValueError as exc:
        raise OAuthLoginError("GSF returned an invalid OAuth callback") from exc
    if (
        callback.scheme != expected.scheme
        or callback.hostname != expected.hostname
        or callback.port != expected.port
        or callback.username is not None
        or callback.password is not None
        or callback.path != expected.path
        or callback.fragment
    ):
        raise OAuthLoginError(
            "GSF returned the OAuth callback to an unexpected endpoint"
        )

    parameters = parse_qs(callback.query, keep_blank_values=True)
    if parameters.get("error"):
        raise OAuthLoginError("GSF rejected the OAuth authorization request")
    _single_parameter(parameters, "code")
    state = _single_parameter(parameters, "state")
    if not hmac.compare_digest(state, request.state):
        raise OAuthLoginError("GSF returned an OAuth callback with the wrong state")
    return value


def _validate_login_redirect(
    location: str | None, request: AuthorizationRequest
) -> str:
    if not location or len(location) > MAX_URL_LENGTH:
        raise OAuthLoginError("GSF did not redirect the authorization request to login")
    value = urljoin(f"{request.origin}/", location)
    parsed = urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path != LOGIN_PATH
        or parsed.fragment
        or _canonical_origin(
            urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
            label="GSF login redirect",
        )
        != request.origin
    ):
        raise OAuthLoginError("GSF returned an unexpected login redirect")
    return value


def _callback_from_location(
    location: str | None,
    request: AuthorizationRequest,
    *,
    base_url: str,
) -> str | None:
    if not location:
        return None
    if len(location) > MAX_URL_LENGTH:
        raise OAuthLoginError("GSF returned an oversized redirect")
    try:
        value = urljoin(base_url, location)
        parsed = urlsplit(value)
        parsed.port
        expected = urlsplit(request.redirect_uri)
    except ValueError as exc:
        raise OAuthLoginError("GSF returned an invalid redirect") from exc
    if (
        parsed.scheme == expected.scheme
        and parsed.hostname == expected.hostname
        and parsed.port == expected.port
        and parsed.path == expected.path
    ):
        return parse_callback_url(value, request)

    response_origin = _canonical_origin(
        urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
        label="GSF sign-in redirect",
    )
    if response_origin != request.origin:
        raise OAuthLoginError("GSF sign-in redirected away from the expected origin")
    return None


def _new_opener() -> urllib.request.OpenerDirector:
    # Preserve OpenShell's injected proxy: it is the enforced path to the
    # allowlisted private GSF origin. URL validation above keeps every
    # credential-bearing request pinned to that exact HTTPS origin.
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        _RejectRedirects(),
    )


def _request(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    *,
    timeout: float,
) -> HttpResult:
    response = None
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        status = int(response.getcode())
        location = response.headers.get("Location")
        return HttpResult(status=status, location=location)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise OAuthLoginError("GSF OAuth endpoint could not be reached") from exc
    finally:
        if response is not None:
            response.close()


def credential_callback(
    request: AuthorizationRequest,
    email: str,
    password: str,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> str:
    """Sign in through Better Auth and return its validated loopback callback."""

    client = opener or _new_opener()
    authorize = urllib.request.Request(
        request.url,
        headers={"Accept": "text/html,application/xhtml+xml"},
        method="GET",
    )
    first = _request(client, authorize, timeout=timeout)
    if first.status not in REDIRECT_STATUSES:
        raise OAuthLoginError("GSF did not start the credential login flow")
    login_url = _validate_login_redirect(first.location, request)

    body = json.dumps(
        {"email": email, "password": password}, separators=(",", ":")
    ).encode("utf-8")
    sign_in_url = f"{request.origin}{SIGN_IN_PATH}"
    sign_in = urllib.request.Request(
        sign_in_url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": request.origin,
            "Referer": login_url,
        },
        method="POST",
    )
    signed_in = _request(client, sign_in, timeout=timeout)
    if signed_in.status in REDIRECT_STATUSES:
        callback = _callback_from_location(
            signed_in.location, request, base_url=sign_in_url
        )
        if callback is not None:
            return callback
    elif not 200 <= signed_in.status < 300:
        raise OAuthLoginError("GSF credential sign-in was rejected")

    # Better Auth can return a JSON success response after setting the session
    # cookie. Re-entering the authorize endpoint lets the MCP plugin consume
    # the signed login-prompt cookie without exposing the JSON session token.
    resumed = _request(client, authorize, timeout=timeout)
    if resumed.status not in REDIRECT_STATUSES:
        raise OAuthLoginError("GSF did not resume OAuth after credential sign-in")
    callback = _callback_from_location(resumed.location, request, base_url=request.url)
    if callback is None:
        raise OAuthLoginError("GSF credential sign-in did not establish a session")
    return callback


def _read_credentials(stream: IO[str]) -> tuple[str, str]:
    def read_field(label: str) -> str:
        value = stream.readline()
        if not value:
            raise OAuthLoginError(f"missing {label} on stdin")
        if value.endswith("\n"):
            value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
        if not value or "\x00" in value:
            raise OAuthLoginError(f"invalid {label} on stdin")
        return value

    email = read_field("GSF administrator email")
    password = read_field("GSF administrator password")
    if stream.readline():
        raise OAuthLoginError("unexpected data after GSF credentials on stdin")
    return email, password


def _output_reader(stream: IO[str], messages: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            messages.put(line)
    finally:
        messages.put(None)


def _authorization_candidate(
    line: str, expected_origin: str
) -> AuthorizationRequest | None:
    clean = _ANSI_ESCAPE.sub("", line)
    for match in _HTTPS_URL.finditer(clean):
        candidate = match.group(0).rstrip("'\".,;)>]")
        try:
            path = urlsplit(candidate).path
        except ValueError:
            continue
        if path == AUTHORIZE_PATH:
            return parse_authorization_url(candidate, expected_origin)
    return None


def _next_message(messages: queue.Queue[str | None], deadline: float) -> str | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OAuthLoginError("Hermes OAuth login timed out")
    try:
        return messages.get(timeout=remaining)
    except queue.Empty as exc:
        raise OAuthLoginError("Hermes OAuth login timed out") from exc


def _stop_process(process: subprocess.Popen[str]) -> None:
    try:
        if process.stdin is not None:
            process.stdin.close()
    except OSError:
        pass
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def complete_login(
    expected_origin: str,
    email: str,
    password: str,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    authorization_timeout: float = AUTHORIZATION_URL_TIMEOUT,
    completion_timeout: float = COMPLETION_TIMEOUT,
) -> None:
    """Drive ``hermes mcp login gsf`` and feed its callback through stdin."""

    origin = _canonical_origin(expected_origin, label="GSF public URL")
    child_environment = os.environ.copy()
    for name in (
        "GSF_ADMIN_EMAIL",
        "GSF_ADMIN_PASSWORD",
        "DISPLAY",
        "WAYLAND_DISPLAY",
    ):
        child_environment.pop(name, None)
    # If Hermes ever runs where a display is present, this prevents the Python
    # webbrowser module from starting an interactive browser.
    child_environment["BROWSER"] = "/bin/false"
    process: subprocess.Popen[str] | None = None
    try:
        process = popen(
            ["hermes", "mcp", "login", "gsf"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_environment,
        )
        if process.stdin is None or process.stdout is None:
            raise OAuthLoginError("Hermes OAuth subprocess streams are unavailable")

        messages: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(
            target=_output_reader, args=(process.stdout, messages), daemon=True
        )
        reader.start()

        authorization_deadline = time.monotonic() + authorization_timeout
        authorization: AuthorizationRequest | None = None
        while authorization is None:
            line = _next_message(messages, authorization_deadline)
            if line is None:
                raise OAuthLoginError(
                    "Hermes exited before presenting a GSF authorization URL"
                )
            authorization = _authorization_candidate(line, origin)

        callback = credential_callback(authorization, email, password, opener=opener)
        process.stdin.write(f"{callback}\n")
        process.stdin.flush()
        process.stdin.close()

        authenticated = False
        completion_deadline = time.monotonic() + completion_timeout
        while True:
            line = _next_message(messages, completion_deadline)
            if line is None:
                break
            clean = _ANSI_ESCAPE.sub("", line)
            if re.search(r"(?:^|\s)Authenticated(?:\s|\(|\N{EM DASH})", clean):
                authenticated = True

        remaining = max(0.0, completion_deadline - time.monotonic())
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise OAuthLoginError("Hermes OAuth login timed out") from exc
        if status != 0 or not authenticated:
            raise OAuthLoginError("Hermes did not confirm GSF OAuth authentication")
    except OSError as exc:
        raise OAuthLoginError("Hermes OAuth login could not be started") from exc
    finally:
        if process is not None:
            _stop_process(process)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Complete Hermes' GSF MCP OAuth login without a browser."
    )
    parser.add_argument(
        "--gsf-origin",
        required=True,
        help="expected public HTTPS origin of this Query Claw GSF deployment",
    )
    return parser


def main(argv: list[str]) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        email, password = _read_credentials(sys.stdin)
        complete_login(args.gsf_origin, email, password)
    except OAuthLoginError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        # Drop the last direct references promptly. Python cannot promise a
        # memory wipe, but neither value is passed through argv or child env.
        email = ""
        password = ""
    print("GSF MCP OAuth login completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
