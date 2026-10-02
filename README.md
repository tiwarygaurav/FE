# Urja Meter API

A clean, documented, read-only REST API over **Urja Meter Ops**, a legacy web portal that a
Jaipur electricity distribution utility uses to look up smart-meter details, where a meter
sits in the network, and its recent consumption. The portal has no API. This service logs in
as a normal user, speaks the portal's internal protocol, and serves the data as typed JSON,
together with things the portal can't do: filtering by any attribute, proximity search, a
reconstructed network, consumption roll-ups, data-quality and anomaly reports, and a web
dashboard.

| Deliverable | Where |
|---|---|
| How the portal works (auth, endpoints, quirks) | **[PROTOCOL.md](PROTOCOL.md)** |
| OpenAPI 3.1 description of *this* API | **[openapi.json](openapi.json)**, also rendered live at `/docs` (Swagger UI) and `/redoc` |
| Reflection | [REFLECTION.md](REFLECTION.md) |
| Investigation notes (how I reverse-engineered it) | [docs/investigation-log.md](docs/investigation-log.md) |
| Web client | `/app/` on the running service |

**Start reading the code here:** `portal/client.py` (the portal's protocol), `sync.py`
(freshness policy), `domain/network.py` (hierarchy reconstruction), `store.py` (the local
index) and `api/routes/meters.py` (the main endpoints), all under `src/urja_api/`.

---

## Quick start

Requirements: **Python 3.12+** and [uv](https://docs.astral.sh/uv/). uv can install Python
for you.

```bash
git clone https://github.com/tiwarygaurav/FE.git urja-meter-api && cd urja-meter-api
cp .env.example .env        # then set URJA_PORTAL_EMAIL / URJA_PORTAL_PASSWORD
uv sync                     # creates .venv with pinned dependencies (uv.lock)
uv run urja-api             # serves on http://127.0.0.1:8000  (--host/--port to change)
```

The portal credentials are the ones in the assignment brief. They are deliberately not
committed, and the server refuses to start while they are empty.

Without uv: `python -m venv .venv`, activate it, `pip install -e .`, then `python -m urja_api`.

What happens on start-up:

1. Reference data is synced from the portal's bulk export. This takes about 2 s, and
   `GET /v1/status` shows `index_ready: true` when it's done.
2. The readings cache warms in the background for all 403 meters. At a pace kept below the
   portal's rate limit this takes about 4 minutes, and it is repeated hourly. Per-meter
   endpoints work straight away, because they fetch on demand.
3. Everything is kept in `data/urja.sqlite3`, so after a restart the index is served at once
   and every cached series within about 3 s, even if the portal is down. The file is only a
   cache: if its schema version doesn't match the code, it's dropped and rebuilt from the
   portal.

Then open:
* <http://127.0.0.1:8000/app/>: the dashboard, map, network explorer and meter drill-down.
* <http://127.0.0.1:8000/docs>: interactive API docs.

**If `/v1/*` keeps answering `503 index_not_ready`,** the first sync failed. The problem's
`detail` says why (usually the credentials), and `GET /v1/status` has it under
`reference_data.last_run.error`. Fix `.env` and restart. A failed first sync is retried by
itself after 5 s, 10 s, 20 s and so on up to every 2 minutes, and `Retry-After` says when.

### Configuration

Settings come from environment variables or `.env`, both resolved relative to the directory
you run from. [.env.example](.env.example) lists them all with their defaults:

| Variable | Default | Effect |
|---|---|---|
| `URJA_PORTAL_EMAIL`, `URJA_PORTAL_PASSWORD` | (required) | The portal login. |
| `URJA_PORTAL_BASE_URL` | `https://urja-ops.flockenergy.tech` | The portal. |
| `URJA_PORTAL_TIMEOUT_S` | `10` | One HTTP request to the portal. |
| `URJA_PORTAL_MAX_WAIT_S` | `25` | Longest one portal call may take, retries included, before a `503` (3 s when a cached copy exists). |
| `URJA_PORTAL_RATE_LIMIT_PER_MINUTE`, `_BURST` | `100`, `10` | Our own pacing, below the portal's ~120 per minute. |
| `URJA_DB_PATH` | `data/urja.sqlite3` | The local index and readings cache. |
| `URJA_SYNC_INTERVAL_S` | `900` | Reference data re-sync interval. |
| `URJA_READINGS_TTL_S` | `900` | How long a meter's cached readings count as fresh. |
| `URJA_WARM_READINGS`, `URJA_WARM_INTERVAL_S` | `true`, `3600` | Background refresh of every meter's readings, and how often. |
| `URJA_API_KEY` | (unset) | If set, every `/v1/*` request needs it as the `X-API-Key` header. Printable ASCII, no spaces; blank means unset. |

Tests: `uv run pytest`. They run offline against an in-process fake portal. See
[Testing](#testing).

## Sample requests

```bash
# One meter: nameplate, location, network path, and what we had to correct
curl -s localhost:8000/v1/meters/J100400
```
```jsonc
{
  "meter_id": "J100400", "serial_number": "GN31592", "make": "Genus",
  "phase": "three", "status": "installed", "installation_type": "ct_operated",
  "transformer_code": "DT-007", "feeder_code": "F-007",
  "location": {"latitude": 26.882703, "longitude": 75.740158},
  "network": {
    "zone": {"code": "Z-01", "name": "Jaipur Zone 1"}, "circle": {"code": "C-01", "name": "Circle 1"},
    "division": {"code": "D-07", "name": "Division 7"}, "subdivision": {"code": "SD-07", "name": "Subdivision 7"},
    "substation": {"code": "SS-07", "name": "Substation 7"}, "feeder": {"code": "F-007", "name": "Feeder 7"},
    "transformer": {"code": "DT-007", "name": "Sanganer DT 7"}
  },
  "transformer": {"code": "DT-007", "name": "Sanganer DT 7", "feeder_code": "F-007", "capacity_kva": 63.0},
  "data_issues": [{
    "code": "stale_name", "level": "transformer", "message": "Portal reported an outdated transformer name.",
    "reported": "Old Malviya Nagar Xfmr", "resolved": "Sanganer DT 7"
  }],
  "readings_coverage": {"interval_minutes": 1440, "count": 30, "first_reading_at": "2026-06-01T00:00:00+05:30",
                        "last_reading_at": "2026-06-30T00:00:00+05:30", "fetched_at": "2026-10-01T16:43:34Z"},
  "synced_at": "2026-10-01T16:39:34Z"
}
```

```bash
# Daily consumption, derived from the cumulative kWh register
curl -s "localhost:8000/v1/meters/J100089/consumption?from=2026-06-28&to=2026-06-30"
```
```jsonc
{ "meter_id": "J100089", "granularity": "day",
  "start": "2026-06-28T00:00:00+05:30", "end": "2026-06-30T00:00:00+05:30",
  "last_reading_at": "2026-06-30T00:00:00+05:30", "total_kwh": 15.29,
  "buckets": [
    {"start": "2026-06-28T00:00:00+05:30", "end": "2026-06-29T00:00:00+05:30", "consumption_kwh": 7.65, "apparent_kvah": 8.25, "power_factor": 0.9273, "coverage": 1.0, "complete": true},
    {"start": "2026-06-29T00:00:00+05:30", "end": "2026-06-30T00:00:00+05:30", "consumption_kwh": 7.64, "apparent_kvah": 8.26, "power_factor": 0.9249, "coverage": 1.0, "complete": true}
  ],
  "freshness": {"fetched_at": "2026-10-01T16:39:49Z", "stale": false} }
```

The window asked for 30 June too, but that day can't be computed: it would need a reading
on 1 July, and the data ends at 30 June 00:00. Buckets stop where the cached data does.

More examples:

```bash
curl -s "localhost:8000/v1/meters?status=faulty&phase=three&zone=Z-01"           # filter on anything
curl -s "localhost:8000/v1/meters?near=26.9124,75.7873&radius_km=1.5"            # what's near a point
curl -s "localhost:8000/v1/meters/J100000/readings?from=2026-06-10&to=2026-06-10" # 48 half-hourly readings
curl -s "localhost:8000/v1/network/division/D-01"                                # one node, all its parents
curl -s "localhost:8000/v1/insights/consumption?group_by=status&from=2026-06-01&to=2026-06-29"
curl -s "localhost:8000/v1/insights/anomalies?severity=error"
```

## The API

All responses are JSON. Full schemas are in [openapi.json](openapi.json).

| Endpoint | What it gives you |
|---|---|
| `GET /v1/meters` | List and filter meters by `q` (id/serial substring), `status`, `make`, `phase`, `installation_type`, any network code (`zone`…`feeder`, `transformer`), `has_issues`, or `interval_minutes` (30 = half-hourly, 1440 = daily). Supports `near=lat,lng&radius_km=` and `bbox=` (GeoJSON order), `limit`/`offset` paging, and `sort=` (see below). Each item carries its `data_issue_count` and reading interval. |
| `GET /v1/meters/{id}` | Nameplate, location, full network path, transformer, `data_issues` (what the portal said vs what we serve), and what the readings cache holds for the meter. |
| `GET /v1/meters/{id}/readings` | Register readings as numbers with ISO 8601 (+05:30) timestamps. Each reading carries `consumption_kwh` since the previous one and quality flags. Includes a summary and freshness. |
| `GET /v1/meters/{id}/consumption` | Energy per IST day or hour, taken from the registers at the bucket boundaries, with `complete` and `coverage` per bucket. |
| `GET /v1/meters/{id}/anomalies` | Every anomaly rule (below) run over that meter's readings. |
| `GET /v1/transformers`, `/{code}` | Distribution transformers: capacity, network path, meter counts by status, name variants, and their meters. |
| `GET /v1/network` | Shape of the reconstructed network: node counts per level, which parent/child edges are functional, `is_tree`. |
| `GET /v1/network/tree` | A drill-down tree, zone → transformer, with counts that add up. |
| `GET /v1/network/{level}`, `/{level}/{code}` | Nodes, identified by (level, code), with their parents, children, transformers and meter counts. |
| `GET /v1/data-quality` | Every correction applied to the portal's data, structural findings, and how many meters trip each data-integrity rule. |
| `GET /v1/insights/consumption` | Consumption over complete days, grouped by any network level, status, make, and so on. `group_by=meter` with `limit` gives the top consumers; `groups_total` says how many groups there are. |
| `GET /v1/insights/anomalies` | Fleet-wide anomaly counts per rule and severity, and the meters worth a look. Meters that are merely `stale` are counted but only listed with `rule=stale`. `include_meters=false` gives counts only. |
| `GET /v1/status` | Freshness of reference data and the readings cache, warm-up progress, and portal client counters (logins, retries, 429s, session expiry, clock skew). |
| `POST /v1/sync` | Re-sync reference data now. Joins a sync that is already running, and returns the previous result if one succeeded in the last 30 s. |
| `GET /healthz` | Liveness. |

**The query contract.**
* Meter ids and network codes are case-insensitive (`j100000` works).
* **Unknown query parameters are a `422`**, with a suggestion: `transformer_code=DT-007` on
  `/v1/meters` answers "did you mean `transformer`?" instead of returning the whole fleet.
* `sort` keys: `meter_id`, `serial_number`, `make`, `status`, `phase`, `installation_type`,
  `transformer`, `feeder`, `issues`, `interval`, `distance` (needs `near=`). The response's
  field names (`transformer_code`, `data_issue_count`, …) work too. Prefix `-` for
  descending; ties are broken by `meter_id`.
* `interval_minutes` and `sort=interval` only know meters whose readings are cached, which is
  all of them once the first warm-up pass has finished (`/v1/status` shows the progress).
* `from`/`to` take `YYYY-MM-DD` or an ISO 8601 datetime between 2000 and 2100. Datetimes in
  any offset are converted to IST, and naive ones are read as IST. A bare date as `to`
  covers that whole day. With neither, the window is the 7 days up to the meter's latest
  reading (the portal's own default, relative to the *data*); with only `to`, the 7 days
  ending there. Consumption buckets cover every hour or day the window touches, as far as
  the cached data reaches; a `to` exactly on a bucket boundary closes the bucket before it.
* How many meters or transformers a record covers is always `meter_count` /
  `transformer_count`, and a plain `meters` is always a list. Totals keep descriptive names
  (`meters_total`, `meters_analysed`, `meters_with_issues`).
* If `URJA_API_KEY` is set, every `/v1/*` endpoint requires an `X-API-Key` header.
  `/healthz` and the docs stay open.

**Errors** are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) `application/problem+json`
with a stable `code`. Every `503` carries `Retry-After`.

| `code` | Status | When |
|---|---|---|
| `unauthorized` | 401 | `X-API-Key` is missing or wrong (only when `URJA_API_KEY` is set). |
| `meter_not_found` | 404 | No meter with that id in the index, or the portal no longer has it. |
| `transformer_not_found` | 404 | No distribution transformer with that code. |
| `network_node_not_found` | 404 | No network node with that level and code. |
| `not_found` | 404 | No such path. |
| `method_not_allowed` | 405 | The path exists, but not for this HTTP method. |
| `http_error` | 4xx | Any other HTTP-level error. |
| `validation_error` | 422 | A parameter is malformed, out of range or not recognised; `errors` says which. |
| `granularity_unavailable` | 422 | Hourly consumption asked of a meter that reports once a day. |
| `internal_error` | 500 | A bug in this service; the server log has the details. |
| `upstream_auth_failed` | 502 | This service could not log in to the portal. |
| `upstream_protocol_error` | 502 | The portal answered in a format this service does not understand. |
| `sync_rejected` | 502 | A sync returned data that looked broken (say, most of the meters or all of their locations missing); the previous snapshot is kept. |
| `index_not_ready` | 503 | The first sync with the portal hasn't finished. |
| `upstream_unavailable` | 503 | The portal is down or too slow, and there is no cached copy to serve. |
| `upstream_rate_limited` | 503 | The portal kept answering 429, and there is no cached copy to serve. |
| `busy` | 503 | Too many requests are queued for the portal's rate budget (our own queue, not a 429). |

**Anomaly rules.** Each rule is proven by an injected-fault test in `tests/test_anomalies.py`.

| Rule | Severity | Fires when |
|---|---|---|
| `duplicate_timestamps` | info | The portal sent the same timestamp twice (merged into one reading). |
| `conflicting_duplicates` | warning | …with different values. |
| `missing_values` | warning | A reading has a blank kWh register or voltage. |
| `gaps` | warning | Readings are missing, given the meter's interval. |
| `register_decrease` | error | The kWh register fell more than 0.02 kWh below its highest value so far. |
| `implausible_load` | error | Average load above what the meter class can carry (60 A single-phase or 100 A three-phase whole-current; 400 A per phase CT-operated). |
| `flatline` | warning | No consumption for 24 hours or more (not checked for decommissioned meters). |
| `consumption_surge` | warning | A complete day above 5× the meter's median day (and above 5 kWh). |
| `power_factor_above_one` | error | On a complete day the kVAh register rose less than the kWh register. |
| `no_voltage` | error | Voltage below 50 V. |
| `voltage_outside_10pct` | error | Voltage outside 207–253 V (±10 % of 230 V). |
| `voltage_outside_6pct` | warning | Voltage outside the statutory 216.2–243.8 V band (±6 %), but within ±10 %. |
| `decommissioned_reporting` | error | A decommissioned meter's register rose by more than 0.02 kWh. |
| `stale` | warning | No reading for two intervals, and at least two days. |

## How it works

```text
 HTTP clients / web app
          │
          ▼
  FastAPI routes (api/) ── problem+json errors, validation, optional API key
          │ reads                                   ▲ on-demand readings
          ▼                                         │
  SQLite local index (store.py) ◄── SyncService (sync.py)
   meters · R*Tree geo index ·       ├─ every 15 min: signed export → normalise → reconcile network → replace snapshot
   transformers · network paths ·    ├─ per meter: fetch readings when older than the TTL (stale-if-error)
   readings cache · sync runs        └─ background warm-up of all readings, paced under the rate limit
                                            │
                                            ▼
                              PortalClient (portal/) ── login + session renewal, soft-redirect detection,
                                            │            token bucket + 429 back-off, retries, HMAC signing
                                            ▼
                                  Urja Meter Ops portal
```

| Module | Responsibility |
|---|---|
| `portal/` | Everything that knows the portal's wire protocol: `client.py` (sessions, throttling, retries, typed endpoint calls), `devalue.py` (SvelteKit's encoding), `signing.py` (export HMAC), `ratelimit.py` (token bucket), `errors.py`. |
| `domain/` | Pure logic with no I/O: `models.py` (the public contract), `normalize.py` (portal shapes → records), `network.py` (hierarchy reconstruction), `readings.py` (duplicate merging, consumption), `anomalies.py` (rules). |
| `store.py` | SQLite schema and queries: filters, R*Tree + haversine proximity search, readings cache. |
| `sync.py` | Freshness policy: reference sync with fallback, readings TTL, stale-if-error, warm-up. |
| `derived.py` | Fleet-wide views over the cache (parsed series, daily buckets, anomaly results), memoised per cache version, and the aggregations behind `/v1/insights`. |
| `config.py` | Settings (see [Configuration](#configuration)). |
| `api/` | FastAPI app, routes, error mapping, request parsing. |
| `web/` | The single-page web client (plain ES modules, no build step). |

## Design decisions and trade-offs

**A synced local index instead of a pass-through proxy.** The portal can only look meters
up by id or serial, 20 at a time, and every `/portal/*` call shares a budget of 120 requests
per minute. A pure proxy would be slow, fragile, and unable to answer "all faulty
three-phase meters on feeder F-007" or "what's near here". The whole reference dataset
comes from **one** signed export call, so syncing it every 15 minutes costs the portal
almost nothing. It also gives consistent snapshots and keeps the service working when the
portal is down. *Cost:* data can be up to one sync interval old. That staleness is bounded,
configurable and visible (`/v1/status`, each record's `synced_at`, `freshness`).

**The export is the system of record, with a crawl fallback.** Every view of a meter agrees
on its nameplate (403/403), but the detail page is *lossy*: it drops codes when a name is
blank. The export is complete, structured and cheap. If it ever answers in a way we don't
understand (for example the signing scheme changes), the sync falls back to the search
listing plus detail pages. That costs 21 budgeted search calls; detail pages aren't
counted. I verified it rebuilds an identical network. Detail pages carry no coordinates, so
each meter keeps its last known location. An export *outage* is not a reason to crawl: the
last snapshot is kept and the next sync tries again.

**Readings: fetched per meter, cached per meter, served stale rather than failing.** A
meter's entire history is one request of up to about 118 KB. The cached series is served
while it's younger than `URJA_READINGS_TTL_S`. After that it's refetched, and concurrent
requests for the same meter share one upstream call and its outcome. If the refresh fails:
* with a cached copy, the copy is served with `freshness.stale: true` within about 3 s, and
  for the next minute that meter is answered from the cache straight away, without asking
  the portal again;
* a payload we can't read (timestamps or kWh values that no longer parse) never replaces
  a good series. Nor does a shorter or newly blank one, unless the portal sends that same
  payload three times running: then it is taken to be the portal's data, not a glitch.
  Readings dated in the future are dropped; a register that is simply blank is served as
  it is, flagged;
* with nothing cached, the caller gets a `503` once that one fetch has used up
  `URJA_PORTAL_MAX_WAIT_S`, with `Retry-After`, a courtesy the portal itself doesn't offer.

*Trade-off:* "fetch the whole history" is right for a month of data. With years of
half-hourly data I'd cache in month chunks, where closed months are immutable and only the
current month has a short TTL.

**The network is anchored on the transformer, not forced into a tree.** The portal's
hierarchy fields aren't a tree: every division has three parent circles, for example (see
[PROTOCOL.md § Data quality](PROTOCOL.md#data-quality)). I considered three models:
* a majority-vote tree, which rewrites 92% of meters' paths on coin-flip ties;
* path-identity nodes, which split one division into three entities;
* what the data supports.

Every meter on a transformer reports the same path, so that path is the unit of truth.
Blanks are repaired from it, and every repair is reported. Nodes are identified by
`(level, code)`, list *all* their parents with counts, and are aggregated from the meters'
own codes, so nothing is double-counted. `/v1/network` says `is_tree: false` rather than
hiding it. `/v1/network/tree` offers a drill-down view for humans.

**Consumption is derived, never guessed.** kWh and kVAh are cumulative registers. The
portal UI labels them "Consumption"; summing that column overstates a meter's June usage by
72× to 227,000× (median about 1,900×), depending on the meter.
* Consumption is a register difference, measured against the register's highest value so
  far. Values are rounded to 0.01, so a reading up to 0.02 below that mark counts as no
  consumption, and energy counts again only once the register passes it: read noise can't
  create energy, and the steps always add up to the register difference. Further below the
  mark, the register has really gone down, which is flagged rather than counted. Readings,
  consumption buckets and the anomaly rules share this one rule (`domain/readings.py`).
* A day needs a reading at both midnights, or it's marked `complete: false`, with a
  `coverage` fraction and no interpolation. A window's `total_kwh` is the register
  difference across it, which loses nothing to missing readings in between.
* Power factor (kWh/kVAh) is only reported when there is at least 1 kVAh of apparent energy
  behind it. Below that, the 0.01 resolution distorts the ratio by 1% or more. In practice
  days get one and half-hours don't.

**Robustness lives in one place.** `PortalClient` handles all of this, and the test suite
covers each case against the fake portal:
* login and renewal before the one-hour hard expiry; re-login when an expired session shows
  up as a 401 *or* as SvelteKit's HTTP-200 soft redirect; one login even under concurrency;
* a cool-down after a rejected or unintelligible login, or a session the portal refuses
  straight away (60 s, doubling to 15 minutes), instead of hammering the login form. A login
  that fails with a 5xx or network error is retried with back-off like any other outage;
* a token bucket for every `/portal/*` call, kept below the limit, and a global pause on
  429. A 429 elsewhere only delays that request;
* bounded, jittered retries for 5xx and network errors, under **one deadline per request**
  that covers login, queueing, retries and hung connections;
* honest error attribution: an outage stays `upstream_unavailable` even when our own
  throttle ends the retries, a wait caused by the portal's 429s is `upstream_rate_limited`,
  and only our own queue backing up is `busy`;
* payloads checked for shape at the boundary, so format drift is a clean
  `upstream_protocol_error` (and a cached copy is served) rather than a crash; error text
  never echoes credential-bearing payloads;
* refreshing the signing secret if a signature is rejected, and correcting signing
  timestamps by the clock skew measured from the portal's `Date` header;
* signing out on shutdown.

Store calls made from the event loop run in worker threads, as do the snapshot rewrite and
the fleet-wide insights, so `/healthz` and cheap requests stay responsive.

**Types are the contract.** Pydantic models define the API, and FastAPI generates
`openapi.json` from them (`uv run python scripts/export_openapi.py`). Closed vocabularies
(statuses, rules, severities, flags, group-by keys) are enums in the spec. A test fails if
the committed file drifts from the code, and another if an error code is missing from the
table above. Values the portal has never sent map to an `unknown` enum member, with a
`data_issue` holding the raw value, rather than breaking the sync or dropping the meter.

**Anomaly rules come from physics, not from this data.** The readings are synthetic straight
lines, so nothing could be tuned on them. The thresholds are India's ±6% statutory LV
voltage band (±10% counted as abnormal), the most a meter class can physically carry, and
the 0.02 kWh rounding tolerance, applied to *cumulative* change so that a slow drift is
caught even when every single step is within it. On the real data three rules fire:
* **decommissioned meters consuming energy** (all 75, 18.3% of June energy), the headline
  operational finding;
* duplicate timestamps (5 meters);
* stale data (the portal's data stops on 30 June).

## Optional extensions

* **Network hierarchy:** reconstructed and repaired, with provenance. See above and
  `/v1/network*`.
* **Local index / query layer:** SQLite with an R*Tree spatial index. It filters on every
  attribute and network level, and answers "what's near this location?" and fleet-wide
  consumption and anomaly questions. *Where it would struggle* is covered in
  [Scale](#scale-where-this-approach-would-struggle).
* **Freshness / caching:**
  * Reference data is re-synced every 15 minutes. An unchanged export is recognised by its
    hash and not rewritten. If a sync fails, or looks broken (for example it would shrink
    the index by more than half), the last good snapshot keeps being served.
  * Readings have a 15-minute TTL per meter, with stale-if-error. On top of that, an
    hourly background pass refreshes every meter under the rate limit.
  * Staleness is always visible (`/v1/status`, `synced_at`, `freshness`).

  The TTLs are short because nothing tells us when the portal's data changes: there are no
  ETags and no change feed. The data never changed during an hour of observation, but a
  15-minute bound on staleness costs the portal only about 4 requests per sync.
* **Robustness:** see above. `/v1/status` exposes the client's counters for operations.
* **The full dataset:** the bulk export path, used properly. All 403 meters come in one
  call, instead of about 424 budgeted requests (21 search pages and 403 `/geo` calls) plus
  403 detail pages. The export ignores paging; we still send the UI's `page=1`, because it
  is part of the signed string. A rate-limit-paced warm-up collects every meter's readings.
* **Web client** at `/app/`:
  * a dashboard with fleet KPIs and the decommissioned-consumption finding;
  * a map with status colours and radius search;
  * meter drill-down with a daily consumption chart and half-hourly load profile;
  * network explorer, transformer loading, and data quality views.

  It uses plain ES modules, Leaflet and Chart.js from a CDN, and has no build step.

## Scale: where this approach would struggle

Today's fleet is 403 meters, and everything here is instant at that size. To find the
limits, `scripts/bench_geo.py` loads synthetic fleets into the real `Store` and times the
real `Store.search_meters` proximity query. It also cross-checks every result against a
brute-force scan, which is what caught a bounding-box bug, since fixed.

```bash
uv run python scripts/bench_geo.py --max 100000   # 403, 10k and 100k meters; omit --max to add 1M
```

| size | build s | re-sync s | near 0.5 km p50/p95 ms | near 2 km p50/p95 ms | avg hits (0.5 / 2 km) | naive scan p50 ms (2 km) |
|---:|---:|---:|---:|---:|---:|---:|
| 403 | 0.03 | 0.03 | 0.07 / 0.17 | 0.16 / 0.41 | 0.5 / 6.7 | 0.67 |
| 10,000 | 2.66 | 2.30 | 0.99 / 9.47 | 8.51 / 21.9 | 10.9 / 166.1 | 27.1 |
| 100,000 | 54.61 | 61.31 | 6.07 / 7.15 | 159 / 363 | 112.1 / 1,658.9 | 337 |

*Python 3.12, SQLite 3.50, Windows 11, Ryzen 5 5600H laptop, while the machine was busy with
other work (CPU 70–99%), so treat absolute times as pessimistic: a later re-run on the
same machine got 3.1 s / 80 s re-syncs and a 102 ms 2 km p50 at 100k. `build` is the first
`replace_reference_data`; `re-sync` is the same call into a populated store, as for every
later sync; `near` is `Store.search_meters` at 50 random points; `naive` is a pure-Python
haversine scan. Every result agreed with the naive scan (100/100 at each size).*

What that means, roughly in the order it would bite:

* **Each sync rewrites the whole snapshot**: about 2–3 s at 10k meters and about a minute
  at 100k. It runs in a worker thread, so the event loop stays responsive, but reads share
  the store's single connection and wait for the write. Past a few tens of thousands of
  meters I'd switch to incremental upserts by `meter_id` (or build-and-swap into a new file)
  plus separate read connections. (An unchanged export already skips the rewrite.)
* **Proximity search** costs roughly one row lookup per *candidate in the bounding box*
  (density × (2r)²), not per meter in the table. At 100k meters a 2 km search takes about
  0.1–0.16 s here; a whole-city radius means ranking every meter in Python. The next steps
  would be ranking in SQL with SQLite's math functions (reading full rows only for the page
  returned), `PRAGMA mmap_size`, and a cap on `radius_km` or the candidate count.
* **Fleet-wide insights** are memoised per cache version: about 1 s to build at 403 meters,
  then about 20 ms. At thousands of meters with years of half-hourly data (more than 10⁸
  readings) I'd maintain pre-aggregated daily tables at write time, or move to a columnar
  store.
* **One process, one SQLite file, one rate limiter.** A second instance would double the
  load on what is probably one shared portal budget. Horizontal scaling needs a single sync
  worker, a shared limiter (Redis), and PostgreSQL + PostGIS (`geography` + GiST,
  `ST_DWithin`, KNN with `<->`), with H3 cells for spatial roll-ups.

## Assumptions

* **Timestamps are IST (UTC+05:30).** The portal sends none. The data aligns to local
  midnight and the utility is in Jaipur. India has no DST, so a fixed offset is exact.
* **Registers:** `kwh` and `kvah` are cumulative registers read at the timestamp. There is
  no multiplying factor or CT ratio (the portal exposes none).
* **Coordinates** are WGS84 (the portal states no datum). They are served but not used to
  infer anything.
* **"Recent"** means the latest data the portal has. The default readings window is the last
  7 days of data, as in the portal. `last_reading_at` makes clear when that is (currently
  June 2026, not "this week").
* **Canonical values:** the DT list is canonical for transformer names and feeders. Majority
  agreement among a transformer's meters is canonical for the levels above.
* **Read-only, polite use:** one service instance, using the supplied account, self-limited
  to 100 of the portal's roughly 120 requests per minute.

## What I intentionally left out

* **Writes of any kind**, because the brief is read-only.
* **Proxying the portal's search.** Local search is faster and escapes `%`/`_` properly.
* **The `/geo` endpoint.** It's redundant with the export, and rate limited.
* **Transformers without meters.** A transformer's place in the network is only known from
  its meters, so one that has none isn't served (the sync says so). On the live portal
  every transformer has meters.
* **Derived transformer locations.** Averaging meter coordinates would be misleading,
  because the portal's coordinates are noise. PROTOCOL.md has the statistics.
* **Dashboard-only figures in the API.** A few numbers on the dashboard are worked out in
  the browser from API responses: decommissioned energy (two `/v1/insights/consumption`
  calls subtracted), a transformer's average load against its rating, and the "flat
  profile" hint in the meter drawer. They belong in the API with tests; see the next
  section.
* **Real authentication** (OAuth/OIDC). There's an optional shared API key. A real
  deployment would sit behind the organisation's gateway.
* **Multi-instance deployment, metrics and tracing.** Logs and `/v1/status` counters are
  enough for one instance.
* **A container image.** uv gives a reproducible one-command setup, and I preferred not to
  ship a Dockerfile I couldn't run here.

## What I'd improve with more time

1. Incremental sync that diffs each export against the previous snapshot and emits
   change events (status changed, meter moved transformer), instead of replacing the
   snapshot.
2. Month-chunked readings cache, plus pre-aggregated daily consumption tables, so insights
   don't recompute from raw readings.
3. A bulk time-series endpoint (daily consumption for many meters in one call) served from
   the memoised daily buckets, and the dashboard's own derivations (decommissioned energy per
   group, transformer loading) moved into the API.
4. A nightly contract test against the live portal. It would detect protocol drift (new
   nameplate format, signing change) before users do. The opt-in live test in
   `tests/test_live.py` is the start of it.
5. Prometheus metrics and structured logs.
6. Cursor pagination and ETags on list endpoints.
7. Playwright tests for the web client.
8. A published container image.

## Testing

```bash
uv run pytest                              # about 420 tests, offline, about half a minute
URJA_LIVE_TESTS=1 uv run pytest -m live    # opt-in smoke test against the real portal (reads .env)
uv run ruff check . && uv run ruff format --check .
```

* **Unit tests** run on real portal payloads in `tests/fixtures/`: a curated subset copied
  verbatim from a live capture. They cover:
  * the devalue decoder (including malformed payloads), signing, and the token bucket;
  * normalisation of both nameplate formats, every hierarchy label form, and impossible dates;
  * network reconstruction and duplicate merging;
  * consumption across blank values, gaps, read noise and a register running backwards;
  * the store: sorting, filters, schema rebuild, and both edges of a proximity search;
  * the fleet aggregations behind `/v1/insights`.
* **Fault-injection tests** cover every anomaly rule. Each fault fires exactly its own rule,
  including slow drifts that stay inside the rounding tolerance at every step.
* **Client robustness tests** run against `tests/fake_portal.py`, an in-process imitation of
  the portal and its quirks, including its shared rate budget. They cover session expiry
  through both the 401 and the soft-redirect paths, one re-login under concurrency, the
  login cool-down, hung connections cut at the deadline, 429 storms, 5xx retries, error
  attribution, unexpected payload shapes, a rotated signing secret, and clock skew.
* **Sync tests** cover the export path, the crawl fallback (rebuilding an identical network,
  keeping locations, and keeping the snapshot when pages are down), the guards against a
  snapshot that lost meters, locations or transformer details, stale-if-error and its
  one-minute memory, refusing unreadable or shorter series, skipping bad records, the
  warm-up's failure accounting, quick retries of a failed first sync, shutdown, and memo
  invalidation.
* **End-to-end API tests** run the real app against the fake portal. They cover every
  endpoint and filter, the query contract (unknown parameters, dates, offsets), the
  problem+json shape and every mapping to a status and `Retry-After`, and the API key.
* **OpenAPI checks** keep `openapi.json` in sync with the code and every error documented.

While building, I also broke behaviours on purpose (about 60 hand-made mutations) to check
that some test failed each time. That harness isn't in the repository, so read this as a
note on method rather than a reproducible result.
