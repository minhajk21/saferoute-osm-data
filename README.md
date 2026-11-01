# saferoute-osm-data

A weekly copy of the OpenStreetMap data that SafeRoute's backend needs for **street lighting** and **Find Help**,
built from Geofabrik extracts and published here as release assets. Inside the areas this copy covers, the
backend answers both features itself, so no user's point or route leaves our server. Outside them it keeps its
Overpass client. The design is in [DESIGN.md](DESIGN.md); section 10 records where the build differs from it.

## Licence

- **Data** (`lighting.pack`, `places.pack`, `manifest.json` in each release): © OpenStreetMap contributors,
  under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). See
  [LICENSE-DATA.md](LICENSE-DATA.md) and [ATTRIBUTION.txt](ATTRIBUTION.txt). Each release's notes link the
  scripts at the commit that built it.
- **Code** (`pipeline/`, `tools/`, `tests/`, `.github/`, `contrib/`): MIT, see [LICENSE](LICENSE).
  `config/uk_outline.json` is derived from OpenStreetMap and is ODbL too.

## What is in the packs

| Pack | Selection (the backend's Overpass query, reproduced) | Scope |
|---|---|---|
| `lighting.pack` | every way with `highway=*` and `lit=*`, all its nodes, the raw `lit` value (`lighting.js` `way[highway][lit](bbox); out geom;`) | UK, Isle of Man, Channel Islands and Ireland whole; elsewhere the backend's city boxes + 5 km |
| `places.pack` | `amenity=hospital` (nodes, ways, relations); `amenity=police` and `amenity=pharmacy` (nodes, ways); `shop=convenience\|supermarket` with `opening_hours~24/7` (nodes, ways), with the 20 tags the backend reads (`safePlaces.js` `tileQuery()`) | every downloaded file, whole |

Both are cut into the backend's 0.02° tiles. An element is stored in every tile its geometry touches, so a
tile's record is the Overpass answer for that tile. Each pack carries a coverage table: only tiles that lie
wholly inside the downloaded extracts' polygons are "complete"; the backend must send anything else to its
fallback. The byte format is specified in [`pipeline/packfmt.py`](pipeline/packfmt.py).

The extracts are listed in [`config/sources.json`](config/sources.json) (35 files, about 12.3 GB per run), the
city boxes in [`config/regions.json`](config/regions.json).

## How the weekly build runs

[`.github/workflows/build.yml`](.github/workflows/build.yml) runs on Sundays at 20:17 UTC (when Geofabrik
has Saturday's files for every region, North America included) and on manual dispatch. Its `build` job is
read-only (no write token, no stored git credentials) and:

1. installs pyosmium from hash-pinned wheels and runs the unit tests;
2. resolves every extract's `-latest` URL first (HEAD only), so a missing one stops the run before anything
   is downloaded; then downloads each extract **one at a time** (the redirect gives the dated file; its MD5
   must match Geofabrik's `.md5`; its OSM timestamp must be under 7 days old and not older than the previous
   release's; `Retry-After` is honoured and a cut-off transfer resumes with `Range`), adds it to a SQLite
   store, and deletes it;
3. merges the store into the two packs;
4. **golden step**: a brute-force emulator of the Overpass path ([`pipeline/golden.py`](pipeline/golden.py),
   which shares no code with the packer) recomputes every tile of DC and of Greater London from the raw
   extracts and must match the packs exactly;
5. **sanity checks** against the previous release: counts per region and per source within −10%/+25%,
   [sentinel places](config/sentinels.json) present, no incomplete tile except those listed in
   [`config/known_incomplete.json`](config/known_incomplete.json) (city boxes and the app's UK outline), and
   **every places tile of the strict area complete** (the backend's covered regions, where it never asks
   Overpass: [`pipeline/strict.py`](pipeline/strict.py)). Open water outside every extract polygon is made
   complete only where a person reviewed it ([`config/reviewed_empty.json`](config/reviewed_empty.json)) and
   no element touches it.

A separate `publish` job, which never reads a `.pbf`, re-checks the hand-over against the manifest, attests
the packs and manifest (GitHub artifact attestation), publishes a release `data-YYYY-MM-DD`, commits
`LATEST.json` and deletes releases only once they are over 26 weeks old and outside the newest 8.

Any failure in 2–5 stops the publish; the previous release stays current. `manifest.json` holds every
source's URL, size, MD5, SHA-256 and OSM timestamp, every asset's size, SHA-256 and counts, the tool
versions, the build commit, and `data_as_of` (the **oldest** source timestamp).

**The schedule cannot stop unnoticed.** GitHub disables a public repo's schedule after 60 days without
activity. The weekly `LATEST.json` commit is activity; the backend repo runs a watchdog every 6 hours
([`contrib/backend-osm-data-watchdog.yml`](contrib/backend-osm-data-watchdog.yml), a template to copy there)
that re-enables this workflow, dispatches it when the newest release's data is over 8.5 days old (but not
again within 72 hours of a failed build), and fails when it had to; and the backend's `/health` and monitor
alert on data age (warning past 9 days, critical past 10) whatever happens here.

**Trust.** The checks catch corrupt downloads, an asset swapped after the backend pinned it (the backend
re-checks its own pinned SHA-256), an asset this workflow did not build (attestation), a broken build (golden,
sentinels, counts) and stale data. They do not catch a malicious OSM edit, a compromised GitHub account or
workflow, or Geofabrik serving altered data with a matching MD5. Nobody reviews a release before the
backend's watchdog pins it, unless the backend sets `OSM_PIN_MODE=pr`. DESIGN.md section 7 has the full model.

## Run it locally

Python 3.12 and pyosmium 4.3.2; nothing else outside the standard library.

```sh
python3 -m venv /path/to/venv
/path/to/venv/bin/pip install --require-hashes --no-deps -r requirements.lock
mkdir -p /path/to/data     # downloads go in their own folder, never next to the scripts
# fetch an extract and Geofabrik's index (small) into /path/to/data, e.g.
#   https://download.geofabrik.de/north-america/us/district-of-columbia-latest.osm.pbf (+ .md5)
#   https://download.geofabrik.de/index-v1.json
/path/to/venv/bin/python -I pipeline/build.py \
  --sources config/sources.local.json --regions config/regions.json \
  --index /path/to/data/index-v1.json \
  --local us/district-of-columbia=/path/to/data/district-of-columbia-latest.osm.pbf \
  --local greater-london=/path/to/data/greater-london-latest.osm.pbf \
  --work /path/to/work --out /path/to/out --partial
```

`--partial` is for a build of a few extracts: coverage holes become warnings and regions without data are
skipped. Without `--local`, `build.py` downloads what `--sources` lists. The steps also run alone:
`pipeline/download.py`, `pipeline/extract.py`, `pipeline/pack.py`, `pipeline/golden.py`, `pipeline/sanity.py`
(each has `--help`). Run Python that reads `.pbf` files with `-I` and pass paths as arguments.

Tests (`unittest`; pytest also collects them):

```sh
/path/to/venv/bin/python -I -m unittest discover -s tests -t .
OSM_TEST_DATA=/path/to/data OSM_TEST_LONDON=1 /path/to/venv/bin/python -I -m unittest discover -s tests -t .
```

The second form adds the golden checks on the real DC and Greater London extracts.

After changing `config/sources.json` or `config/regions.json`, regenerate the known holes and review the diff:
`python -I tools/known_incomplete.py /path/to/index-v1.json`. Then regenerate the backend's strict area and commit it
there as `data/osm-strict-area.json` (the backend's tests check every provider's box against it):
`python -I tools/strict_area.py /path/to/saferoute-backend/data/osm-strict-area.json`.

## Measured (31 Oct 2026, Apple M-series laptop, the two test extracts)

| | DC (21 MB) | Greater London (130 MB) | both |
|---|---|---|---|
| extraction | 0.8 s | 7.1 s | |
| lit ways / places kept | 5,896 / 113 | 188,081 / 1,763 | |
| packing | | | 2.2 s |
| `lighting.pack` | 110 KB alone | 4.6 MB alone | 4,715,745 B (186,954 ways, 475 tiles) |
| `places.pack` | 8.3 KB alone | 165 KB alone | 173,306 B (1,876 places, 404 tiles) |
| golden, same file / build | 26 / 3 tiles, 0 failures | 452 / 334 tiles, 0 failures | 70 s for London |
| whole local build | | | 89 s, peak RSS 1.66 GB |

The peak memory is mostly pyosmium's `IdTracker` bitmap (about 1.2 GB even for DC, because node ids spread
over the whole id range); it is bounded by the id range, not by the file, so the UK should stay near 2–3 GB.
The full 35-file run has not been measured; DESIGN.md section 10 has the estimate.

## Layout

```
pipeline/   download, extract, store, coverage, pack, packfmt, golden, sanity, manifest, build
config/     sources, regions, sentinels, known_incomplete, reviewed_empty, uk_outline, sources.local (test)
tools/      design probes, known_incomplete.py, strict_area.py
tests/      unit, synthetic end-to-end, download (local server), real-extract golden tests
contrib/    the backend repo's daily watchdog (template)
```
