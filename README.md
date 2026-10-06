# padel.how Racket Catalogue MCP

Read-only [Model Context Protocol](https://modelcontextprotocol.io) server for
the padel racket review catalogue of [padel.how](https://padel.how/): search,
compare and inspect 217 reviewed rackets with scores from one published
methodology, plus published retailer offers with the date each price was
observed.

- **Live endpoint (Streamable HTTP, no auth):** `https://padel.how/mcp`
- **Status:** `https://padel.how/mcp/status.json`
- **Client docs:** `https://padel.how/api/`
- **Methodology:** `https://padel.how/equipment/racket/methodology/`
- **Dataset:** `https://padel.how/rackets.json`

## Tools

| Tool | What it does |
|---|---|
| `search_rackets` | Literal, case-insensitive search over the catalogue: query, brand, year, score range, editorial player level, audience; sort and pagination (up to 25 per page). |
| `get_racket` | One racket by catalogue slug: scores, specs, review URL, methodology URL. |
| `compare_rackets` | Side-by-side comparison of 2–4 distinct rackets. |
| `list_racket_brands` | Brand names grouped by normalized spelling, with counts. |
| `get_offers` | Published retailer offers per racket (Amazon, Padel Market, PadelDogs, Tennis Point, Racketshop.com, SPORTLET); every price carries `observed_at`. |

All tools are annotated read-only, idempotent and closed-world.

## Resources

- `padel://dataset/rackets` — the complete published dataset
- `padel://methodology/rackets` — methodology metadata
- `padel://rackets/{slug}` — one published racket record

Every tool response carries a `dataset` block: source endpoint, generation
dates, record counts, `dataset_version` and methodology URL. Paginated calls
accept `snapshot_version` so a walk stays on one dataset. Unknown values stay
missing; nothing is estimated. The server gives no personal recommendations,
and offer prices are published figures, not live checkout prices.

## Client configuration

Any MCP client that supports Streamable HTTP:

```json
{
  "mcpServers": {
    "padel-how": {
      "url": "https://padel.how/mcp"
    }
  }
}
```

Claude Code:

```bash
claude mcp add --transport http padel-how https://padel.how/mcp
```

Clients that only speak stdio can go through
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):
`npx -y mcp-remote https://padel.how/mcp`.

## Run locally

The repository mirrors the production layout: the service lives in
`mcp_server/`, the published data bundle in `public/` (the same files
padel.how serves at `/rackets.json` and `/data/*.json`).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r mcp_server/requirements.lock -r mcp_server/requirements-dev.txt
.venv/bin/python -m pytest                                  # contract suite against the bundled data
PADEL_MCP_DATA_ROOT="$PWD/public" .venv/bin/python -m mcp_server.padel_mcp.server
curl -fsS http://127.0.0.1:8790/healthz
```

The server binds to `127.0.0.1:8790` and serves Streamable HTTP at `/mcp`.
Put your own reverse proxy in front for public exposure.

| Variable | Default | Meaning |
|---|---|---|
| `PADEL_MCP_HOST` | `127.0.0.1` | Bind address |
| `PADEL_MCP_PORT` | `8790` | Bind port |
| `PADEL_MCP_DATA_ROOT` | `public/` | Directory with `rackets.json` (and the optional `data/` bundle) |
| `PADEL_MCP_OFFER_PATH` | `mcp_server/data/offers.json` | Offers file used when the data root has no bundle |
| `PADEL_MCP_REQUIRE_BUNDLE` | unset | `1` refuses to start without a sealed bundle manifest |
| `PADEL_MCP_METRICS_ENABLED` | unset | `1` exposes aggregate tool metrics to loopback clients only |

When `public/data/bundle-manifest.json` is present, the server checks the
SHA-256 of every bundle file before serving it and keeps the last good bundle
if a reload fails. Telemetry is aggregate only: it never stores arguments,
URLs, IP addresses or messages.

## Data and affiliate disclosure

Retailer links in `get_offers` and in `public/data/offers.json` are affiliate
links; padel.how may earn a commission on purchases, and every offer carries a
`disclosure` field saying so. Each price comes with the date it was observed,
its currency and region, and is not a live price.

## License

The code is [MIT](LICENSE). The review data in `public/` and
`mcp_server/data/` belongs to padel.how and is included so the server runs out
of the box; its terms are at https://padel.how/copyright/. Quote scores, specs
and conclusions with attribution and a link to the review (`review_url` and
`methodology_url` in every response).
