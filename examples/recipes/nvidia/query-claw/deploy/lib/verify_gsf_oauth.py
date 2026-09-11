# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify the public GSF authorization server and private MCP OAuth contract."""

from __future__ import annotations

import json
import ipaddress
import re
import sys
import urllib.error
import urllib.request
from email.parser import Parser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class ContractError(ValueError):
    """An OAuth endpoint does not match the deployment contract."""


def _origin(value: str, *, label: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ContractError(f"{label} is not a valid URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError(f"{label} must be an HTTPS URL without credentials")
    host = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in host.split(".")
        ):
            raise ContractError(f"{label} has an invalid hostname")
    else:
        if address.is_unspecified:
            raise ContractError(f"{label} cannot use an unspecified address")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return urlunsplit(("https", authority, "", "", ""))


def validate_authorization_server(document: object, expected_origin: str) -> None:
    """Require the four OAuth endpoints GSF's official MCP client needs."""

    if not isinstance(document, dict):
        raise ContractError("authorization-server metadata must be a JSON object")
    expected_parts = urlsplit(expected_origin)
    if expected_parts.path not in {"", "/"}:
        raise ContractError("GSF_PUBLIC_URL must be an origin")
    expected = _origin(expected_origin, label="GSF_PUBLIC_URL")
    for field in (
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "registration_endpoint",
    ):
        value = document.get(field)
        if not isinstance(value, str) or not value:
            raise ContractError(f"authorization-server metadata is missing {field}")
        if _origin(value, label=field) != expected:
            raise ContractError(f"{field} is not on GSF_PUBLIC_URL")
    if document["issuer"] != expected:
        raise ContractError("issuer must exactly match GSF_PUBLIC_URL")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def probe_authorization_server(expected_origin: str) -> None:
    """Fetch public metadata without credentials or redirect acceptance."""

    origin = _origin(expected_origin, label="GSF_PUBLIC_URL")
    url = f"{origin}/.well-known/oauth-authorization-server"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        response = urllib.request.build_opener(_RejectRedirects).open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        raise ContractError(f"authorization-server metadata returned HTTP {exc.code}") from exc
    except OSError as exc:
        raise ContractError("authorization-server metadata could not be reached") from exc
    with response:
        if response.status != 200:
            raise ContractError(
                f"authorization-server metadata returned HTTP {response.status}"
            )
        if response.headers.get_content_type() != "application/json":
            raise ContractError("authorization-server metadata is not JSON")
        try:
            document = json.load(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("authorization-server metadata is invalid JSON") from exc
    validate_authorization_server(document, origin)


def _response_headers(path: str) -> tuple[int, object]:
    """Return the final response block written by curl --dump-header."""

    raw = Path(path).read_text(encoding="iso-8859-1")
    blocks = [block for block in re.split(r"\r?\n\r?\n", raw) if block.startswith("HTTP/")]
    if not blocks:
        raise ContractError("curl response headers are missing")
    lines = blocks[-1].replace("\r\n", "\n").splitlines()
    match = re.fullmatch(r"HTTP/\S+\s+(\d{3})(?:\s+.*)?", lines[0])
    if not match:
        raise ContractError("curl response status is invalid")
    return int(match.group(1)), Parser().parsestr("\n".join(lines[1:]))


def expected_resource_metadata_url(mcp_url: str) -> str:
    parsed = urlsplit(mcp_url)
    origin = _origin(mcp_url, label="GSF MCP URL")
    if parsed.path != "/mcp" or parsed.query or parsed.fragment:
        raise ContractError("GSF MCP URL must end in /mcp")
    return f"{origin}/.well-known/oauth-protected-resource/mcp"


def challenge_resource_metadata(headers_path: str, mcp_url: str) -> str:
    """Validate the 401 Bearer challenge and return its metadata URL."""

    status, headers = _response_headers(headers_path)
    if status != 401:
        raise ContractError(f"GSF MCP challenge returned HTTP {status}")
    challenge = headers.get("WWW-Authenticate", "")
    match = re.fullmatch(
        r'Bearer\s+resource_metadata="([^"]+)"', challenge.strip(), re.IGNORECASE
    )
    if not match:
        raise ContractError("GSF MCP did not return its Bearer resource_metadata challenge")
    expected = expected_resource_metadata_url(mcp_url)
    if match.group(1) != expected:
        raise ContractError("GSF MCP advertised the wrong protected-resource metadata URL")
    return expected


def validate_protected_resource(
    headers_path: str,
    body_path: str,
    mcp_url: str,
    authorization_server: str,
) -> None:
    """Validate the protected-resource document reached from the challenge."""

    status, headers = _response_headers(headers_path)
    if status != 200:
        raise ContractError(f"protected-resource metadata returned HTTP {status}")
    if headers.get_content_type() != "application/json":
        raise ContractError("protected-resource metadata is not JSON")
    try:
        document = json.loads(Path(body_path).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("protected-resource metadata is invalid JSON") from exc
    expected_server = _origin(authorization_server, label="GSF_PUBLIC_URL")
    if not isinstance(document, dict) or document.get("resource") != mcp_url:
        raise ContractError("protected-resource metadata advertises the wrong resource")
    if document.get("authorization_servers") != [expected_server]:
        raise ContractError("protected-resource metadata advertises the wrong GSF server")


def _load_document(path: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("metadata fixture is invalid JSON") from exc


def main(argv: list[str]) -> int:
    try:
        command, *args = argv
        if command == "authorization-server" and len(args) == 1:
            probe_authorization_server(args[0])
        elif command == "authorization-server-document" and len(args) == 2:
            validate_authorization_server(_load_document(args[1]), args[0])
        elif command == "challenge-resource-metadata" and len(args) == 2:
            print(challenge_resource_metadata(args[0], args[1]))
        elif command == "protected-resource-document" and len(args) == 4:
            validate_protected_resource(args[0], args[1], args[2], args[3])
        else:
            raise ContractError("invalid OAuth probe command")
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
