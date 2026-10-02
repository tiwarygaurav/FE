# PROTOCOL.md: how the Urja Meter Ops portal actually works

This is what I found by reading the portal's client code and probing it as the supplied
user: GET requests plus the authentication endpoints (the login form, better-auth's
session and sign-in/sign-out calls). Nothing was written. The longer version, including the
dead ends, is in [docs/investigation-log.md](docs/investigation-log.md). Everything below
was observed on the live portal between 2026-09-30 20:42 and 21:46 UTC, unless marked as
an inference.

## TL;DR

* It's a **SvelteKit** app. Authentication uses **better-auth**, with a cookie session that
  lasts exactly **one hour** and does not slide.
* The browser gets its data from a small **hidden JSON API** under `/portal/*`, plus
  SvelteKit's `__data.json` page loads (encoded with **devalue**).
* There is a **bulk path**. `GET /portal/export` returns *every* meter with its hierarchy
  and coordinates in one response. It needs an **HMAC-SHA256 signature**, computed with
  a secret that the portal itself hands out at `GET /portal/keys`.
* **Energy readings** only exist per meter. They are **cumulative registers**, not
  consumption.
* **Every `/portal/*` call counts against one rate-limit budget** of 120 requests per 60 s.
  A 429 comes with no `Retry-After`.
* The data has deliberate-looking traps:
  * two nameplate formats;
  * a detail view that drops codes;
  * blank and stale hierarchy values, and a hierarchy that is **not a tree**;
  * duplicate blank rows;
  * a readings window whose default is relative to the *data*, not to today;
  * "decommissioned" meters that are still consuming.

## 1. Application shape

The login page ships `X-Sveltekit-Page: true` and `_app/immutable/*` bundles. The route
manifest in `_app/immutable/entry/app.*.js` lists every page. A negative leaf `n` means node
`~n` has a server `load`.

| Route | Node | Data comes from |
|---|---|---|
| `/` | 3 | server redirect → `/meters` (or `/login`) |
| `/login` | 7 | server load + form action (`POST /login`) |
| `/(portal)/meters` | 5 | **client-side** `fetch` → `/portal/meters/search` |
| `/(portal)/meters/[id]` | 6 | server load → `/meters/{id}/__data.json`, plus client `fetch` → `/geo`, `/energy` |
| `/(portal)/transformers` | 4 | **client-side** `fetch` → `/portal/dts`; export button → `/portal/keys` + `/portal/export` |

The `(portal)` group layout (node 2) has a server load that returns the signed-in user
`{name, email}` and acts as the auth guard. JSON endpoints under `/portal/*` and
`/api/auth/*` don't appear in the manifest. I found them by reading the page bundles, which
contain every `fetch` call the UI makes: 8 application calls, and no WebSocket or SSE.

## 2. Authentication and sessions

### Login is a SvelteKit form action

```http
POST /login
Origin: https://urja-ops.flockenergy.tech        ← required (SvelteKit CSRF check)
Content-Type: application/x-www-form-urlencoded
Accept: application/json
x-sveltekit-action: true                         ← ask for the JSON action result

email=operator%40urja.local&password=…
```

| Outcome | HTTP | Body |
|---|---|---|
| success | **200** | `{"type":"redirect","status":303,"location":"/meters"}` + `Set-Cookie: __Secure-better-auth.session_token=<token>.<sig>; Max-Age=3600; Path=/; HttpOnly; Secure; SameSite=Lax` |
| wrong password | **200** | `{"type":"failure","status":401,"data":"[{\"email\":1,\"error\":2},\"operator@urja.local\",\"Invalid email or password.\"]"}` (the `data` string is devalue) |
| `Origin` missing or foreign | 403 | `{"message":"Cross-site POST form submissions are forbidden"}` |

A login client therefore has to read `type`. The HTTP status says 200 either way. The
response format follows content negotiation. A post without `x-sveltekit-action` but with
`Accept: */*` still got this JSON. A browser's `Accept: text/html` should get SvelteKit's
real 303, but that's framework behaviour I didn't test.

### Session facts (from `GET /api/auth/get-session`)

* `expiresAt = createdAt + 1 h`. `updatedAt` never moved while the session was in use, so the
  session is **fixed-lifetime, not sliding**. A long-running client has to log in again.
* Several sessions can be live at once. Logging in again doesn't revoke older ones.
* `POST /api/auth/sign-out` (with `Origin`) really revokes the session server-side.
* better-auth's own `POST /api/auth/sign-in/email` (JSON body) also works, but the UI
  doesn't use it, so I don't rely on it.

### How "not logged in" shows up depends on the route

| Situation | `/portal/*` JSON | `/meters/{id}/__data.json` | HTML pages |
|---|---|---|---|
| no / expired / revoked session | `401 {"error":"unauthorized"}` | **`200 {"type":"redirect","location":"/login"}`** | `302 /login` |

The `__data.json` case is a "soft redirect": HTTP 200 with the redirect in the body. A
client that only checks status codes will treat an expired session as success.

## 3. Endpoints

All of these need the session cookie. IDs in paths are **case-sensitive** (`j100000` → 404),
but search is case-insensitive.

### `GET /portal/meters/search?q=&page=`: meter listing

`{"data":[{meterId, serialNo, make, phaseType, installStatus, dtCode}], "total":403, "page":1, "pageSize":20}`

* The match is a case-insensitive **substring on meter id and serial only**. It doesn't
  search make, status or DT. Whitespace isn't trimmed.
* `q` is fed into a SQL `LIKE` **unescaped**: `%` returns all 403 meters. By the same
  logic `_` should match any single character, but that is an inference I didn't test.
* Serials such as `L&84997` contain `&`. An unencoded `q=L&84997` silently becomes `q=L`
  (167 hits).
* Page size is fixed at 20, and `pageSize` / `limit` are ignored.
  * Pages below 1 and non-numeric pages give page 1.
  * `page=1.5` starts at row 10.
  * Paging past the end returns `data: []` with the true `total`, not a 404.

### `GET /portal/dts?page=`: distribution transformers

`{"data":[{code, name, feederCode, capacityKva}], "total":40, "page", "pageSize":20}`

This is the **only source of transformer capacity** and the canonical transformer
names. Note that `capacityKva` is a number here, while most other numbers are strings.

### `GET /meters/{id}/__data.json`: meter detail (SvelteKit page data)

```json
{"type":"data","nodes":[null, {layout: user}, {"type":"data","data":[{"meterId":1,"detail":2,"hierarchy":…}, "J100000", …]}]}
```

* **devalue encoding.** Each node's `data` is a flat array. Element 0 is the root, and
  object values are *indices* into the same array. Shared strings (the meter id, the
  status) appear once and are referenced several times.
* **There are two nameplate formats.** The export's `build` field predicts which one a meter
  uses (238/238 and 165/165):
  * `legacy`: `detail.data = [{parameterName:"Meter ID", parameterValue:"J100000"}, …]` with
    six labels: Meter ID, Serial No, Make, Phase Type, Installation Status, Installation Type.
  * `v2`: `detail.classData = "{\"installed_meter\":{\"MeterId\":…,\"SerialNo\":…,…}}"`. This is
    a JSON *string* inside the payload, with PascalCase keys.
* **The hierarchy is a flat object of display strings.** It has keys `Zone`, `Circle`,
  `Division`, `Subdivision`, `Sub Station`, `Feeder` and `DT`, plus three non-level keys
  (`Meter ID`, `Installation Status`, `Installation Type`). Each value is rendered as
  `name && code ? "name (code)" : name || ""` (2821/2821 cells), so:
  * a blank code gives just `"Circle 6"`;
  * a blank name gives `""`. **The code is lost**, even though the export has it (8 cells).
    The UI then filters the empty level out of the breadcrumb, and the remaining crumbs
    shift without any warning.
* An unknown meter returns **HTTP 200** with a `{"type":"error","status":404}` node.

### `GET /portal/meters/{id}/geo`: coordinates

`{"data":{"latitude":"26.938961002479868","longitude":"75.83095696146852"}}`: strings, and
bit-identical to the export's numbers for 403/403 meters. You never need this endpoint.

### `GET /portal/meters/{id}/energy?from=&to=`: readings

`{"data":[{"timestamp":"23/06/2026 23:30","kwh":"48438.74","kvah":"52313.84","voltR":"226"}]}`

The UI never sends parameters, so its detail page always shows the default window. I found
the parameters by trying conventional names:

| Query | Result |
|---|---|
| *(none)* | **the last 7 days of the meter's data**, ending at its last reading (both ends inclusive: 337 half-hourly rows) |
| `from=2026-06-01&to=2026-06-02` | 01/06 00:00 → 02/06 23:30 (`to` is inclusive through the end of the day) |
| `from=` only | 7 days starting at `from` |
| `to=` only | 7 days back from `to`, clamped to the start of the data |
| `from` later than `to` | `[]`, not an error |
| anything but `YYYY-MM-DD` (datetimes, `DD/MM/YYYY`, other names like `start`) | **silently ignored**: you get the default window |
| `from=2000-01-01&to=2099-12-31` | everything the meter has (at most 1440 rows, about 118 KB) |

* Values are strings, with 0.01 resolution and trailing zeros dropped (`"48024"` means
  48024.00). A blank value is `""`.
* Timestamps are `DD/MM/YYYY HH:MM` with **no timezone**. I read them as IST (UTC+05:30),
  because the data aligns to local midnight and the utility is in Jaipur. That is an
  assumption. Note that JavaScript's `new Date()` misreads 97% of them, taking them as
  MM/DD.
* `kwh` and `kvah` are **cumulative registers**, not consumption, but the portal UI heads the
  table "Consumption". Summing the column overstates June usage for J100000 by 114,000×.
* An unknown meter returns `404 {"error":"not_found","message":"Meter not found"}`.

### `GET /portal/keys` + `GET /portal/export`: the bulk path

From the Transformers page's "Export all meters" button (node 4 bundle):

```text
secret    = GET /portal/keys → data.signingSecret        (stable; I cache it)
timestamp = unix time in SECONDS
message   = "GET" + "\n" + "/portal/export" + "\n" + "page=1" + "\n" + timestamp
signature = lowercase hex( HMAC-SHA256(key = utf8(secret), msg = utf8(message)) )

GET /portal/export?page=1
x-timestamp: <timestamp>
x-signature: <signature>
```

* The query string in the message must match the URL byte for byte.
* The server accepts timestamps within about ±4 minutes of **its own clock**: offsets up to
  ±240 s passed, while −299 s, −301 s and +400 s failed. Milliseconds are rejected.
* Replaying the same signature is accepted, since there is no nonce.
* A bad signature gives `401 {"error":"signature_invalid"}`. This is distinct from the
  session 401, `{"error":"unauthorized"}`.
* **Paging is ignored.** `page=1`, `page=2`, `page=0`, `pageSize=5` or no page at all all return
  **all 403 meters** (about 230 KB, about 200 ms). The UI's hard-coded `page=1` looks like it
  would truncate but doesn't.
* Each record is the richest view of a meter:
  `{meterId, serialNo, make, phaseType, installStatus, installType, build, dtCode,
  hierarchy:{zone,circle,division,subdivision,substation,feeder,dt: {name, code}}, geo:{lat, lng}}`.
* It counts once against the shared `/portal/*` budget (see §4). That is far cheaper than
  403 calls to `/geo`.

## 4. Rate limiting

* Over the limit you get `429 {"error":"rate_limited","message":"Rate limit exceeded; slow down."}`,
  with **no `Retry-After` and no rate-limit headers**.
* **The limit is 120 requests per 60-second window, shared by every `/portal/*` JSON
  call.** That covers search, dts, keys, export, geo and energy. `__data.json` page loads
  and `POST /login` don't count.
* **The window is fixed-length but not aligned to the clock.** It opens at the first
  counted request after the previous window expired. Reopen times were :32.5, :35.0, :35.1,
  :36.8, :37.6 and :45.0 past the minute, and that drift only ever moved forward.
* That model reproduces **all 1,177 `/portal/*` outcomes in my crawl log with zero
  mismatches**, simulated request by request, with 429s not charged. The alternatives fit
  worse:
  * a budget of 119 or 121 gives 6 mismatches;
  * counting only geo and energy gives 33;
  * windows aligned to the wall-clock minute give 436.
* It also explains a puzzle. In the first window only 93 geo and energy calls succeeded,
  because keys, export, 22 search pages and 3 DT pages had already used 27 of the 120.
* Only geo and energy *returned* 429 in practice, because they are what a client calls in
  bulk. A client should still pace every `/portal/*` call, which this API does.
* I don't know whether the limit is kept per session, per user or per IP. I didn't try to
  find out, because testing it would mean trying to get around it.

(The crawl log and the small simulator behind these figures stayed on my machine, so they
are recorded observations rather than something the repository can re-run. The test
suite's fake portal implements exactly this model.)

## 5. Latency and reliability

In about 1,600 requests there were **zero 5xx responses and zero timeouts**. The median
latency is about 20 ms. The slowest geo and energy responses (0.5–1.3 s) were mostly 429s,
or came right after a window reopened. The export takes about 200 ms.

The data was **byte-identical across repeated fetches from 20:46 to 21:46 UTC**: 7 export
captures, energy series, search pages and detail pages. So it looks static, with no ETag,
no `Cache-Control` on `/portal/*` and no change feed. The service is still built as if the
data can change: it re-syncs on a schedule, and TTLs and staleness are visible.

## 6. The data

| | Count | Notes |
|---|---|---|
| Meters | 403 | `J100000`–`J100402`, contiguous. Every view (export, search, detail, geo, energy) has the same 403. |
| Transformers (DTs) | 40 | `DT-001`–`DT-040`, 10 meters each, except DT-007 with 13. Capacity 63–400 kVA. |
| Network codes | 3 zones · 6 circles · 10 divisions · 14 subdivisions · 18 substations · 28 feeders | |
| Make | HPL 90 · Genus 88 · Secure 85 · Allied 76 · L&T 64 | Serial prefixes don't match the make: only 77 of the 400 non-`GN` serials do (GE counted as Genus), about the 80 expected by chance. The 3 `GN…` serials are all Genus |
| Status | Installed 238 · Faulty 90 · Decommissioned 75 | Agrees across all views |
| Phase / type | single 277 · three 126 / whole-current 205 · CT-operated 198 | |
| Readings | June 2026 only | 40 meters half-hourly (1440 rows), 363 daily at 00:00 (30 rows) |

Nameplate fields agree across export, search and both detail formats for **403/403**
meters. There are no conflicting sources, only *lossy* ones. The export is the system of
record.

## Data quality

These are the quirks that shaped the API. Each one was confirmed by independent re-analysis
of a full snapshot, and the live service reports most of them (`/v1/data-quality`,
`/v1/network`, `/v1/insights/anomalies`). The snapshot and the one-off analysis scripts are
not in the repository, so the statistics below (the fits, KS and permutation tests) are
recorded observations; the counts can be checked against the running service.

1. **Blank hierarchy cells (22 on 22 meters).** They are:
   * 14 blank codes: circle 6, substation 3, feeder 5;
   * 8 blank names: circle 5, feeder 3.

   Every meter on the same DT reports the same path, so each blank can be repaired from the
   DT's other 9 meters. The name↔code pattern and the DT list's `feederCode` agree on all
   22. Filtering on *raw* codes would silently drop 14 meters (C-01 would show 72 meters,
   not 73).
2. **Stale transformer name.** J100400–J100402 call DT-007 "Old Malviya Nagar Xfmr". The DT
   list, and the other 10 meters on DT-007, say "Sanganer DT 7". These three look like later
   additions: they are the only meters outside the round-robin assignment, and the only
   `GN…` serials.
3. **The hierarchy is not a tree.**

   | Child → parent edge | Children with more than 1 parent |
   |---|---|
   | circle → zone | 0 of 6 |
   | division → circle | **10 of 10** |
   | subdivision → division | **14 of 14** |
   | substation → subdivision | **18 of 18** |
   | feeder → substation | **12 of 28** |
   | DT → feeder | 0 of 40 |

   Each level is assigned to DTs independently, apparently round-robin: level index =
   (DT number − 1) mod N, where N is the number of codes at that level. That holds for
   2,807/2,807 non-blank codes. So D-01 really does sit under C-01, C-03 *and* C-05.
   * A majority-vote "fix" would rewrite 92% of meters' paths on coin-flip ties.
   * Walking the child lists top-down inflates totals: every zone would appear to contain
     all 403 meters.

   The API anchors on the DT, identifies nodes by `(level, code)`, and says `is_tree: false`.
4. **Two reading intervals.** The half-hourly meters are exactly J100000–J100039, the first
   meter on each DT. No attribute predicts the interval, so the API detects it from the data.
5. **Duplicate blank rows.** Five daily meters (J100089, J100134, J100195, J100211, J100330)
   repeat `30/06/2026 00:00` with `kwh:""` and `voltR:""`. These are the only blanks in
   68,495 rows. The portal UI shows the duplicate as the last row of its readings table,
   with kWh "—". Clients that parse `""` as 0 (JavaScript's `Number("")`) see a fake
   register reset of about −12,000 kWh.
6. **The data stops on 30 June 2026**, three months before "today". Because the default
   window is relative to the data, the portal still looks "recent". The last day is
   partial: daily meters have no 1 July reading to close 30 June.
7. **Decommissioned meters are consuming.** All 75 have rising registers and live voltage.
   Between them they account for **18.3% of the fleet's June energy** (33,432 of
   182,277 kWh over 1–29 June). That is either unbilled consumption or wrong status data.
   "Faulty" meters show nothing unusual either.
8. **The readings are synthetic straight lines.** They look like interpolation rather than
   measurement:
   * every register rises linearly, with a maximum deviation of 0.0098 kWh;
   * kVAh is exactly `round(kWh × 1.08, 2)`, so the power factor is a constant 0.926;
   * voltage is uniform random between 220 and 240 V.

   There are no resets, spikes or gaps, so anomaly thresholds can't be tuned on this data.
   The API's rules use physical and statutory limits and are tested with injected faults.
9. **The coordinates are noise.** They are uniform random within Jaipur ±0.125° (KS p≈0.1
   and 0.94). They have no clustering by DT, feeder or any other level: permutation tests
   give ratios of about 1.00, and meters sit a median of about 9.7 km from their DT's
   centroid.
   DT neighbourhood names ("Sanganer DT 7") don't match where their meters are. So the API
   **doesn't** derive transformer locations or infer topology from GPS. The portal also
   sends 15 decimal places, which is false precision; the API rounds to 6.
10. **These issues leak into roll-ups.** DT-007 is the top-consuming transformer for 1–29
    June (5,656 kWh), but only because of the three appended meters from item 2. They
    contribute 1,616 kWh (28.6%). Without them DT-007 would fall well below DT-034
    (5,500 kWh). This is why every roll-up in this API reports its meter counts, and why
    `data_issues` travel with each meter.

## UI bugs spotted along the way

* "Export all meters" requests `page=1`. It is harmless, because paging is ignored, but it
  looks like a truncation bug.
* When a session has expired, the export button fails silently. The `/portal/keys`
  response is never checked, so a `TypeError` is swallowed by `try/finally`.
* The UI never checks `res.ok` on geo or energy, so rate limiting shows up as misleading
  states:
  * a 429 on `/energy` renders *"No readings in the default window."*;
  * a 429 on `/geo` leaves *"Loading location…"* on screen forever.
* After the session expires, the meters and transformers lists show "Page 1 of NaN". They
  never check `res.ok` either.
* The search box fires a request on every debounced keystroke with no abort or sequence
  guard. A slow earlier response can overwrite newer results, and each call spends the
  shared `/portal/*` budget.
* The breadcrumb hides blank levels, so crumbs shift position with no gap shown.
* v2 meters show raw keys (`MeterId`, `SerialNo`) as labels. Legacy meters show "Meter ID".

## How this API uses the portal

| Need | Portal calls | Cost |
|---|---|---|
| Reference sync (meters, network, locations, DTs) | `POST /login` (hourly) · `GET /portal/keys` (cached) · `GET /portal/export` · `GET /portal/dts` ×2 | about 4 budgeted requests, every 15 minutes |
| Fallback if the export answers in a way we don't understand (e.g. signing changes) | `/portal/meters/search` ×21 + `__data.json` ×403 | 21 budgeted requests (detail pages don't count). It rebuilds an **identical** network (verified); detail pages carry no coordinates, so each meter keeps its last known location. An export *outage* doesn't trigger it: the last snapshot is kept instead |
| One meter's readings | `GET /portal/meters/{id}/energy?from=2000-01-01&to=2099-12-31` | 1 throttled request per meter per TTL (15 minutes) |
| Whole-fleet readings (warm-up) | 403 × the above | about 4 minutes at our self-imposed 100/min, repeated hourly |
