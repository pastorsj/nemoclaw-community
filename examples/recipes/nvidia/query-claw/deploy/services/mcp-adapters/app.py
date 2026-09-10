#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dataset-bound MCP facade for Query Claw's retrieval and prediction routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse


MAX_ROWS = 25
MAX_TEXT = 8_000
MAX_SCOPE_SECONDS = 3_600
UPSTREAM_TIMEOUT_SECONDS = 110
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VIEWS = frozenset({"structured", "documents", "predictions"})
_PUBLIC_VIEWS = {
    "records": "structured",
    "documents": "documents",
    "predictions": "predictions",
}
_VIEW_NAMES = {internal: public for public, internal in _PUBLIC_VIEWS.items()}


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


class StaticBearerAuth(TokenVerifier):
    """Verify the one OpenShell-injected credential for this facade."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(token=token, client_id="query-claw", scopes=[])


@dataclass(frozen=True)
class Dataset:
    id: str
    title: str
    description: str
    industry: str
    views: frozenset[str]
    database: str | None
    prediction_database: str | None
    collection: str | None


class DatasetCatalog:
    """The immutable data boundary mounted into one Query Claw deployment."""

    def __init__(self, path: Path) -> None:
        try:
            value = json.loads(
                path.read_text(encoding="utf-8"), object_pairs_hook=_json_object
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("QUERY_CLAW_ACTIVE_MANIFEST is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "fingerprint", "datasets"}
            or value.get("schema_version") != 1
            or not isinstance(value.get("fingerprint"), str)
            or not _SHA256.fullmatch(value["fingerprint"])
            or not isinstance(value.get("datasets"), list)
            or not value["datasets"]
        ):
            raise RuntimeError("QUERY_CLAW_ACTIVE_MANIFEST is invalid")
        datasets = [self._parse(item) for item in value["datasets"]]
        if len({item.id for item in datasets}) != len(datasets):
            raise RuntimeError("active dataset identifiers must be unique")
        bindings: dict[tuple[str, str], str] = {}
        for dataset in datasets:
            resources: set[tuple[str, str]] = set()
            for name in {dataset.database, dataset.prediction_database}:
                if name is not None:
                    resources.add(("ontology", name))
            if dataset.collection is not None:
                resources.add(("retriever", dataset.collection))
            for resource in resources:
                if resource in bindings and bindings[resource] != dataset.id:
                    raise RuntimeError("active datasets share a service binding")
                bindings[resource] = dataset.id
        self.fingerprint = value["fingerprint"]
        self.datasets = {item.id: item for item in datasets}

    @staticmethod
    def _parse(raw: Any) -> Dataset:
        allowed = {
            "schema_version",
            "id",
            "title",
            "description",
            "industry",
            "views",
            "bindings",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) != allowed
            or raw.get("schema_version") != 1
        ):
            raise RuntimeError("active dataset definition is invalid")
        for key in ("id", "title", "description", "industry"):
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise RuntimeError("active dataset definition is invalid")
        if not _ID.fullmatch(raw["id"]):
            raise RuntimeError("active dataset definition is invalid")
        views_raw = raw["views"]
        if (
            not isinstance(views_raw, dict)
            or not views_raw
            or not set(views_raw) <= _VIEWS
            or any(not isinstance(path, str) or not path for path in views_raw.values())
        ):
            raise RuntimeError("active dataset views are invalid")
        bindings = raw["bindings"]
        if not isinstance(bindings, dict) or not set(bindings) <= {
            "ontology",
            "retriever",
        }:
            raise RuntimeError("active dataset bindings are invalid")
        ontology = bindings.get("ontology")
        retriever = bindings.get("retriever")
        if bool({"structured", "predictions"} & set(views_raw)) != bool(ontology):
            raise RuntimeError("active Ontology binding is invalid")
        if ("documents" in views_raw) != bool(retriever):
            raise RuntimeError("active Retriever binding is invalid")
        if ontology is not None:
            if (
                not isinstance(ontology, dict)
                or not set(ontology)
                <= {"database", "prediction_database", "prediction_probe"}
                or "database" not in ontology
                or ("predictions" in views_raw and "prediction_probe" not in ontology)
            ):
                raise RuntimeError("active Ontology binding is invalid")
            database = ontology["database"]
            prediction_database = ontology.get("prediction_database", database)
            if not all(
                isinstance(item, str) and _NAME.fullmatch(item)
                for item in (database, prediction_database)
            ):
                raise RuntimeError("active Ontology database is invalid")
            prediction_probe = ontology.get("prediction_probe", "")
            if "prediction_probe" in ontology and (
                not isinstance(prediction_probe, str)
                or not prediction_probe.strip()
                or len(prediction_probe) > 2_000
            ):
                raise RuntimeError("active Ontology prediction probe is invalid")
        else:
            database = prediction_database = None
        if retriever is not None:
            if not isinstance(retriever, dict) or set(retriever) != {"collection"}:
                raise RuntimeError("active Retriever binding is invalid")
            collection = retriever["collection"]
            if not isinstance(collection, str) or not _NAME.fullmatch(collection):
                raise RuntimeError("active Retriever collection is invalid")
        else:
            collection = None
        return Dataset(
            id=raw["id"],
            title=raw["title"],
            description=raw["description"],
            industry=raw["industry"],
            views=frozenset(views_raw),
            database=database,
            prediction_database=prediction_database,
            collection=collection,
        )

    def resolve(self, view: str, dataset_id: str | None = None) -> Dataset:
        if view not in _VIEWS:
            raise ToolError("unsupported dataset view")
        if dataset_id is None:
            if len(self.datasets) != 1:
                raise ToolError(
                    "dataset_id is required when multiple datasets are active"
                )
            dataset = next(iter(self.datasets.values()))
        elif isinstance(dataset_id, str) and _ID.fullmatch(dataset_id):
            dataset = self.datasets.get(dataset_id)
            if dataset is None:
                raise ToolError("dataset is not active")
        else:
            raise ToolError("dataset is not active")
        if view not in dataset.views:
            raise ToolError("dataset view is unavailable")
        return dataset

    def public_inventory(
        self, visible: dict[str, frozenset[str]] | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "title": item.title,
                "description": item.description,
                "industry": item.industry,
                "views": sorted(
                    _VIEW_NAMES[view]
                    for view in (item.views if visible is None else visible[item.id])
                ),
            }
            for item in sorted(self.datasets.values(), key=lambda value: value.id)
            if visible is None or item.id in visible
        ]


def _qualified_prediction_databases(
    path: Path, catalog: DatasetCatalog
) -> frozenset[str]:
    """Read the setup-time prediction qualification receipt, if current."""

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_json_object
        )
        databases = value["databases"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    expected = {
        dataset.prediction_database
        for dataset in catalog.datasets.values()
        if "predictions" in dataset.views
    }
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "selection_fingerprint", "databases"}
        or value.get("schema_version") != 1
        or value.get("selection_fingerprint") != catalog.fingerprint
        or not isinstance(databases, list)
        or any(not isinstance(database, str) for database in databases)
        or set(databases) != expected
        or len(databases) != len(set(databases))
    ):
        return frozenset()
    return frozenset(databases)


@dataclass
class Scope:
    datasets: dict[str, frozenset[str]]
    expires_at: datetime
    attempts: list[dict[str, str]] = field(default_factory=list)


class ScopeStore:
    """Short-lived capabilities that can only narrow the deployment allowlist."""

    def __init__(self, catalog: DatasetCatalog) -> None:
        self.catalog = catalog
        self._scopes: dict[str, Scope] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _prune(self) -> None:
        now = datetime.now(UTC)
        self._scopes = {
            key: scope for key, scope in self._scopes.items() if scope.expires_at > now
        }

    async def _require_unscoped_access(self) -> None:
        async with self._lock:
            self._prune()
            if self._scopes or len(self.catalog.datasets) != 1:
                raise ToolError(
                    "source_scope is required while a scoped turn or multiple "
                    "datasets are active"
                )

    async def create(self, requested: list[dict[str, Any]], ttl_seconds: int) -> str:
        datasets: dict[str, frozenset[str]] = {}
        if not isinstance(requested, list) or not 1 <= ttl_seconds <= MAX_SCOPE_SECONDS:
            raise ToolError("dataset scope request is invalid")
        for item in requested:
            if not isinstance(item, dict) or set(item) != {"id", "views"}:
                raise ToolError("dataset scope request is invalid")
            dataset_id, public_views = item["id"], item["views"]
            if (
                not isinstance(dataset_id, str)
                or not _ID.fullmatch(dataset_id)
                or dataset_id in datasets
                or not isinstance(public_views, list)
                or not public_views
                or any(not isinstance(view, str) for view in public_views)
            ):
                raise ToolError("dataset scope request is invalid")
            dataset = self.catalog.datasets.get(dataset_id)
            if (
                dataset is None
                or len(public_views) != len(set(public_views))
                or any(view not in _PUBLIC_VIEWS for view in public_views)
            ):
                raise ToolError("dataset scope request is invalid")
            internal_views = frozenset(_PUBLIC_VIEWS[view] for view in public_views)
            if not internal_views <= dataset.views:
                raise ToolError("dataset scope request is invalid")
            datasets[dataset_id] = internal_views
        token = secrets.token_urlsafe(32)
        async with self._lock:
            self._prune()
            self._scopes[self._key(token)] = Scope(
                datasets=datasets,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )
        return token

    async def resolve(
        self, token: str | None, view: str, dataset_id: str | None, tool: str
    ) -> Dataset:
        if token is None:
            await self._require_unscoped_access()
            return self.catalog.resolve(view, dataset_id)
        if not isinstance(token, str) or not token:
            raise ToolError("dataset scope is invalid or expired")
        async with self._lock:
            self._prune()
            scope = self._scopes.get(self._key(token))
            if scope is None:
                raise ToolError("dataset scope is invalid or expired")
            if not scope.datasets:
                raise ToolError("no datasets are available in this source scope")
            if dataset_id is None:
                if len(scope.datasets) != 1:
                    raise ToolError(
                        "dataset_id is required when multiple datasets are scoped"
                    )
                dataset_id = next(iter(scope.datasets))
            if dataset_id not in scope.datasets:
                raise ToolError("dataset is outside this source scope")
            if view not in scope.datasets[dataset_id]:
                raise ToolError("dataset view is outside this source scope")
            dataset = self.catalog.resolve(view, dataset_id)
            if len(scope.attempts) >= 1_000:
                raise ToolError("dataset scope call limit reached")
            # Reserve the attempt before releasing the authorization lock. Failed
            # and in-flight upstream calls therefore count and remain auditable.
            scope.attempts.append({"tool": tool, "dataset_id": dataset.id})
            return dataset

    async def public_inventory(
        self, token: str | None = None, dataset_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the deployment inventory narrowed by an optional scope."""

        if token is None:
            await self._require_unscoped_access()
            visible = {item.id: item.views for item in self.catalog.datasets.values()}
        else:
            if not isinstance(token, str) or not token:
                raise ToolError("dataset scope is invalid or expired")
            async with self._lock:
                self._prune()
                scope = self._scopes.get(self._key(token))
                if scope is None:
                    raise ToolError("dataset scope is invalid or expired")
                visible = dict(scope.datasets)
        if dataset_id is not None:
            if not isinstance(dataset_id, str) or dataset_id not in visible:
                raise ToolError("dataset is outside this source scope")
            visible = {dataset_id: visible[dataset_id]}
        return self.catalog.public_inventory(visible)

    async def inspect(self, token: str, *, revoke: bool = False) -> dict[str, Any]:
        if not isinstance(token, str) or not token:
            raise ToolError("dataset scope is invalid or expired")
        async with self._lock:
            self._prune()
            key = self._key(token)
            scope = self._scopes.pop(key, None) if revoke else self._scopes.get(key)
            if scope is None:
                raise ToolError("dataset scope is invalid or expired")
            return {
                "calls": list(scope.attempts),
                "call_semantics": "attempted",
                "expires_at": scope.expires_at.isoformat(),
            }


def _server(name: str, *, lifespan: Any = None) -> FastMCP:
    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    if len(token) < 32:
        raise RuntimeError("MCP_BEARER_TOKEN must contain at least 32 characters")
    return FastMCP(
        name=name,
        version="2",
        auth=StaticBearerAuth(token),
        lifespan=lifespan,
    )


def _safe_rows(result: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    rows = result.get("rows") or result.get("sql_response_from_db") or []
    if isinstance(rows, str) or (
        isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], str)
    ):
        encoded = rows if isinstance(rows, str) else rows[0]
        try:
            rows = json.loads(encoded)
        except json.JSONDecodeError:
            rows = []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ToolError("upstream returned invalid rows")
    return rows[:MAX_ROWS], len(rows)


def _short_strings(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:512] for item in value[:limit]]


def _bounded_json(value: Any, *, max_items: int = 25, depth: int = 0) -> Any:
    """Keep upstream diagnostics useful without returning an unbounded payload."""

    # Retriever evidence is nested as results -> evidence -> locator. Preserve
    # that bounded contract while still cutting off unexpected deep payloads.
    if depth >= 7:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, list):
        return [
            _bounded_json(item, max_items=max_items, depth=depth + 1)
            for item in value[:max_items]
        ]
    if isinstance(value, dict):
        return {
            str(key)[:128]: _bounded_json(item, max_items=max_items, depth=depth + 1)
            for key, item in list(value.items())[:max_items]
        }
    return str(value)[:512]


def query_claw_server() -> FastMCP:
    """Build the complete facade from one immutable active-data manifest."""

    manifest = Path(
        os.environ.get(
            "QUERY_CLAW_ACTIVE_MANIFEST",
            "/query-claw-active/active-data-packs.json",
        )
    )
    catalog = DatasetCatalog(manifest)
    qualified_predictions = _qualified_prediction_databases(
        manifest.parent / "prediction-qualified.json", catalog
    )
    scopes = ScopeStore(catalog)
    scope_bearer = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    # NemoClaw owns Hermes's 120-second MCP deadline. Stop upstream work first
    # so Hermes receives a bounded tool error instead of opening its circuit.
    upstream_timeout = UPSTREAM_TIMEOUT_SECONDS
    gsf = httpx.AsyncClient(
        base_url=os.environ.get("GSF_API_URL", "http://gsf:3001").rstrip("/"),
        timeout=upstream_timeout,
    )
    retriever_token = os.environ.get("RETRIEVER_API_TOKEN", "").strip()
    if len(retriever_token) < 32:
        raise RuntimeError("RETRIEVER_API_TOKEN must contain at least 32 characters")
    retriever = httpx.AsyncClient(
        base_url=os.environ.get("RETRIEVER_API_URL", "http://retriever:7670").rstrip(
            "/"
        ),
        headers={
            "Authorization": f"Bearer {retriever_token}",
            "X-NRL-Scope": os.environ.get("RETRIEVER_SCOPE", "query-claw"),
        },
        follow_redirects=False,
        timeout=upstream_timeout,
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            await asyncio.gather(gsf.aclose(), retriever.aclose())

    mcp = _server("Query Claw", lifespan=lifespan)

    async def gsf_json(
        path: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = (
                await gsf.post(path, json=payload)
                if payload is not None
                else await gsf.get(path)
            )
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolError("Ontology request failed") from exc
        if not isinstance(value, dict):
            raise ToolError("Ontology returned invalid data")
        return value

    async def gsf_stream(payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(upstream_timeout):
                async with gsf.stream(
                    "POST",
                    "/api/chat/completions",
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        event = json.loads(raw)
                        if not isinstance(event, dict):
                            raise ValueError("invalid event")
                        if event.get("type") == "error":
                            raise ToolError("Ontology agent failed")
                        if event.get("type") == "result":
                            result = event.get("answer") or event
        except ToolError:
            raise
        except (TimeoutError, httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise ToolError("Ontology question failed") from exc
        if not isinstance(result, dict):
            raise ToolError("Ontology ended without a result")
        return result

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "Query Claw"})

    async def scope_body(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ToolError("invalid scope request") from exc
        if not isinstance(body, dict):
            raise ToolError("invalid scope request")
        return body

    def scope_authorized(request: Request) -> bool:
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        return (
            separator == " "
            and scheme.casefold() == "bearer"
            and secrets.compare_digest(token, scope_bearer)
        )

    @mcp.custom_route("/scopes/create", methods=["POST"])
    async def create_scope(request: Request) -> JSONResponse:
        if not scope_authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await scope_body(request)
            if set(body) - {"datasets", "ttl_seconds"}:
                raise ToolError("invalid scope request")
            datasets = body.get("datasets")
            ttl = body.get("ttl_seconds", 900)
            if not isinstance(datasets, list) or type(ttl) is not int:
                raise ToolError("invalid scope request")
            token = await scopes.create(datasets, ttl)
            return JSONResponse({"scope_token": token})
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @mcp.custom_route("/scopes/read", methods=["POST"])
    async def read_scope(request: Request) -> JSONResponse:
        if not scope_authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await scope_body(request)
            if set(body) != {"scope_token"} or not isinstance(body["scope_token"], str):
                raise ToolError("invalid scope request")
            return JSONResponse(await scopes.inspect(body["scope_token"]))
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @mcp.custom_route("/scopes/revoke", methods=["POST"])
    async def revoke_scope(request: Request) -> JSONResponse:
        if not scope_authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await scope_body(request)
            if set(body) != {"scope_token"} or not isinstance(body["scope_token"], str):
                raise ToolError("invalid scope request")
            return JSONResponse(await scopes.inspect(body["scope_token"], revoke=True))
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def check_readiness(
        dataset_id: str | None = None, scope_token: str | None = None
    ) -> dict[str, Any]:
        """List only the datasets and evidence views exposed to this turn."""
        inventory = await scopes.public_inventory(scope_token, dataset_id)
        if not inventory:
            return {
                "ready": True,
                "datasets": [],
            }
        needs_ontology = any(
            {"records", "predictions"} & set(item["views"]) for item in inventory
        )
        semantic_ready = False
        names: set[str] = set()
        if needs_ontology:
            try:
                status, databases = await asyncio.gather(
                    gsf_json("/api/semantic-compilation/status"),
                    gsf_json("/api/datasources/dbs"),
                )
                rows = databases.get("data") or []
                names = {
                    str(row.get("name") or row.get("database_name"))
                    for row in rows
                    if isinstance(row, dict)
                }
                semantic_ready = status.get("calculated") is True
            except ToolError:
                pass
        for item in inventory:
            dataset = catalog.datasets[item["id"]]
            readiness: dict[str, bool] = {}
            if "records" in item["views"]:
                readiness["records"] = semantic_ready and dataset.database in names
            if "documents" in item["views"]:
                try:
                    response = await retriever.get(
                        f"/v1/collections/{dataset.collection}"
                    )
                    response.raise_for_status()
                    readiness["documents"] = True
                except httpx.HTTPError:
                    readiness["documents"] = False
            if "predictions" in item["views"]:
                readiness["predictions"] = (
                    semantic_ready
                    and dataset.prediction_database in names
                    and dataset.prediction_database in qualified_predictions
                )
            item["readiness"] = readiness
        return {
            "ready": all(
                all(view_ready for view_ready in item["readiness"].values())
                for item in inventory
            ),
            "datasets": inventory,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def check_answerable(
        question: Annotated[str, Field(min_length=1, max_length=4_000)],
        dataset_id: str | None = None,
        scope_token: str | None = None,
    ) -> dict[str, Any]:
        """Check structured coverage; a positive result requires ask_question."""
        dataset = await scopes.resolve(
            scope_token, "structured", dataset_id, "check_answerable"
        )
        value = await gsf_json(
            "/api/question-entity-coverage",
            payload={"question": question, "target_db": dataset.database},
        )
        return {**_bounded_json(value), "dataset_id": dataset.id}

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def ask_question(
        question: Annotated[str, Field(min_length=1, max_length=4_000)],
        dataset_id: str | None = None,
        scope_token: str | None = None,
    ) -> dict[str, Any]:
        """Query governed records in exactly one active dataset."""
        dataset = await scopes.resolve(
            scope_token, "structured", dataset_id, "ask_question"
        )
        complete = (
            question.rstrip()
            + "\n\nPreserve every entity, status, date, population, ranking, and "
            "other filter exactly. Include stable identifiers needed to trace "
            "every row."
        )
        result = await gsf_stream({"question": complete, "target_db": dataset.database})
        rows, row_count = _safe_rows(result)
        return {
            "dataset_id": dataset.id,
            "sql": str(result.get("sql_code") or "")[:MAX_TEXT],
            "rows": rows,
            "row_count": row_count,
            "truncated": row_count > MAX_ROWS,
        }

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def query(
        question: Annotated[str, Field(min_length=1, max_length=4_000)],
        dataset_id: str | None = None,
        scope_token: str | None = None,
        top_k: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> dict[str, Any]:
        """Retrieve cited evidence from one active document collection."""
        dataset = await scopes.resolve(scope_token, "documents", dataset_id, "query")
        try:
            response = await retriever.post(
                "/v1/query",
                json={
                    "query": question,
                    "top_k": top_k,
                    "format": "evidence",
                    "rerank": False,
                    "collection_name": dataset.collection,
                },
            )
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolError("Retriever query failed") from exc
        if not isinstance(value, dict):
            raise ToolError("Retriever returned invalid data")
        bounded = _bounded_json(value)
        if not isinstance(bounded, dict):
            raise ToolError("Retriever returned invalid data")
        return {**bounded, "dataset_id": dataset.id}

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
    async def predict(
        question: Annotated[str, Field(min_length=1, max_length=4_000)],
        dataset_id: str | None = None,
        scope_token: str | None = None,
    ) -> dict[str, Any]:
        """Predict only with an explicit target, population/entity, and horizon."""
        dataset = await scopes.resolve(
            scope_token, "predictions", dataset_id, "predict"
        )
        result = await gsf_stream(
            {
                "question": question,
                "prediction": True,
                "target_db": dataset.prediction_database,
            }
        )
        rows, row_count = _safe_rows(result)
        response = result.get("response")
        graph = result.get("graph_receipt")
        if (
            not isinstance(graph, dict)
            or graph.get("database_name") != dataset.prediction_database
        ):
            raise ToolError("Ontology prediction source did not match the dataset")
        graph_receipt = {
            key: _bounded_json(graph[key], max_items=12)
            for key in ("database_name", "graph_revision", "prediction_scope")
            if key in graph
        }
        return {
            "dataset_id": dataset.id,
            "response": str(response)[:MAX_TEXT] if response is not None else "",
            "pql": str(
                result.get("pql")
                or result.get("pql_code")
                or result.get("sql_code")
                or ""
            )[:MAX_TEXT],
            "rows": rows,
            "row_count": row_count,
            "truncated": row_count > MAX_ROWS,
            "assumptions": _short_strings(result.get("assumptions")),
            "warnings": _short_strings(result.get("warnings")),
            "graph_receipt": graph_receipt,
        }

    return mcp


def main() -> int:
    if len(sys.argv) != 1:
        print("usage: app.py", file=sys.stderr)
        return 2
    port = int(os.environ.get("MCP_PORT", "8000"))
    query_claw_server().run(transport="http", host="0.0.0.0", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
