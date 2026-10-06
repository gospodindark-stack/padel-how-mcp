from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
try:
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel

from mcp_server.padel_mcp import server
from mcp_server.padel_mcp.catalog import RacketCatalog


ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "public"
DATASET = PUBLIC / "rackets.json"
EXPECTED_TOOLS = {"search_rackets", "get_racket", "compare_rackets", "list_racket_brands", "get_offers"}
EXPECTED_RESOURCES = {"padel://dataset/rackets", "padel://methodology/rackets"}
EXPECTED_TEMPLATES = {"padel://rackets/{slug}"}


# The catalogue grows with every batch. Pin the contract to the published
# catalogue so a new review does not read as a contract break.
CATALOGUE_SIZE = len(json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / "public" / "rackets.json")
    .read_text(encoding="utf-8")
)["rackets"])

def envelope(result: dict | BaseModel) -> dict:
    if isinstance(result, BaseModel):
        result = result.model_dump(mode="json")
    assert set(result) == {"schema_version", "dataset", "tool", "result", "citations", "warnings"}
    assert result["schema_version"] == "1.2.0"
    assert result["dataset"]["record_count"] == CATALOGUE_SIZE
    assert result["dataset"]["dataset_version"].startswith("sha256:")
    return result


def test_catalog_has_exact_count_safe_unique_slugs_and_sha256_version() -> None:
    raw = DATASET.read_bytes()
    payload = json.loads(raw)
    snapshot = RacketCatalog(PUBLIC).snapshot()
    slugs = [row["slug"] for row in snapshot.rackets]

    assert payload["total"] == CATALOGUE_SIZE
    assert len(snapshot.rackets) == CATALOGUE_SIZE
    assert len(slugs) == len(set(slugs)) == CATALOGUE_SIZE
    assert all(RacketCatalog.is_safe_slug(slug) for slug in slugs)
    if (PUBLIC / "data/bundle-manifest.json").exists():
        from mcp_server.padel_mcp.bundle import load_bundle
        assert snapshot.version == load_bundle(PUBLIC).version
    else:
        assert snapshot.version == "sha256:" + hashlib.sha256(raw + (PUBLIC / "data/offers.json").read_bytes()).hexdigest()


def test_catalog_reloads_after_copied_dataset_mtime_and_content_change(tmp_path: Path) -> None:
    copied = tmp_path / "rackets.json"
    shutil.copy2(DATASET, copied)
    catalog = RacketCatalog(tmp_path)
    first = catalog.snapshot()

    payload = json.loads(copied.read_text())
    payload["rackets"][0]["name"] += " (reload marker)"
    changed = json.dumps(payload, indent=2, ensure_ascii=False).encode()
    copied.write_bytes(changed)
    stat = copied.stat()
    os.utime(copied, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    second = catalog.snapshot()
    assert second is not first
    assert second.rackets[0]["name"].endswith("(reload marker)")
    assert second.version == f"sha256:{hashlib.sha256(changed + catalog.offer_path.read_bytes()).hexdigest()}"


def test_normalization_not_specified_is_null_unknown_and_women_is_audience_only() -> None:
    # 2026-08-26: the emergency data fills left no "Not specified" weights in
    # the catalogue, so the unknown-branch is asserted on a synthetic row.
    unknown = next(row for row in server.catalog.snapshot().rackets if row.get("weight_g"))
    unknown = dict(unknown, weight_g="Not specified")
    normalized = server.public_racket(unknown)
    assert normalized["specifications"]["weight_g"] == {"value": None, "known": False}
    for row in server.catalog.snapshot().rackets:
        assert row.get("weight_g") != "Not specified", \
            "placeholder weights must not reach the catalogue"

    women = next(row for row in server.catalog.snapshot().rackets if row.get("gender") == "women")
    item = server.public_racket(women)
    assert item["editorial_player_level_tag"] is None
    assert item["audience_tags"] == ["women"]


def test_search_defaults_order_filters_pagination_and_limits() -> None:
    default = envelope(server.search_rackets())
    items = default["result"]["items"]
    assert len(items) == 10
    assert default["result"]["sort"] == "score_desc"
    assert [(-item["score"], item["name"].casefold(), item["slug"]) for item in items] == sorted(
        (-item["score"], item["name"].casefold(), item["slug"]) for item in items
    )

    by_name = envelope(server.search_rackets(sort="name_asc", limit=25))
    assert [item["name"].casefold() for item in by_name["result"]["items"]] == sorted(
        item["name"].casefold() for item in by_name["result"]["items"]
    )
    assert envelope(server.search_rackets(brand="nOx"))["result"]["total_matches"] == sum(
        str(row.get("brand", "")).casefold() == "nox" for row in server.catalog.snapshot().rackets
    )
    query = envelope(server.search_rackets(query="AT10"))["result"]
    assert query["total_matches"] > 0
    assert all("at10" in (item["name"] + item["brand"]).casefold() for item in query["items"])
    filtered = envelope(server.search_rackets(query=" AT10 ", brand=" nOx ", limit=1))["result"]
    assert filtered["applied_filters"] == {"query": "AT10", "brand": "nOx"}
    assert filtered["has_more"] is True
    with pytest.raises(ToolError, match="query cannot be empty"):
        server.search_rackets(query="   ")
    with pytest.raises(ToolError, match="brand cannot be empty"):
        server.search_rackets(brand="\t")

    source = server.catalog.snapshot().rackets
    year = source[0]["year"]
    year_result = envelope(server.search_rackets(year=year))["result"]
    assert all(item["year"] == year for item in year_result["items"])
    bounded = envelope(server.search_rackets(min_score=80, max_score=85))["result"]
    assert all(80 <= item["score"] <= 85 for item in bounded["items"])
    level = envelope(server.search_rackets(editorial_player_level_tag="advanced"))["result"]
    assert all(item["editorial_player_level_tag"] == "advanced" for item in level["items"])
    women = envelope(server.search_rackets(audience="women"))["result"]
    assert women["total_matches"] == 17  # Royal Padel 2026 batch added Whip Light, marketed primarily to women (16 -> 17)
    assert all(item["audience_tags"] == ["women"] for item in women["items"])

    page = envelope(server.search_rackets(limit=3, offset=2))["result"]
    full = envelope(server.search_rackets(limit=25))["result"]["items"]
    assert page["items"] == full[2:5]
    for kwargs in ({"limit": 0}, {"limit": 26}, {"offset": -1}, {"offset": 9007199254740992}, {"min_score": 90, "max_score": 80}):
        with pytest.raises(ToolError):
            server.search_rackets(**kwargs)


def test_get_known_unknown_and_unsafe_slug() -> None:
    slug = server.catalog.snapshot().rackets[0]["slug"]
    result = envelope(server.get_racket(slug))
    assert result["result"]["slug"] == slug
    with pytest.raises(ToolError, match="not found"):
        server.get_racket("does-not-exist")
    with pytest.raises(ToolError, match="unsupported"):
        server.get_racket("../rackets.json")


def test_compare_two_to_four_preserves_order_and_reports_all_numeric_deltas() -> None:
    slugs = [row["slug"] for row in server.catalog.snapshot().rackets[:4]]
    result = envelope(server.compare_rackets(slugs))
    comparison = result["result"]
    items = comparison["items"]
    assert [item["slug"] for item in items] == slugs
    deltas = comparison["numeric_deltas"]
    assert set(deltas) == {"overall_score", "score_categories"}
    assert set(deltas["score_categories"]) == set(server.catalog.snapshot().score_categories)
    expected_overall = [item["score"] - items[0]["score"] for item in items]
    assert deltas["overall_score"] == expected_overall
    assert deltas["overall_score"][0] == 0
    for category in server.catalog.snapshot().score_categories:
        expected = [item["scores"][category] - items[0]["scores"][category] for item in items]
        assert deltas["score_categories"][category] == expected
        assert deltas["score_categories"][category][0] == 0


def test_compare_rejects_invalid_inputs() -> None:
    slugs = [row["slug"] for row in server.catalog.snapshot().rackets[:4]]
    for bad in ([slugs[0]], slugs[:4] + ["extra"], [slugs[0], slugs[0]], [slugs[0], "../bad"], [slugs[0], "missing"]):
        with pytest.raises(ToolError):
            server.compare_rackets(bad)


def test_brand_listing_is_deterministic_and_reveals_case_variants() -> None:
    first = envelope(server.list_racket_brands())["result"]["brands"]
    second = envelope(server.list_racket_brands())["result"]["brands"]
    assert first == second
    assert all(isinstance(brand, dict) for brand in first)
    assert [brand["normalized"] for brand in first] == sorted(brand["normalized"] for brand in first)
    nox = next(brand for brand in first if brand["normalized"] == "nox")
    # 2026-09-06: the catalogue was deduplicated to the majority spelling,
    # so the variant reveal lists a single display name; the mechanism (and
    # this pin) still surfaces any variant that reappears.
    assert nox["display_names"] == ["Nox"]
    assert nox["racket_count"] == sum(
        str(row.get("brand", "")).casefold() == "nox" for row in server.catalog.snapshot().rackets
    )


def test_get_offers_lists_filters_and_always_warns_prices_are_not_live() -> None:
    snapshot = server.catalog.snapshot()
    default = envelope(server.get_offers(limit=5))
    assert default["result"]["total_matches"] == len(snapshot.offers)
    assert default["result"]["offer_count"] == sum(
        len(snapshot.offer_lists.get(slug) or [offer]) for slug, offer in snapshot.offers.items()
    )
    assert [entry["slug"] for entry in default["result"]["items"]] == sorted(snapshot.offers)[:5]
    assert default["result"]["has_more"] is True
    assert default["result"]["applied_filters"] == {}
    assert server.NON_LIVE_OFFER_WARNING in default["warnings"]

    slug = sorted(snapshot.offers)[0]
    single = envelope(server.get_offers(slug=slug))
    entry = single["result"]["items"][0]
    assert entry["slug"] == slug
    assert entry["offers"] == snapshot.offer_lists.get(slug) or [snapshot.offers[slug]]
    assert entry["name"] == snapshot.by_slug[slug].get("name")
    assert single["result"]["applied_filters"] == {"slug": slug}

    retailer = snapshot.offers[slug]["retailer"]
    filtered = envelope(server.get_offers(retailer=retailer, limit=25))
    assert filtered["result"]["offer_count"] == sum(
        1
        for offers in snapshot.offer_lists.values()
        for offer in offers
        if offer["retailer"] == retailer
    )
    assert all(
        offer["retailer"] == retailer for entry in filtered["result"]["items"] for offer in entry["offers"]
    )
    assert filtered["result"]["applied_filters"] == {"retailer": retailer}
    assert server.NON_LIVE_OFFER_WARNING in filtered["warnings"]

    no_offers = envelope(server.get_offers(slug=next(row["slug"] for row in snapshot.rackets if row["slug"] not in snapshot.offers)))
    assert no_offers["result"]["items"] == []
    assert no_offers["result"]["total_matches"] == 0

    with pytest.raises(ToolError, match="unsupported"):
        server.get_offers(slug="../offers.json")
    with pytest.raises(ToolError, match="not found"):
        server.get_offers(slug="does-not-exist")
    with pytest.raises(ToolError, match="empty"):
        server.get_offers(slug="  ")
    for kwargs in ({"limit": 0}, {"limit": 26}, {"offset": -1}, {"offset": 9007199254740992}):
        with pytest.raises(ToolError):
            server.get_offers(**kwargs)


def test_envelopes_cite_methodology_and_concrete_review_urls_without_primary_claims() -> None:
    result = envelope(server.search_rackets(limit=3))
    citations = result["citations"]
    assert any(citation["url"] == result["dataset"]["methodology_url"] for citation in citations)
    review_urls = {item["review_url"] for item in result["result"]["items"]}
    cited_review_urls = {citation["url"] for citation in citations if citation["type"] == "locator" and "-review/" in citation["url"]}
    assert review_urls <= cited_review_urls
    assert all(citation["type"] == "locator" for citation in citations)
    assert not any("primary" in json.dumps(citation).casefold() for citation in citations)


def test_offer_is_consistent_across_search_get_compare_and_resources() -> None:
    snapshot = server.catalog.snapshot()
    slug = sorted(snapshot.offers)[0]
    raw = snapshot.by_slug[slug]
    expected = snapshot.offers[slug]

    searched = envelope(server.search_rackets(query=raw["name"], limit=25))["result"]["items"]
    assert next(item for item in searched if item["slug"] == slug)["offer"] == expected
    assert envelope(server.get_racket(slug))["result"]["offer"] == expected
    other = next(row["slug"] for row in snapshot.rackets if row["slug"] != slug)
    compared = envelope(server.compare_rackets([slug, other]))["result"]["items"]
    assert next(item for item in compared if item["slug"] == slug)["offer"] == expected
    assert json.loads(server.racket_resource(slug))["result"]["offer"] == expected
    dataset_item = next(item for item in json.loads(server.dataset_resource())["result"]["items"] if item["slug"] == slug)
    assert dataset_item["offer"] == expected
    serialized = json.dumps(dataset_item).casefold()
    assert not any(key in serialized for key in ("evidence", "collector_model", "source_run", "price_history"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("url", "https://example.com/not-padel-how/first-racket/", "invalid review URL"),
        ("scores", {"wrong_category": 8}, "scores must match score_categories"),
        ("score", 101, "total scores must be numeric values from 0 to 100"),
    ],
)
def test_catalog_rejects_invalid_identity_score_and_review_url_payloads(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    payload = json.loads(DATASET.read_text())
    payload["rackets"][0][field] = value
    copied = tmp_path / "rackets.json"
    copied.write_text(json.dumps(payload, ensure_ascii=False))
    with pytest.raises(ValueError, match=message):
        RacketCatalog(tmp_path).snapshot()


def test_fastmcp_registration_names_annotations_and_output_schemas() -> None:
    tools = server.mcp._tool_manager.list_tools()
    resources = server.mcp._resource_manager.list_resources()
    templates = server.mcp._resource_manager.list_templates()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert {str(resource.uri) for resource in resources} == EXPECTED_RESOURCES
    assert {template.uri_template for template in templates} == EXPECTED_TEMPLATES
    assert all(tool.annotations.model_dump(by_alias=True)["readOnlyHint"] is True for tool in tools)
    assert all(tool.annotations.model_dump(by_alias=True)["destructiveHint"] is False for tool in tools)
    assert all(tool.annotations.model_dump(by_alias=True)["idempotentHint"] is True for tool in tools)
    required_envelope = {"schema_version", "dataset", "tool", "result", "citations", "warnings"}
    expected_result_properties = {
        "search_rackets": {"items", "total_matches", "limit", "offset", "sort", "has_more", "applied_filters"},
        "get_racket": {"slug", "name", "brand", "year", "score", "review_url", "specifications", "scores"},
        "compare_rackets": {"items", "numeric_deltas"},
        "list_racket_brands": {"brands"},
        "get_offers": {"items", "total_matches", "offer_count", "limit", "offset", "has_more", "applied_filters"},
    }
    for tool in tools:
        schema = tool.fn_metadata.output_schema
        assert schema["type"] == "object"
        assert required_envelope <= set(schema["properties"])
        assert required_envelope <= set(schema["required"])
        result_schema = schema["properties"]["result"]
        if "$ref" in result_schema:
            result_schema = schema["$defs"][result_schema["$ref"].rsplit("/", 1)[-1]]
        assert expected_result_properties[tool.name] <= set(result_schema["properties"])
        assert expected_result_properties[tool.name] <= set(result_schema["required"])
        if tool.name == "get_racket":
            assert "offer" in result_schema["properties"]
            assert "offer" not in result_schema["required"]
    assert all("recommend" not in tool.name.casefold() for tool in tools)


def test_no_recommend_tool_is_registered() -> None:
    assert not any("recommend" in tool.name.casefold() for tool in server.mcp._tool_manager.list_tools())


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def running_server() -> str:
    port = unused_port()
    env = os.environ.copy()
    env.update({"PADEL_MCP_DATA_ROOT": str(PUBLIC), "PADEL_MCP_HOST": "127.0.0.1", "PADEL_MCP_PORT": str(port), "PYTHONPATH": str(ROOT)})
    process = subprocess.Popen(
        [sys.executable, "-m", "mcp_server.padel_mcp.server"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f"MCP subprocess exited: {stdout}\n{stderr}")
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.25)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("MCP subprocess did not become ready")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


async def exercise_streamable_http(url: str) -> None:
    async with streamable_http_client(url) as streams:
        read_stream, write_stream = streams[:2]
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert initialized.model_dump(by_alias=True)["protocolVersion"] in {"2025-11-25", "2026-07-28"}
            tools = await session.list_tools()
            resources = await session.list_resources()
            templates = await session.list_resource_templates()
            assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
            assert {str(resource.uri) for resource in resources.resources} == EXPECTED_RESOURCES
            assert {template.model_dump(by_alias=True)["uriTemplate"] for template in (templates.resource_templates if server.SDK_MODERN else templates.resourceTemplates)} == EXPECTED_TEMPLATES
            assert not any("recommend" in tool.name.casefold() for tool in tools.tools)

            first = server.catalog.snapshot().rackets[0]["slug"]
            calls = [
                ("search_rackets", {"limit": 2}),
                ("get_racket", {"slug": first}),
                ("compare_rackets", {"slugs": [row["slug"] for row in server.catalog.snapshot().rackets[:2]]}),
                ("list_racket_brands", {}),
                ("get_offers", {"limit": 5}),
            ]
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                assert result.model_dump(by_alias=True)["isError"] is False
                assert result.model_dump(by_alias=True)["structuredContent"] is not None
                envelope(result.model_dump(by_alias=True)["structuredContent"])

            fixed = await session.read_resource("padel://methodology/rackets")
            dataset = await session.read_resource("padel://dataset/rackets")
            templated = await session.read_resource(f"padel://rackets/{first}")
            assert json.loads(fixed.contents[0].text)["tool"] == "methodology_resource"
            assert len(json.loads(dataset.contents[0].text)["result"]["items"]) == CATALOGUE_SIZE
            assert json.loads(templated.contents[0].text)["result"]["slug"] == first


def test_real_streamable_http_subprocess_protocol_tools_and_resources(running_server: str) -> None:
    asyncio.run(exercise_streamable_http(running_server))


def test_health_route_host_and_origin_policy(running_server: str) -> None:
    root = running_server.removesuffix("/mcp")
    valid = httpx.get(f"{root}/healthz", headers={"Host": root.removeprefix("http://"), "Origin": root})
    invalid_origin = httpx.get(f"{root}/healthz", headers={"Origin": "https://evil.example"})
    invalid_host = httpx.get(f"{root}/healthz", headers={"Host": "evil.example"})
    assert valid.status_code == 200
    assert invalid_origin.status_code == 403
    assert invalid_host.status_code == 421


def test_raw_invalid_streamable_http_headers_are_rejected(running_server: str) -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}}}
    with httpx.Client(timeout=5) as client:
        invalid_accept = client.post(running_server, json=payload, headers={"Accept": "application/json"})
        invalid_content_type = client.post(
            running_server,
            content=json.dumps(payload),
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "text/plain"},
        )
    assert invalid_accept.status_code == 406
    assert invalid_content_type.status_code in {415, 400}

@pytest.mark.parametrize('protocol', ['2025-11-25','2026-07-28'])
def test_protocol_generation_matrix(running_server,protocol):
    headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':protocol}
    with httpx.Client(timeout=5) as client:
        if protocol=='2025-11-25':
            response=client.post(running_server,headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':protocol,'capabilities':{},'clientInfo':{'name':'local-test','version':'1'}}})
            assert response.status_code==200
            assert response.json()['result']['protocolVersion']==protocol
            assert response.json()['result']['serverInfo']['version']=='1.2.0'
        else:
            if not server.SDK_MODERN:pytest.skip('modern protocol requires SDK2')
            headers['Mcp-Method']='server/discover'
            response=client.post(running_server,headers=headers,json={'jsonrpc':'2.0','id':1,'method':'server/discover','params':{'_meta':{'io.modelcontextprotocol/protocolVersion':protocol,'io.modelcontextprotocol/clientCapabilities':{},'io.modelcontextprotocol/clientInfo':{'name':'local-test','version':'1'}}}})
            assert response.status_code==200, response.text
            assert 'error' not in response.json(),response.text
            assert protocol in response.json()['result']['supportedVersions']
        headers['Mcp-Method']='tools/list'
        response=client.post(running_server,headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{'_meta':{'io.modelcontextprotocol/protocolVersion':protocol,'io.modelcontextprotocol/clientCapabilities':{}}}})
        assert response.status_code==200,response.text
        assert {tool['name'] for tool in response.json()['result']['tools']}==EXPECTED_TOOLS
        assert 'mcp-session-id' not in response.headers

def test_modern_errors_and_request_bound(running_server):
    if not server.SDK_MODERN:pytest.skip('SDK2 transport matrix')
    headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2026-07-28','Mcp-Method':'tools/list'}
    with httpx.Client(timeout=5) as client:
        malformed=client.post(running_server,headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
        assert malformed.status_code==400 and 'error' in malformed.json()
        unknown=client.post(running_server,headers={**headers,'MCP-Protocol-Version':'2099-01-01'},json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{'_meta':{'io.modelcontextprotocol/protocolVersion':'2099-01-01','io.modelcontextprotocol/clientCapabilities':{}}}})
        assert unknown.status_code==400 and 'error' in unknown.json()
        oversized=client.post(running_server,headers={**headers,'Content-Type':'application/json'},content=b' '*1048577)
        assert oversized.status_code==413
        for method in ('GET','DELETE'):
            response=client.request(method,running_server,headers=headers)
            assert response.status_code in {400,405}
