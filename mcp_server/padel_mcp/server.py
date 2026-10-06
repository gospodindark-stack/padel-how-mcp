"""Streamable HTTP MCP server for the public padel.how racket catalogue."""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, Literal
from pydantic import Field
import math

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
    SDK_MODERN = True
except ImportError:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    SDK_MODERN = False
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .catalog import CatalogError, RacketCatalog, public_racket
from .telemetry import observe_tool, CONTEXT, snapshot as telemetry_snapshot
from .models import (
    CompareRacketsEnvelope,
    DatasetMetadata,
    GetOffersEnvelope,
    GetRacketEnvelope,
    ListRacketBrandsEnvelope,
    LocatorCitation,
    SearchRacketsEnvelope,
)


SOURCE_ENDPOINT = "https://padel.how/rackets.json"
SCHEMA_VERSION = "1.2.0"
NON_LIVE_OFFER_WARNING = (
    "Offer prices are published figures with an observed_at timestamp, "
    "not live checkout prices; verify on the retailer page before buying."
)
ALLOWED_HOSTS = ["padel.how", "padel.how:443", "localhost:*", "127.0.0.1:*"]
ALLOWED_ORIGINS = ["https://padel.how", "http://localhost:*", "http://127.0.0.1:*"]
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
catalog = RacketCatalog()

mcp = FastMCP(
    "padel.how Racket Catalogue",
    instructions=(
        "Read-only published racket catalogue. It does not provide "
        "recommendations; offer prices are published figures, not live prices."
    ),
    **({"version": "1.2.0"} if SDK_MODERN else {
        "host": os.getenv("PADEL_MCP_HOST", "127.0.0.1"),
        "port": int(os.getenv("PADEL_MCP_PORT", "8790")),
        "streamable_http_path": "/mcp", "stateless_http": True, "json_response": True,
        "transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS),
    }),
)


def _snapshot():
    try:
        return catalog.snapshot()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError("Catalogue is unavailable; retry after a verified snapshot is installed") from exc


def _dataset_metadata(snapshot: Any) -> DatasetMetadata:
    return DatasetMetadata(
        source_endpoint=SOURCE_ENDPOINT,
        source_generated=snapshot.generated,
        record_count=len(snapshot.rackets),
        dataset_version=snapshot.version,
        methodology_url=snapshot.methodology_url,
        offer_source_generated=snapshot.offer_generated,
        offer_record_count=len(snapshot.offers),
    )


def _public_racket(snapshot: Any, raw: dict[str, Any]) -> dict[str, Any]:
    return public_racket(raw, snapshot.offers.get(raw["slug"]), snapshot.offer_lists.get(raw["slug"]))


def _citations(snapshot: Any, items: list[dict[str, Any]] | None = None) -> list[LocatorCitation]:
    urls = sorted({item["review_url"] for item in items or [] if item.get("review_url")})
    return [
        *[
            LocatorCitation(url=url, label="Published review page locator")
            for url in urls
        ],
        LocatorCitation(url=snapshot.methodology_url, label="Racket methodology locator"),
    ]


def _resource_envelope(
    tool: str,
    result: Any,
    warnings: list[str] | None = None,
    items: list[dict[str, Any]] | None = None,
    snapshot: Any | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or _snapshot()
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": _dataset_metadata(snapshot).model_dump(mode="json"),
        "tool": tool,
        "result": result,
        "citations": [citation.model_dump(mode="json") for citation in _citations(snapshot, items)],
        "warnings": warnings or [],
    }


def _item_warnings(items: list[dict[str, Any]]) -> list[str]:
    if any(item["editorial_player_level_tag"] is not None for item in items):
        return ["editorial_player_level_tag is an editorial catalogue label, not an individualized recommendation."]
    return []


@mcp.tool(annotations=READ_ONLY, structured_output=True)
@observe_tool
def search_rackets(
    query: str | None = None,
    brand: str | None = None,
    year: Annotated[int, Field(strict=True, ge=1900, le=2200)] | None = None,
    min_score: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)] | None = None,
    max_score: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)] | None = None,
    editorial_player_level_tag: Literal["beginner", "intermediate", "advanced"] | None = None,
    audience: Literal["women"] | None = None,
    limit: Annotated[int, Field(strict=True, ge=1, le=25)] = 10,
    offset: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)] = 0,
    snapshot_version: str | None = None,
    sort: Literal["score_desc", "name_asc"] = "score_desc",
) -> SearchRacketsEnvelope:
    """Search published rackets using literal, case-insensitive catalogue filters."""
    if type(limit) is not int or not 1 <= limit <= 25:
        raise ToolError("limit must be between 1 and 25")
    if type(offset) is not int or not 0 <= offset <= 9007199254740991:
        raise ToolError("offset must be a nonnegative safe integer")
    if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100) for value in (min_score, max_score)):
        raise ToolError("scores must be finite numbers from 0 to 100")
    if year is not None and (type(year) is not int or not 1900 <= year <= 2200):
        raise ToolError("year must be an integer from 1900 to 2200")
    if min_score is not None and max_score is not None and min_score > max_score:
        raise ToolError("min_score cannot exceed max_score")
    snapshot = _snapshot()
    if snapshot_version is not None and snapshot_version != snapshot.version:
        raise ToolError("stale snapshot_version; restart pagination at offset 0")
    query = query.strip() if query is not None else None
    brand = brand.strip() if brand is not None else None
    if query == "":
        raise ToolError("query cannot be empty")
    if brand == "":
        raise ToolError("brand cannot be empty")
    query_folded = query.casefold() if query is not None else None
    brand_folded = brand.casefold() if brand is not None else None
    rows = []
    for raw in snapshot.rackets:
        item = _public_racket(snapshot, raw)
        if query_folded is not None and query_folded not in str(item["name"] or "").casefold() and query_folded not in str(item["brand"] or "").casefold():
            continue
        if brand_folded is not None and str(item["brand"] or "").casefold() != brand_folded:
            continue
        if year is not None and item["year"] != year:
            continue
        if min_score is not None and (not isinstance(item["score"], (int, float)) or item["score"] < min_score):
            continue
        if max_score is not None and (not isinstance(item["score"], (int, float)) or item["score"] > max_score):
            continue
        if editorial_player_level_tag is not None and item["editorial_player_level_tag"] != editorial_player_level_tag:
            continue
        if audience is not None and audience not in item["audience_tags"]:
            continue
        rows.append(item)
    if sort == "score_desc":
        rows.sort(key=lambda item: (-float(item["score"] or 0), str(item["name"] or "").casefold(), item["slug"]))
    else:
        rows.sort(key=lambda item: (str(item["name"] or "").casefold(), item["slug"]))
    page = rows[offset : offset + limit]
    applied_filters = {
        key: value
        for key, value in {
            "query": query,
            "brand": brand,
            "year": year,
            "min_score": min_score,
            "max_score": max_score,
            "editorial_player_level_tag": editorial_player_level_tag,
            "audience": audience,
        }.items()
        if value is not None
    }
    return SearchRacketsEnvelope(
        schema_version=SCHEMA_VERSION,
        dataset=_dataset_metadata(snapshot),
        tool="search_rackets",
        result={
            "items": page,
            "total_matches": len(rows),
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "has_more": offset + len(page) < len(rows),
            "applied_filters": applied_filters,
        },
        citations=_citations(snapshot, page),
        warnings=_item_warnings(page),
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
@observe_tool
def get_racket(slug: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]) -> GetRacketEnvelope:
    """Get one published racket by its validated catalogue slug."""
    if not catalog.is_safe_slug(slug):
        raise ToolError("slug is unsupported")
    snapshot = _snapshot()
    raw = snapshot.by_slug.get(slug)
    if raw is None:
        raise ToolError("racket not found")
    item = _public_racket(snapshot, raw)
    return GetRacketEnvelope(
        schema_version=SCHEMA_VERSION,
        dataset=_dataset_metadata(snapshot),
        tool="get_racket",
        result=item,
        citations=_citations(snapshot, [item]),
        warnings=_item_warnings([item]),
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
@observe_tool
def compare_rackets(slugs: Annotated[list[Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]], Field(min_length=2, max_length=4, json_schema_extra={"uniqueItems": True})]) -> CompareRacketsEnvelope:
    """Compare two to four distinct published racket slugs."""
    if not 2 <= len(slugs) <= 4:
        raise ToolError("provide between 2 and 4 slugs")
    if len(set(slugs)) != len(slugs):
        raise ToolError("duplicate slugs are unsupported")
    if any(not catalog.is_safe_slug(slug) for slug in slugs):
        raise ToolError("one or more slugs are unsupported")
    snapshot = _snapshot()
    missing = [slug for slug in slugs if slug not in snapshot.by_slug]
    if missing:
        raise ToolError("one or more rackets were not found")
    items = [_public_racket(snapshot, snapshot.by_slug[slug]) for slug in slugs]
    numeric_deltas = {
        "overall_score": _deltas([item["score"] for item in items]),
        "score_categories": {
            category: _deltas([item["scores"][category] for item in items])
            for category in snapshot.score_categories
        },
    }
    return CompareRacketsEnvelope(
        schema_version=SCHEMA_VERSION,
        dataset=_dataset_metadata(snapshot),
        tool="compare_rackets",
        result={"items": items, "numeric_deltas": numeric_deltas},
        citations=_citations(snapshot, items),
        warnings=_item_warnings(items),
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
@observe_tool
def list_racket_brands() -> ListRacketBrandsEnvelope:
    """List brand variants grouped by case-insensitive normalized spelling."""
    grouped: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    snapshot = _snapshot()
    for row in snapshot.rackets:
        brand = row.get("brand")
        if not isinstance(brand, str) or not brand:
            continue
        normalized = brand.casefold()
        grouped.setdefault(normalized, []).append(brand)
        counts[normalized] = counts.get(normalized, 0) + 1
    brands = [
        {"normalized": normalized, "display_names": sorted(set(names), key=lambda name: (name.casefold(), name)), "racket_count": counts[normalized]}
        for normalized, names in sorted(grouped.items())
    ]
    return ListRacketBrandsEnvelope(
        schema_version=SCHEMA_VERSION,
        dataset=_dataset_metadata(snapshot),
        tool="list_racket_brands",
        result={"brands": brands},
        citations=_citations(snapshot),
        warnings=[],
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
@observe_tool
def get_offers(
    slug: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")] | None = None,
    retailer: Literal["Amazon", "Padel Market", "PadelDogs", "Tennis Point", "Racketshop.com", "SPORTLET"] | None = None,
    limit: Annotated[int, Field(strict=True, ge=1, le=25)] = 25,
    offset: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)] = 0,
    snapshot_version: str | None = None,
) -> GetOffersEnvelope:
    """List published retailer offers per racket; prices carry observed_at and are not live."""
    if type(limit) is not int or not 1 <= limit <= 25:
        raise ToolError("limit must be between 1 and 25")
    if type(offset) is not int or not 0 <= offset <= 9007199254740991:
        raise ToolError("offset must be a nonnegative safe integer")
    snapshot = _snapshot()
    if snapshot_version is not None and snapshot_version != snapshot.version:
        raise ToolError("stale snapshot_version; restart pagination at offset 0")
    if slug is not None:
        slug = slug.strip()
        if slug == "":
            raise ToolError("slug cannot be empty")
        if not catalog.is_safe_slug(slug):
            raise ToolError("slug is unsupported")
        if slug not in snapshot.by_slug:
            raise ToolError("racket not found")
    entries: list[dict[str, Any]] = []
    matched_offer_count = 0
    for offer_slug in sorted(snapshot.offers):
        if slug is not None and offer_slug != slug:
            continue
        offers = snapshot.offer_lists.get(offer_slug) or [snapshot.offers[offer_slug]]
        filtered = [
            offer
            for offer in offers
            if retailer is None or str(offer.get("retailer") or "").casefold() == retailer.casefold()
        ]
        if not filtered:
            continue
        matched_offer_count += len(filtered)
        raw = snapshot.by_slug[offer_slug]
        entries.append(
            {
                "slug": offer_slug,
                "name": raw.get("name"),
                "review_url": raw.get("url"),
                "offers": filtered,
            }
        )
    page = entries[offset : offset + limit]
    applied_filters = {
        key: value
        for key, value in {"slug": slug, "retailer": retailer}.items()
        if value is not None
    }
    return GetOffersEnvelope(
        schema_version=SCHEMA_VERSION,
        dataset=_dataset_metadata(snapshot),
        tool="get_offers",
        result={
            "items": page,
            "total_matches": len(entries),
            "offer_count": matched_offer_count,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < len(entries),
            "applied_filters": applied_filters,
        },
        citations=_citations(snapshot, page),
        warnings=[NON_LIVE_OFFER_WARNING],
    )


@mcp.resource("padel://dataset/rackets", mime_type="application/json")
def dataset_resource() -> str:
    """The complete published dataset in the MCP envelope."""
    snapshot = _snapshot()
    items = [_public_racket(snapshot, row) for row in sorted(snapshot.rackets, key=lambda row: row["slug"])]
    return json.dumps(
        _resource_envelope("dataset_resource", {"items": items}, _item_warnings(items), items, snapshot),
        sort_keys=True,
    )


@mcp.resource("padel://methodology/rackets", mime_type="application/json")
def methodology_resource() -> str:
    """Dataset methodology metadata."""
    snapshot = _snapshot()
    return json.dumps(
        _resource_envelope("methodology_resource", {"methodology_url": snapshot.methodology_url}, snapshot=snapshot),
        sort_keys=True,
    )


@mcp.resource("padel://rackets/{slug}", mime_type="application/json")
def racket_resource(slug: str) -> str:
    """One validated published racket record."""
    if not catalog.is_safe_slug(slug):
        raise ToolError("slug is unsupported")
    snapshot = _snapshot()
    raw = snapshot.by_slug.get(slug)
    if raw is None:
        raise ToolError("racket not found")
    item = _public_racket(snapshot, raw)
    return json.dumps(
        _resource_envelope("racket_resource", item, _item_warnings([item]), [item], snapshot),
        sort_keys=True,
    )


def _deltas(values: list[int | float]) -> list[int | float]:
    baseline = values[0]
    return [value - baseline for value in values]


def _header(scope: Scope, name: bytes) -> str | None:
    values = [value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == name]
    return ",".join(values) if values else None


def _matches_allowed(value: str | None, allowed: list[str]) -> bool:
    if value is None:
        return False
    normalized = value.casefold()
    for pattern in allowed:
        expected = pattern.casefold()
        if expected.endswith(":*"):
            prefix = expected[:-1]
            if normalized.startswith(prefix) and normalized[len(prefix) :].isdigit():
                return True
        elif normalized == expected:
            return True
    return False


def _accepts_mcp(scope: Scope) -> bool:
    accept = _header(scope, b"accept")
    if accept is None:
        return False
    tokens = {part.split(";", 1)[0].strip().casefold() for part in accept.split(",")}
    return {"application/json", "text/event-stream"}.issubset(tokens)


class PadelMCPASGIApp:
    """Preserve FastMCP lifespan while enforcing edge contracts before its app."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/internal/metrics":
            if os.getenv("PADEL_MCP_METRICS_ENABLED") != "1" or (scope.get("client") or (None,))[0] not in ("127.0.0.1", "::1"):
                await JSONResponse({"detail": "not found"}, status_code=404)(scope, receive, send)
                return
            await JSONResponse(telemetry_snapshot(), headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        if path == "/healthz":
            host = _header(scope, b"host")
            if not _matches_allowed(host, ALLOWED_HOSTS):
                await JSONResponse({"detail": "misdirected request"}, status_code=421, headers={"Cache-Control": "no-store"})(scope, receive, send)
                return
            origin = _header(scope, b"origin")
            if origin is not None and not _matches_allowed(origin, ALLOWED_ORIGINS):
                await JSONResponse({"detail": "forbidden origin"}, status_code=403, headers={"Cache-Control": "no-store"})(scope, receive, send)
                return
            snapshot = _snapshot()
            await JSONResponse(
                {"status": "degraded" if catalog.reload_degraded else "ok", "reload_degraded": catalog.reload_degraded, "product_version": "1.2.0", "dataset_version": snapshot.version, "record_count": len(snapshot.rackets), "catalog_version": snapshot.signature[0], "offer_version": snapshot.signature[1], "bundle_version": snapshot.signature[2] or None},
                headers={"Cache-Control": "no-store"},
                status_code=503 if catalog.reload_degraded else 200,
            )(scope, receive, send)
            return
        if scope.get("method") == "POST" and path == "/mcp" and not _accepts_mcp(scope):
            await JSONResponse({"detail": "Accept must include application/json and text/event-stream"}, status_code=406)(scope, receive, send)
            return
        family = (_header(scope, b"user-agent") or "").lower()
        client = next((name for name in ("claude", "chatgpt", "inspector") if name in family), "unknown")
        # This header labels synthetic probes; it grants no access or trust.
        internal = _header(scope, b"x-padel-internal-probe") == "1"
        token = CONTEXT.set(("internal" if internal else client, internal))
        try:
            await self.inner(scope, receive, send)
        finally:
            CONTEXT.reset(token)


if not SDK_MODERN:
    mcp._mcp_server.version = "1.2.0"
app = PadelMCPASGIApp(mcp.streamable_http_app(**({
    "streamable_http_path": "/mcp", "stateless_http": True, "json_response": True,
    "max_request_body_size": 1048576,
    "transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS),
} if SDK_MODERN else {})))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("PADEL_MCP_HOST", "127.0.0.1"), port=int(os.getenv("PADEL_MCP_PORT", "8790")))
