# Reflection

**What assumptions did I make?**
The biggest one is that timestamps are IST. The portal never says, but every series aligns
to local midnight, the utility is in Jaipur, and India has one DST-free offset. So I emit
`+05:30` and say so everywhere. I also treat `kwh`/`kvah` as cumulative registers read at
the timestamp, with a multiplying factor of 1, since the portal exposes none. I trust the
bulk export as the system of record, the DT list as the authority for transformer names
and feeders, and a transformer's own meters (by majority) for everything above. "Recent"
means the portal's latest data, not the calendar week, and responses carry
`last_reading_at` so nobody mistakes June for today. Finally, I assumed one service
instance, using the supplied account politely: 100 of the portal's roughly 120 requests
per minute.

**Which part was the most difficult, and how did I get unstuck?**
Modelling the network. The per-meter hierarchy looked like a tree with some noise, and my
first reconstruction was a trie of paths whose counts added up. Testing hypotheses
against all 403 records showed the real structure. Every level is a strict function of the
transformer, but the levels are assigned independently of each other, so a division really
does sit under three circles. That isn't noise to vote away. A majority-vote tree would
have rewritten 92% of meters' paths on ties, and path-identity nodes split one division
into three. What got me unstuck was to stop forcing a tree. I anchored on the transformer,
where there are zero conflicts, identified nodes by `(level, code)`, listed all parents
with counts, and said `is_tree: false` out loud. The trie survives only as a labelled
drill-down view.

**If I had another day, what would I improve?**
First, incremental sync: diff each export against the last snapshot and emit change events
such as a status change or a meter moving transformer. Downstream teams would care more
about those than about snapshots. Then:
* a month-chunked readings cache with pre-aggregated daily tables, so the insights stop
  recomputing from raw readings;
* a nightly contract test against the live portal to catch protocol drift;
* metrics;
* browser tests for the web client.

**What mistake did I make?**
Three worth owning.

The first cost time. My re-implementation of the export signature kept returning
`signature_invalid`, and I doubted the algorithm and clock skew before checking the exact
bytes I was signing. Git Bash on Windows had rewritten the argument `/portal/export` into
`C:/Program Files/Git/portal/export`. Printing the signed message would have caught it in a
minute.

The second was less polite. My first full crawl ran three workers with no shared rate
limiting (each only paused 120 ms between its own requests), and I only discovered the
rate limit by collecting about 400 × 429s.

The third was a pair of silent correctness bugs at the edge of a proximity search. First, the
bounding box assumed 111.32 km per degree while the distance function uses a 6,371 km sphere
(111.195 km per degree), so a meter 0.1% inside the radius could be dropped. Then, after
fixing that, I compared distances only after rounding them to 4 decimals, so a meter 0.04 m
*outside* the radius could slip in. Unit tests on 403 meters noticed neither. Both were caught
by the benchmark comparing every query with a brute-force scan over 100,000 points. The
lesson: test an index against a naive oracle, not only against examples.

**If I were reviewing this submission, what would I criticise?**
* **Size.** It is on the large side for a take-home. The anomaly engine and the insights
  endpoints go well past "an API over the portal", and a reviewer could fairly call them
  scope creep.
* **Scale and deployment.** Some choices only hold at this scale:
  * the rate limiter lives in one process, so two instances would double the load on the
    portal;
  * reads and the sync share one SQLite connection;
  * each sync rewrites the whole snapshot.

  All are fine at 403 meters but not at 100k (see the README's scale section).
* **Failure paths got less attention than the happy path.** A late adversarial review still
  found real bugs: a window given in UTC was bucketed on UTC days, a portal outage made
  readings requests wait about 25 s before serving the cached copy, and the per-request
  deadline didn't bound a hung connection. All are fixed and tested now, but they should
  have been designed in from the start.
* **Test blind spot.** The fake portal encodes *my* understanding of the portal, so if I
  misread a behaviour, the tests pass anyway. The opt-in live test only partly closes that
  gap.
* **Timezone.** The IST assumption is well supported, but it's still an assumption.
* **Web client.** It has no automated tests.
