# saferoute-osm-data: design (31 October 2026)

Every week we build our own copy of the OpenStreetMap (OSM) data that street lighting and Find Help need, from Geofabrik extracts. The backend serves both features from that copy inside the areas it covers. Outside them it uses the existing Overpass client (`overpass.js`).

Code references are to backend commit `34ad0d1` (branch `overpass-stopgap`). The worktree has uncommitted edits by another session, so its line numbers may drift. The measured numbers come from the two approved test extracts. The probes behind them are in `tools/`.

## 0. Decisions

- **Weekly build.** A public repo builds `lighting.pack` and `places.pack` every week. It publishes them, with `manifest.json`, `ATTRIBUTION.txt` and `LICENSE-DATA.md`, as release assets under ODbL 1.0.
- **Pinning.** The private backend repo pins the release by SHA-256 in `data/osm-manifest.json`. Render's build downloads and verifies the assets, and boot verifies them again. The packs are read from disk through a tile index, with a ~48 MB LRU in memory.
- **Who answers.**
  - Find Help decides per 0.02° tile: a covered tile is answered locally, and any other tile goes down today's Overpass path.
  - Lighting decides per request: local if the padded route box lies wholly inside lighting coverage, otherwise Overpass.
  - Both keep their algorithms and their JSON.
- **Data age.** `/health` reports it:
  - over 10 days: degraded;
  - over 30 days: the copy is switched off and Overpass is used.

  The monitor warns at more than 9 days and raises a critical alert at more than 10 (§11).
- **The build cannot stop silently.** Three guards:
  - the public schedule, plus a weekly bot commit;
  - a private daily watchdog;
  - the data-age alert in the monitor's email.

## 1. Regions and Geofabrik files

**Boxes** are the backend `covers()` boxes, in `tools/regions.json`. Each is widened by **5 km** for lighting. The files are the smallest set whose polygons contain every 0.005° sample of each widened box. `tools/geofabrik_coverage.py` computed them against `index-v1.json`, fetched today.

| Regions (covers() at) | Geofabrik files |
|---|---|
| NYC `nycProvider.js:67-68`; Philadelphia `phillyProvider.js:60-61` | us/new-york; us/pennsylvania; us/new-jersey for both |
| Chicago `chicagoProvider.js:102-103` | us/illinois, us/indiana |
| DC `dcProvider.js:70-71`; Baltimore `baltimoreProvider.js:40-41` | us/district-of-columbia, us/maryland, us/virginia |
| Boston `bostonProvider.js:72-73`; Hartford `hartfordProvider.js:32-33` | us/massachusetts; us/connecticut |
| Detroit `detroitProvider.js:29-30` | us/michigan, ontario (Windsor) |
| Cleveland `clevelandProvider.js:58-59`; Minneapolis `minneapolisProvider.js:35-36` | us/ohio; us/minnesota |
| Kansas City `kansasCityProvider.js:102-103` | us/missouri, us/kansas |
| Charlotte `charlotteProvider.js:38-39` | us/north-carolina, us/south-carolina |
| Nashville `nashvilleProvider.js:44-45`; Memphis `memphisProvider.js:23-24` | us/tennessee; also us/mississippi and us/arkansas for Memphis |
| New Orleans `nolaProvider.js:48-49` | us/louisiana, us/mississippi |
| Houston, Dallas, Fort Worth `houstonProvider.js:42-44`, `dallasProvider.js:104-105`, `fortWorthProvider.js:40-41` | us/texas |
| Denver `denverProvider.js:32-33`; Tucson `tucsonProvider.js:62-63`; Las Vegas `lasVegasProvider.js:93` | us/colorado; us/arizona; us/nevada |
| SF `sfProvider.js:62-63`; LA, Long Beach `losAngelesProvider.js:80-82`, `longBeachProvider.js:32-33`; San Diego `sanDiegoProvider.js:26-27` | norcal; socal; socal and mexico for San Diego (Tijuana is inside +5 km) |
| Seattle `seattleProvider.js:70-71` | us/washington |
| Toronto `torontoProvider.js:97-98`; Vancouver `vancouverProvider.js:82-83` | ontario; southcoast-admreg (117 MB; it covers the box +10 km, so BC's 1,191 MB file is not needed) |
| Mexico City `cdmxProvider.js:45-46` | mexico |
| UK `ukProvider.js:371-374`, plus the app's outline | united-kingdom, isle-of-man, guernsey-jersey |

**The UK outline.**
- In 1.4.2 the app's outline includes the Isle of Man, Jersey and Guernsey with Alderney, Sark and Herm (`wt-release-142/SafeRoute/Core/Models/UKOutline.swift:7-8`).
- Geofabrik's `united-kingdom` file has England, Scotland, Wales and Northern Ireland, **not** the islands. `isle-of-man` and `guernsey-jersey` are separate files, each with parent `europe`.
- I sampled the outline every 0.05° (`tools/uk_outline_coverage.py`). The three files cover everything except open sea: 35 samples south of 49.0° N (around Les Minquiers) and 2 in the North Channel (about 54.2° N, 5.25° W).

**Per run:** 34 files, about **11.9 GB**:
- 28 US files: 8.0 GB;
- ontario: 978 MB;
- southcoast-admreg: 117 MB;
- mexico: 618 MB;
- united-kingdom: 2,174 MB, the largest file;
- isle-of-man: 6 MB;
- guernsey-jersey: 4 MB.

Sizes are from HEAD requests on 30–31 Oct.

**Coverage:**
- **Find Help** is built from the *whole* of every file: all of the UK, the Isle of Man and the Channel Islands, the 28 states, Ontario, the BC South Coast and all of Mexico.
- **Lighting** is clipped to the city boxes +5 km in the US, Canada and Mexico. In the UK it covers the whole of the three files, because the app plans routes in Scotland and on the islands too.
- **Complete tiles.** A tile is *complete* only if it lies inside the union of the downloaded polygons.
- **Incomplete tiles** in the region boxes are open water, and they go to Overpass. Every build reports them by region:
  - Lake Michigan east of about −87.525° (3–7 km offshore);
  - the ocean south of NYC below 40.465° N;
  - the two sea patches above.

## 2. Extraction rules

**Lighting** is today's `way[highway][lit](bbox); out geom;` (`lighting.js:90-92`).
- Commands: `osmium tags-filter IN w/lit -o l1.pbf`, then `osmium tags-filter l1.pbf w/highway -o lit.pbf`. The two passes give AND; referenced nodes are kept by default.
- For each way the packer keeps the id, the raw `lit` value and every node.
- The unlit rule (`lighting.js:60`) stays in the backend.

**Places** are exactly `tileQuery()` (`safePlaces.js:419-441`):
- nodes, ways and relations with `amenity=hospital`;
- nodes and ways with `amenity=police`;
- nodes and ways with `amenity=pharmacy`, all of them (the 24-hour filter runs later, in `normaliseElements`);
- nodes and ways with `shop` = convenience|supermarket and `opening_hours` matching `/24\/7/` (unanchored and case-sensitive, like Overpass `~`).

Commands: `osmium tags-filter -R IN nwr/amenity=hospital nw/amenity=police,pharmacy nw/shop=convenience,supermarket -o match.pbf`, then `osmium getid -r -I match.pbf IN -o places.pbf`, which adds every referenced node and member. The packer then applies the full rule above to each object.

**Tags kept:** only the 20 keys the backend reads.
- `amenity`, `shop`, `opening_hours`, `emergency`, `beds`, `healthcare`, `healthcare:speciality` (`safePlaces.js:480-488`, `:676-735`);
- `name`, `name:en`, `alt_name`, `official_name` (`:623-626`; the alternatives report missed the last two);
- `wikidata`, `operator`, `brand`, `phone`, `contact:phone`;
- `addr:street`, `addr:housenumber`, `addr:city`, `addr:postcode` (`:737-749`).

`hospitalVerdict`, `is24_7`, the rule-9 test (`:668-671`) and `normaliseElements` stay in the backend and run unchanged.

**Overpass semantics we reproduce.** These are assumptions A1–A6. §6 tests them.
- **A1.** A way is in a bbox if any of its segments touches the closed box.
- **A2.** A relation is in a bbox if a member node is inside or a member way touches the box.
- **A3.** `center` is the midpoint of the element's bounding box (for a relation, over its member nodes and its member ways' nodes, with sub-relations ignored), to 7 decimal places.
- **A4.** Output order is nodes, then ways, then relations, each by id ascending.
- **A5.** `out geom` gives every node of the way, in order, at 1e-7°.
- **A6.** A node on a tile edge belongs to both tiles.

**How the packer applies them:**
- All tile arithmetic is done in integers at 1e-7° (one tile = 200,000 units).
- Each element is stored in **every tile its geometry touches**, not only the tile its centre falls in. So each tile's record *is* the Overpass answer for that tile, and `getSafePlaces` (merge, dedupe, radius filter, sort: `:206-223`) gives the same list.
- If an element is in two files, the higher `version` wins.
- An element with a member missing from every downloaded file marks the tiles it touches as incomplete. This matters because Geofabrik keeps only ways and multipolygons complete across a border (download.geofabrik.de/technical.html).

## 3. Formats and sizes

**Pack file** (both kinds, little-endian):
- a 64-byte header: magic `SROSMPK1`, kind, version, tile size (200000), `dataAsOf` (the oldest source timestamp), and offsets to the index, the coverage table and a string table;
- the **index**: 20-byte records `(x i32, y i32, offset u32, length u32, count u32)`, sorted by (y, x) and found by binary search;
- the **coverage table**: per tile row, run-length `[x0, x1]` ranges of complete tiles. A complete tile with no index entry is a true empty;
- the **payloads**: raw deflate, one per tile.

**Lighting payload:** a varint way count, then for each way:
- the way id as a delta;
- an index into the string table of `lit` values;
- the point count;
- the points as zigzag-varint deltas at 1e-7°, the first one relative to the tile's south-west corner.

These are OSM's own integers, so decoding gives exactly the doubles that Overpass's 7-decimal JSON parses to.

**Places payload:** UTF-8 JSON in Overpass order and shape: `{type, id, lat, lon}` or `{type, id, center}`, plus `tags`.

**Measured** with `tools/measure.py` (pyosmium 4.3.2, the same rules):

| Extract (OSM as of) | Lit ways / points | Lighting | Places | Places pack |
|---|---|---|---|---|
| DC, 21 MB (30 Oct 21:24Z) | 5,896 / 31,845 | 146 KB, 45 tiles | 113 | 12.6 KB |
| Greater London, 130 MB (31 Oct 21:23Z) | 188,081 / 972,559 | 4.88 MB, 619 tiles (largest 78 KB, 17,406 points) | 1,763 (199 hospitals incl. 10 relations, 130 police, 1,287 pharmacies, 147 shops) | 165 KB, 414 tiles |

- Storing ways whole in every tile they touch costs 1.13× in points.
- Coordinates take about 5 B per point, and deflate barely helps them.

**Estimates for the full build:**
- **Lighting:** UK lit ways (1.15 M, taginfo) are 6.1× London's, so about 30–40 MB. The clipped US boxes are 24 MB or less (907 k US-wide at about 26 B per way); Ontario, BC and Mexico 5 MB or less. **Total ≈ 40–60 MB.**
- **Places:** about 120 k elements at ≈ 95 B each, so **6–15 MB**.
- **Index and coverage:** about 4 MB, resident in memory.

## 4. Weekly build (public repo `minhajk21/saferoute-osm-data`)

`.github/workflows/build.yml` runs on `ubuntu-24.04`, which for a public repo means 4 vCPU, 16 GB RAM and 14 GB SSD.
- **Triggers:** `schedule` at `17 20 * * 0` (Sundays 20:17 UTC, once Geofabrik has Saturday's North American files: §11) and `workflow_dispatch`.
- **Settings:** `timeout-minutes: 240`; two jobs (§11): `build` with `contents: read` only, and `publish` with `contents: write`, `id-token: write`, `attestations: write`; actions pinned by SHA.

1. **Install:** `apt-get install osmium-tool`, then `pip install --require-hashes -r requirements.lock` (pyosmium and shapely, pinned). Fail if less than 6 GB of disk is free.
2. **Download, one file at a time:**
   - HEAD the `-latest` URL; it 307-redirects to a dated file such as `district-of-columbia-261030.osm.pbf`;
   - GET that file and its dated `.md5`, verify the MD5 and record the SHA-256;
   - read `osmosis_replication_timestamp` and fail if it is older than the previous build's or more than 7 days old;
   - send the User-Agent `SafeRoute/1.0 (https://safe-route.app; minhaj@safe-route.app)`.
3. **Process each file:** run the §2 filters, then the packer writes into a SQLite intermediate store (stdlib) keyed by tile. Delete the file's `.pbf`s. Peak disk use is under 4 GB.
4. **Merge** into the two packs. Coverage is `union(polygons).contains(tile)`, using shapely.
5. **Golden step** (§6.1) on the DC file and on a London box cut from the UK file with `osmium extract -b`. Any tile that differs fails the build.
6. **Sanity checks against the previous release.** Any failure stops the publish:
   - per-region place and way counts within −10% / +25%;
   - about 30 sentinel places present (for example University College Hospital, Bellevue, GW Hospital);
   - every region box complete apart from the known water tiles;
   - every places tile of the strict area complete, and no lighting hole there next to lit streets (§11).
7. **Publish:**
   - `actions/attest-build-provenance` on the packs and the manifest;
   - `gh release create data-YYYY-MM-DD` with the packs, `manifest.json` (each source's URL, bytes, MD5, SHA-256 and timestamp; each asset's bytes, SHA-256 and counts; the tool versions; `dataAsOf`; the region boxes), `ATTRIBUTION.txt` and `LICENSE-DATA.md`.
8. **Keepalive:** commit `LATEST.json` (date, hashes, counts). Delete a release only once it is over 26 weeks old and outside the newest 8 (§11).

**Run time:** expected 30–75 min, a guess. Downloads dominate: about 12 GB at 10–40 MB/s. My pyosmium probe took 39 s for London.

**Licence:**
- `ATTRIBUTION.txt`: "© OpenStreetMap contributors. Data under the Open Database License 1.0, from Geofabrik extracts (download.geofabrik.de)".
- `LICENSE-DATA.md`: ODbL 1.0 for the packs, which are Derivative Databases.
- The release notes link the scripts at the build commit (ODbL 4.6).
- The scripts take a code licence of the owner's choosing.
- The app's OSM credits are unchanged (`AboutView.swift`, `NearestHelpSheet.swift`).

**The 60-day disable.** GitHub turns off a public repo's schedule after 60 days with no activity (`overpass-outage-2026-10-31/overpass-ch-pages/gh_disable.html`). Three guards:
- **(a)** The weekly bot commit is repository activity. That is likely, not proven, for a `GITHUB_TOKEN` push.
- **(b)** A private-repo workflow, `osm-data.yml` (template: `contrib/backend-osm-data-watchdog.yml`), runs every 6 hours. The rule applies to public repos only. It uses a fine-grained PAT, `OSM_DATA_TOKEN`, with Actions read/write on the data repo only, and only to enable and dispatch:
  - if the build workflow's state is not `active`, it runs `gh workflow enable`;
  - if the newest release's *data* (`data_as_of_epoch` in its manifest) is more than 8.5 days old, it runs `gh workflow run build.yml`, unless a build is already queued or running, or the last one failed less than 72 hours ago (§11);
  - either way it then *fails*, so the run shows red.
- **(c)** Even if (a) and (b) both fail, for example because the PAT expired, the data-age alert (§5) emails within 6 hours of the data passing 9 days.

## 5. Backend (summary)

The backend is private; its own `docs/osm-data.md` has the file-by-file map, the monitor thresholds and the policy edits that used to be here. In short:
- **Pin.** A private watchdog verifies each new release (attestation from this repo's `build.yml` on `main`, the release's own sanity flag, every hash, data newer than the pinned data) and commits `data/osm-manifest.json` there. Render's build fetches and re-hashes the pinned packs; boot re-hashes them again.
- **Memory.** The packs stay on disk. Only the tile index, coverage and strings are resident; tiles are read on demand into byte-capped caches (about 48 MB), each entry kept at most 1 hour.
- **Answers.** Find Help per 0.02° tile, lighting per padded route box, from complete tiles only; the same JSON and the same algorithms as the Overpass path.
- **Strict area.** Inside the backend's covered regions (every tile meeting a city box or the app's UK outline, `pipeline/strict.py`) the backend never asks Overpass once a pin is committed: with no usable copy, Find Help there answers its retryable 502 and lighting is null. That is why every release must have every strict tile complete (§4 step 6).
- **Data age** is on `/health`: degraded past 10 days, the copy off past 30.

## 6. Proving it matches the Overpass path

Live Overpass is unusable, so the reference is a brute-force **Python emulator of A1–A6** (`pipeline/golden.py`). It reads the *raw* extract with pyosmium and shares no code with the packer.

1. **Per tile, in every build.** For every tile of DC and of the London box, the emulator's answer must equal the pack's payload: the same elements, order and tags, with centres within 1e-7°. The only tolerated difference is a node on a tile edge (A6).
2. **Per request, once, in the backend.** `test/osm-golden.test.js` uses committed fixtures: a few hundred KB of central DC and London packs, plus the emulator's JSON for the same tiles.
   - **Find Help:** request points every 300 m over 10 × 10 km of each city, at radius 500, 1,000, 1,500 and 5,000 m. Each runs through `getSafePlaces` twice: once with `overpassQuery` stubbed to return the emulator's tiles, once local. The responses must be deep-equal apart from `cachedUntil`.
   - **Lighting:** 2,000 synthetic routes per city (chained highway ways, plus straight lines from 0.3 to 8 km). The `{lit, unlit, unknown}` values and the null decisions, including London's `MAX_WAYS` nulls, must be identical.
3. **Anchor check, once, when a working Overpass is available.** Compare the emulator with real answers: 3 DC and 3 London place tiles and 3 lighting boxes, on the extract's date, with `out meta` so that a newer `version` explains any difference. Until this runs, equivalence rests on A1–A6.
4. **Regions.** `test/osm-regions.test.js` checks that every provider's `covers()` box, widened by 5 km, is inside the pinned lighting coverage. A city added without data then fails `npm test`.

## 7. Trust model

**What we trust:**
- OSM contributors, for the content, as we do today. A fake "hospital" edit reaches users either way, and only `hospitalVerdict` filters some of them.
- Geofabrik, to cut the extracts faithfully. Its MD5 over HTTPS only catches corruption in transit.
- Ubuntu apt and PyPI for the tools (pinned, with hashes); GitHub's runners, releases and Sigstore attestations; and the owner's GitHub account (2FA), which controls both repos.

**What the checks catch:**
- a corrupt download (MD5, then SHA-256 at every hop);
- a release asset swapped after pinning (the backend checks its own pinned hash at build time and at boot);
- an asset our workflow did not build (the attestation);
- a broken build (the golden step, the sanity checks and the sentinels);
- stale data (`/health`).

**What they do not catch:**
- malicious OSM edits;
- a compromised account or workflow. An attestation proves who built a file, not that it is benign;
- Geofabrik serving altered data with a matching MD5.

The pin makes every change of data an auditable, revertible commit in the private repo. It is **not a review**: by default the watchdog commits each verified pin to the backend's `main` and Render deploys it, with nobody looking first. The gate is automatic (attestation, sanity, golden and strict-area checks, hashes). The watchdog's `OSM_PIN_MODE=pr` makes each pin a pull request instead, and a `data/osm-hold` file there stops it changing the pin (for a rollback that sticks).

**The build job never holds a write token.** It parses about 12 GB of downloaded files with libosmium, so it runs with `contents: read`, no stored git credentials and no token in the build step's environment. A separate `publish` job, which never reads a `.pbf`, re-checks the hand-over against the manifest, attests, publishes and commits. A parser exploit in the build job could therefore produce bad packs (which the attestation would then truthfully say `build.yml` built), but could not sign or publish anything else.

## 8. Policy and LIA consequences

These are the owner's, in the private backend repo (`docs/osm-data.md`). Geofabrik and GitHub receive no user data from this repo: downloads happen at build time, never per request.

## 9. Owner actions, effort, open points

In the private backend repo (`docs/osm-data.md`). The public open points:
- A1–A6 are unverified until the anchor check against a live Overpass runs (§6).
- Whether a `GITHUB_TOKEN` commit counts as repository activity is unproven; the watchdog covers it.

## 10. Implementation notes (1 November 2026)

The pipeline is built and tested on the two approved extracts. Where it differs from sections 0–9:

- **Ireland is now a source (35 files, about 12.3 GB per run).** Tiles on the Northern Ireland border reach into Ireland, and the `united-kingdom` polygon stops at the border. Without `ireland-and-northern-ireland` (398 MB), 544 tiles of the app's UK outline are incomplete: about 270 along the land border and in the North Channel, and 272 at sea south of Jersey. With it, 272 tiles remain (corrected in §11): sea south and east of Jersey, at least 8 km from its coast, including Les Minquiers' Maîtresse Île (an uninhabited reef with huts, land, not sea), plus two mid-Channel and two Irish Sea squares. The section 1 probe sampled points inside the outline, but a Find Help request needs every *tile* it touches, and those tiles cross the border. Lighting for Ireland is kept whole too, so the border tiles are complete for lighting as well. As a side effect, Find Help is local across Ireland.
- **No osmium-tool and no shapely.** Neither is installed here (shapely was not approved), so extraction is pyosmium 4.3.2 alone, filtering in C++ (`KeyFilter`, `TagFilter`, `IdFilter`, `IdTracker`). Reads are one object type each, and node locations are read by id only for the nodes kept ways need. No full location index is built: the UK has about 300 M nodes, and `flex_mem` at 16 B per node, with vector doubling, would peak near the runner's 16 GB. The cost is pyosmium's `IdTracker` bitmap, about 1.2 GB whatever the file size. Coverage is my own exact integer and rational test (`pipeline/coverage.py`), not shapely.
- **Neighbouring US state polygons overlap by only 20–100 m.** I measured this along DC's border with Maryland and Virginia. A sampling or cell-subdivision coverage test either drops every border tile or, with coarse samples, wrongly accepts gaps. `tile_covered` cuts each border tile into slabs at every edge end point, every edge-edge crossing and every edge-side crossing, and requires the polygons' inside intervals to cover the full tile width in each slab. A test with a 2-unit (0.2 m) gap and a 2-unit overlap covers this.
- **The golden step runs twice per city.** First, packs are built from the golden extract alone and compared with margin 0: 26 DC tiles and 452 London tiles. Second, the build's real packs are compared with that extract, with a 1-tile margin because elements near the edge come from other files: 3 DC tiles (DC is small) and 334 London tiles. For London, CI downloads `greater-london` of the same day as the UK file (130 MB), instead of cutting a box with `osmium extract`. The cross check runs only when both files carry the same OSM timestamp.
- **Unverified sentinels only warn.** Fourteen sentinels (9 London, 5 DC) are pinned by id from the test extracts and block a publish. The other 20, in other cities, are matched by name near a point and only warn until the first full build confirms them (`config/sentinels.json`).
- **Release retention could break a backend deploy.** Fixed in §11: releases are kept by age, and the backend's build step treats a 404 as "no packs".
- **Smaller points.**
  - The build and pin scripts check `manifest.sanity.ok`; they do not re-run the sanity checks.
  - `tools/regions.json` is superseded by `config/regions.json`, which holds the same boxes, re-checked against `34ad0d1`.
  - Locally the packs are about 5% smaller than `tools/measure.py` reported. Only complete tiles are written; London has 452 complete tiles, against the 619 tiles it touches.
  - The full-build estimate stands at 30–60 min. Extraction took 57 ms per MB on this Mac, so allow about twice that on a 4-vCPU runner, plus the downloads.

## 11. Review fixes (1 November 2026)

Two reviews of the pipeline and the backend loader; what changed here:

- **Data age, not release age.** The watchdog read the release's `createdAt`, which GitHub sets to the date of the commit the release points at (last week's `LATEST.json` commit), so every release looked about 7 days old when published and triggered an extra build every week. It now reads `data_as_of_epoch` from the newest release's `manifest.json`. Retention uses `publishedAt`.
- **Schedule.** Geofabrik publishes day D's North American extracts around 16:30 UTC on D+1 (the DC file of 30 Oct was last modified 31 Oct 16:28 UTC) and Europe's by about 00:30 UTC on D+1, so a 02:17 UTC Sunday run got Friday's North American data. The build now runs Sundays at 20:17 UTC on Saturday's data; the watchdog runs every 6 hours and pins within 6 hours of a release. A healthy copy is about 1.3 days old when pinned and about 8.3 just before the next pin, so the monitor warns past **9** days (not 8) and `/health` degrades past 10. One dropped schedule run is re-dispatched once the newest data passes 8.5 days (the Monday 12:41 UTC watchdog run) and pinned that evening, when the served copy is about 9 days old: at worst a warning, never a degraded `/health`.
- **No daily 12 GB retries.** The watchdog does not dispatch while a build is queued or running, nor within 72 hours of a failed one (it only fails red). `download.preflight()` resolves all 35 `-latest` URLs (HEAD) before any file is downloaded, so a renamed extract stops the run at once. A 429 or 503 with `Retry-After` is waited out as asked (up to 10 minutes; a longer ask fails the run), and a cut-off download resumes with a `Range` request.
- **Retention by age.** Releases are deleted only when over 26 weeks old and outside the newest 8, so a backend pin cannot 404 because of manual re-runs or a stalled watchdog. The backend's build step also treats a 404 as "no packs" (a warning, not a failed deploy).
- **Two jobs.** See §7.
- **Watchdog.** Token failures no longer skip the pin step (the PAT is used only to enable and dispatch; reading and pinning use the backend's own token, the data repo being public). `gh attestation verify` adds `--source-ref refs/heads/main` and `--deny-self-hosted-runners`. A pin is refused unless its data is newer than the pinned data, its manifest names its own tag, and the pack format is 1. `data/osm-hold`, `OSM_PIN_MODE=pr` and an optional deploy hook are documented at the top of the template.
- **The manifest names its tag** (`tag`), so an unpinned reader fetches the packs from that tag, not from `latest`, which may move between its requests. `LATEST.json` gains `data_as_of_epoch`.
- **Strict area and reviewed-empty tiles** (`pipeline/strict.py`). The backend now never asks Overpass inside its covered regions once a pin is committed, so the sanity check fails a release unless every places tile there is complete, and fails on any lighting hole there within 2 tiles of lit streets (a hole next to streets is not open water). Two groups of tiles lie outside every extract polygon yet can hold nothing either feature returns; a reviewer listed them in `config/reviewed_empty.json` with the reason, and `pack.py` marks them complete and empty, unless any element from any extract touches one, which fails the build instead:
  - 45 places and 102 lighting tiles of Lake Michigan off Chicago (at or east of 87.54 W, at or north of 41.76 N, at least 0.7 km offshore). Before this, a 1–1.5 km Find Help request from the South Shore lakefront touched an incomplete tile and depended on Overpass.
  - The 272 places tiles of the UK outline above (Find Help only; they stay incomplete for lighting, where the backend answers null).

  `tools/strict_area.py` writes the strict area as the backend commits it.
- **Corrections to §10:** the nearest UK-outline hole is at least 8 km from Jersey's coast (not "more than 10 km"), and Les Minquiers' Maîtresse Île tile is land, not sea. `config/uk_outline.json` now carries a `_licence` field (ODbL), since the repository's root `LICENSE` (MIT) covers the code only.
- **Not done:** the live anchor check against a real Overpass (§6; A1–A6 remain assumptions shared by the packer, the golden check and the backend's tests), and the stale-element case between overlapping extracts (`store.py` keeps the higher version of an element that still matches in one file; an element that stopped matching in a newer overlapping file survives from the older one, at most the day of skew between Geofabrik's files, inside data that is a week old anyway).

