# Investigation log

How the portal was reverse-engineered. The polished result is
[PROTOCOL.md](../PROTOCOL.md); this file keeps the reasoning, the dead ends and the
evidence. All probing used the supplied account: GET requests plus the authentication
endpoints (the login form, better-auth's session and sign-in/sign-out calls). Nothing was
written to the portal.

The probing ran on 2026-09-30 between about 20:42 and 21:46 UTC. Several threads ran side
by side (scripted probes, often AI-assisted), so the sections below are grouped by topic
rather than strictly by time. The raw captures and analysis scripts stayed on my machine,
so the figures here are recorded observations, not something the repository can re-run.

## 1. What is it?

* `GET /` → `302 /login`. The login page carries `X-Sveltekit-Page: true`, a
  `_app/immutable/...` asset tree and an inline `kit.start(...)` bootstrap, so this is a
  **SvelteKit** app. That tells you a lot about its internals before logging in:
  * page data comes from `load` functions and is fetched on client-side navigation from
    `/<route>/__data.json`, encoded with **devalue**;
  * forms are "form actions": `POST <route>` with `x-sveltekit-action: true` returns a JSON
    `{type, status, ...}` result;
  * `_app/immutable/entry/app.*.js` contains the whole **route manifest**.
* `robots.txt` allows everything; there is no sitemap and no `.well-known`.

## 2. Reading the client bundle

The manifest in `app.03ZTmlg3.js`:

```js
dictionary = {
  "/":                     [-4],        // node 3, has a server load (redirect)
  "/login":                [-8],        // node 7, server load + form action
  "/(portal)/meters":      [5, [2]],    // node 5, NO server load, layout node 2
  "/(portal)/meters/[id]": [-7, [2]],   // node 6, server load
  "/(portal)/transformers":[4, [2]],    // node 4, NO server load
}
server_loads = [2]                     // the (portal) layout loads the user server-side
```

(A negative leaf is `~nodeId` and means "this page has a server `load`".) The two list
pages have no server load, so their data must come from `fetch` calls in the page
components. Downloading every `nodes/*.js` file gave the full API surface:

| Component | Calls |
|---|---|
| layout (node 2) | `POST /api/auth/sign-out` (the `/api/auth/*` shape is the **better-auth** library) |
| meters list (node 5) | `GET /portal/meters/search?q=&page=`, 20 per page |
| transformers (node 4) | `GET /portal/dts?page=`; **"Export all meters"**: `GET /portal/keys`, then `GET /portal/export?page=1` with `x-timestamp` / `x-signature` (HMAC-SHA256 computed in the browser) |
| meter detail (node 6) | server data (`__data.json`) + `GET /portal/meters/{id}/geo` + `GET /portal/meters/{id}/energy` |

The detail component also showed two things before any data was fetched:

* the nameplate has **two formats**: `detail.classData` (a JSON *string* containing
  `installed_meter`) or `detail.data` (a list of `{parameterName, parameterValue}`);
* the breadcrumb is built from `hierarchy[level]` for Zone, Circle, Division, Subdivision,
  Sub Station, Feeder and DT, with empty values filtered out. So blanks were expected.

## 3. Logging in

`POST /login` (form-urlencoded `email`, `password`) with `Origin` and
`x-sveltekit-action: true` → `200 {"type":"redirect","status":303,"location":"/meters"}`
plus `Set-Cookie: __Secure-better-auth.session_token=…; Max-Age=3600; HttpOnly; Secure;
SameSite=Lax`. Later checks showed:

* without an `Origin` header, or with a foreign one, you get `403 Cross-site POST form
  submissions are forbidden` (SvelteKit's CSRF guard);
* bad credentials still return **HTTP 200**, as `{"type":"failure","status":401,"data":"<devalue>"}`;
* `GET /api/auth/get-session` shows `expiresAt = createdAt + 1h`, and `updatedAt` doesn't
  move when the session is used, so sessions are **fixed-lifetime, not sliding**;
* several sessions can be live at once, and `POST /api/auth/sign-out` really revokes one;
* better-auth's own `POST /api/auth/sign-in/email` (JSON) also works, but the UI doesn't use it.

## 4. The data endpoints

* Search: `{data, total: 403, page, pageSize: 20}`. The search is a case-insensitive
  substring match on meter id and serial only. `%` matches everything (the LIKE wildcard
  is passed through), `page=1.5` starts at row 10, and there is no page-size parameter.
* DTs: 40 transformers with `feederCode` and `capacityKva`. That data exists nowhere else.
* Detail `__data.json`: devalue flat arrays; hierarchy values are `"Name (CODE)"` strings
  mixed with non-hierarchy keys (`Meter ID`, `Installation Status`, `Installation Type`).
* Geo: `{"latitude": "26.93…", "longitude": "75.83…"}` as **strings**.
* Energy: `[{"timestamp": "23/06/2026 23:30", "kwh": "48438.74", "kvah": "…", "voltR": "226"}]`.
  All values are strings, the date format is `DD/MM/YYYY` and there is no timezone.

## 5. The energy window

The UI says *"No readings in the default window"*, so there had to be a window parameter,
but the UI never sends one. With no parameters the portal returned exactly 7 days
(337 half-hourly readings) ending **30/06/2026 23:30**, three months before "today". A few
conventional names were tried:

| Query | Result |
|---|---|
| `from=2026-06-01&to=2026-06-02` | 96 readings, 01/06 00:00 → 02/06 23:30 ✅ |
| `start=…&end=…`, `startDate=…`, `days=1`, `date=…` | default window (ignored) |
| `from=01/06/2026&to=02/06/2026` | default window (ignored) |
| `from=2026-01-01&to=2026-12-31` | 1440 readings: all of June 2026, the whole dataset |
| `from=2026-06-30&to=2026-06-01` | `[]`, no error |
| `from=2026-06-01T10:00:00&…` | default window (datetimes not supported) |
| `from=2026-06-01` only | 7 days from `from` |
| `to=2026-06-02` only | 7 days back from `to`, clamped to the data start |

Conclusion: `from`/`to` are `YYYY-MM-DD`, `to` is inclusive, and **invalid input is
silently ignored rather than rejected**. That last point is why our API validates dates
itself.

## 6. The export signature, and my mistake

`GET /portal/keys` → `{"data":{"signingSecret":"…"}}`. Re-implementing the bundle's
`HMAC(secret, "GET\n/portal/export\npage=1\n<ts>")` still gave `401 signature_invalid`,
even with a fresh secret and a clock within 2 s of the server's `Date` header.

The bug was mine, in the shell rather than the algorithm. Under Git Bash on Windows, MSYS
rewrites command-line arguments that look like POSIX paths, so the signer received
`C:/Program Files/Git/portal/export` instead of `/portal/export`.
`node -e 'console.log(process.argv)' /portal/export` confirmed it, and
`MSYS_NO_PATHCONV=1` fixed it. Lesson: when a signature fails, print the exact bytes being
signed before doubting the algorithm.

With the signature working:

* the export returns **all 403 meters in one response**, whatever `page` says (`page=2`,
  `page=0`, no `page`, `pageSize=5` all give 403). The UI button sends `page=1` and would
  have been fine anyway;
* each record is richer than every other view: `installType`, `build` (`legacy`|`v2`),
  structured `hierarchy` (`{name, code}` per level) and numeric `geo`;
* timestamps are accepted within about ±4 minutes of server time: offsets of ±30, ±60,
  ±120, ±150, ±180 and ±240 s passed, while −299, −301, −600 and +400 s failed. Seconds are
  required; milliseconds fail. Replaying the same signature works.

## 7. Rate limiting, found by accident

A crawl for an offline snapshot (3 concurrent workers, each pausing 120 ms between its own
requests, with no shared rate limiting) took 376 s instead of about 90 s. The crawl log showed 398 × `429 {"error":"rate_limited"}` with **no
`Retry-After` and no rate-limit headers**, and only on `/portal/meters/{id}/geo` and
`/energy`. Search, DT list, detail pages, keys and export were never limited.

Successes per 10-second bucket made the shape obvious: exactly **120 successes, then
nothing for about 60 s, then another 120**. The windows reopened at :32, :35, :35, :37, :38
seconds past the minute. That drift means they are not aligned to the wall clock: either a
window that starts on the first request, or a sliding log. 54 files that had exhausted their
retries were refetched at about 100/min with zero 429s.

*Later refinement, from the offline re-analysis.* One detail didn't fit: the first window
admitted only **93** geo/energy calls, not 120. That is because the budget is shared by
*every* `/portal/*` call. Keys, export, 22 search pages and 3 DT pages had used the other 27.
The model "fixed 60 s window, opened by the first `/portal/*` request after expiry, 120
requests" reproduces all 1,177 logged outcomes with zero mismatches. Only geo and energy
showed 429s because they are the endpoints called in bulk. The client now paces every
`/portal/*` call.

## 8. Error and edge behaviour

| Situation | `/portal/*` JSON | `/meters/{id}/__data.json` | HTML pages |
|---|---|---|---|
| no / invalid / revoked session | `401 {"error":"unauthorized"}` | **`200 {"type":"redirect","location":"/login"}`** | `302 /login` |
| unknown meter | `404 {"error":"not_found"}` | **`200`** with an embedded `{"type":"error","status":404}` node | 404 page |

Meter ids are case-sensitive on geo/energy/detail (`j100000` → 404) but not in search.

## 9. Is the data live?

A fresh export, two energy series, a search page and a detail page, all re-fetched 15
minutes after the snapshot, were **byte-identical**. The dataset looks static (June 2026
readings only), but the service is still built for data that changes: TTL-based sync and
caching, and staleness reported to the caller.

## 10. Offline analysis

With a full snapshot on disk (export, 22 search pages, DT pages, 403 detail pages, 403 geo
responses, 403 full-range energy series), the data-quality work was done offline, with no
further load on the portal. See [PROTOCOL.md § Data quality](../PROTOCOL.md#data-quality).
