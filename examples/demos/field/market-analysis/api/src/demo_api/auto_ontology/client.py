# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client for the two Auto Ontology calls the API makes: list databases and export a model.

The agent asks Auto Ontology questions through its MCP server; the API only reads the ontology
for the data viewer. Auto Ontology signs users in with a password session (Better Auth), so the
client signs in on entry and out on exit.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from .models import DatabaseList
from .models import ModelDocument
from .models import OntologySnapshot
from .snapshot import project_snapshot

_RETRYABLE = frozenset({429, 500, 502, 503, 504})
MAX_RESPONSE_BYTES = 5_000_000


class AutoOntologyError(Exception):
    """A failure the route maps to an HTTP status; the message is safe to show."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class AutoOntologyClient:
    def __init__(
        self,
        base_url: str,
        *,
        email: str,
        password: str,
        origin: str = "",
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._max_retries = max_retries
        # Better Auth checks the Origin of sign-in and sign-out requests against its trusted origins.
        origin = (origin or self._base_url).rstrip("/")
        self._origin = {"Origin": origin, "Referer": f"{origin}/"}
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), transport=transport)

    async def __aenter__(self) -> AutoOntologyClient:
        try:
            if self._email:
                response = await self._send(
                    "POST", "api/auth/sign-in/email", {"email": self._email, "password": self._password}
                )
                if response.status_code in {401, 403}:
                    raise AutoOntologyError("Auto Ontology rejected the API's sign-in", status_code=503)
                self._check(response)
        except BaseException:
            await self._http.aclose()
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        try:
            if self._email:
                await self._http.post(f"{self._base_url}/api/auth/sign-out", json={}, headers=self._origin)
        except httpx.HTTPError:
            pass  # the session expires on its own
        finally:
            await self._http.aclose()

    async def list_databases(self) -> DatabaseList:
        response = await self._send("GET", "api/datasources/dbs")
        try:
            return DatabaseList.model_validate_json(self._body(response))
        except ValidationError as error:
            raise AutoOntologyError("Auto Ontology returned an invalid database list") from error

    async def export_model(self, database_id: str) -> ModelDocument:
        """Export one database's model; never "export all"."""
        body = {"databases": [database_id], "format": "auto_ontology"}
        response = await self._send("POST", "api/model/export", body, accept="application/x-yaml")
        try:
            document = yaml.safe_load(self._body(response))
            return ModelDocument.model_validate(document if isinstance(document, dict) else {"invalid": True})
        except (yaml.YAMLError, ValidationError) as error:
            raise AutoOntologyError("Auto Ontology returned an invalid model export") from error

    async def ontology_snapshot(self, *, source_id: str, database_name: str) -> OntologySnapshot:
        """The ontology of the database registered under ``database_name``."""
        databases = [
            db for db in (await self.list_databases()).data if (db.name or "").casefold() == database_name.casefold()
        ]
        if len(databases) != 1:
            raise AutoOntologyError(f"Auto Ontology has no single database named {database_name}", status_code=404)
        try:
            return project_snapshot(
                await self.export_model(databases[0].id), source_id=source_id, database_name=database_name
            )
        except ValueError as error:
            raise AutoOntologyError("Auto Ontology returned an unexpected model export") from error

    async def _send(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, accept: str = "application/json"
    ) -> httpx.Response:
        headers = {"Accept": accept, **self._origin}
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.request(method, f"{self._base_url}/{path}", json=body, headers=headers)
            except httpx.TimeoutException as error:
                raise AutoOntologyError("Auto Ontology timed out", status_code=504) from error
            except httpx.TransportError as error:
                raise AutoOntologyError("Auto Ontology is unreachable", status_code=503) from error
            if response.status_code not in _RETRYABLE or attempt == self._max_retries:
                return response
            await asyncio.sleep(min(2**attempt, 10))
        raise AssertionError("unreachable")

    def _body(self, response: httpx.Response) -> bytes:
        self._check(response)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise AutoOntologyError("Auto Ontology's response is too large")
        return response.content

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code == 404:
            raise AutoOntologyError("Auto Ontology does not offer this capability", status_code=503)
        if response.status_code >= 400:
            raise AutoOntologyError(f"Auto Ontology answered HTTP {response.status_code}")
