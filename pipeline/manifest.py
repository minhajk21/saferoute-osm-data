"""manifest.json, LATEST.json and the release notes for one build.

manifest.json is what the backend pins (by its own SHA-256) and checks every
asset against: each source extract's URL, size, MD5, SHA-256 and OSM
timestamp; each asset's size, SHA-256 and counts; the tool versions; the
build commit; dataAsOf (the OLDEST source timestamp, so the data is never
claimed newer than its stalest part); the region boxes; coverage; the golden
and sanity results.
"""
import datetime as dt
import hashlib
import json
import os
import platform
import sqlite3
import sys

ATTRIBUTION = ('© OpenStreetMap contributors. Data under the Open Database License 1.0, '
               'from Geofabrik extracts (download.geofabrik.de).')
REPO = 'https://github.com/minhajk21/saferoute-osm-data'


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def tool_versions():
    import osmium.version as ov
    return {'python': platform.python_version(), 'pyosmium': ov.pyosmium_release,
            'libosmium': ov.libosmium_version, 'sqlite': sqlite3.sqlite_version, 'platform': sys.platform}


def source_entry(sid, s):
    st = s['stats']
    return {'id': sid, 'url': s.get('url'), 'dated_url': s.get('dated_url'), 'bytes': s.get('bytes'),
            'md5': s.get('md5'), 'sha256': s.get('sha256'), 'timestamp': s['timestamp'], 'lighting': s['lighting'],
            'counts': {'places': sum(st['places'].values()), 'lit_ways': st['lit_ways_kept'],
                       'lit_points': st['lit_points_kept']},
            'seconds': s.get('timings')}


def build_manifest(out_dir, sources, report, regions_cfg, index_info, golden, sanity, commit=None, run_url=None,
                   tag=None):
    assets = {}
    for name in ('lighting.pack', 'places.pack', 'ATTRIBUTION.txt', 'LICENSE-DATA.md'):
        p = os.path.join(out_dir, name)
        assets[name] = {'bytes': os.path.getsize(p), 'sha256': sha256_file(p)}
    assets['lighting.pack'].update(kind='lighting', ways=report['lighting']['ways'],
                                   tiles=report['lighting']['tiles_with_data'],
                                   complete_tiles=report['lighting']['complete_tiles'],
                                   points=report['lighting']['points_stored'])
    assets['places.pack'].update(kind='places', elements=sum(report['places'][k] for k in ('node', 'way', 'relation')),
                                 by_kind=report['places']['by_kind'], tiles=report['places']['tiles_with_data'],
                                 complete_tiles=report['places']['complete_tiles'])
    regions = []
    for r in regions_cfg['regions']:
        rr = report['regions'][r['name']]
        regions.append({'name': r['name'], 'box': r['box'], 'margin_km': r.get('margin_km', 0),
                        'places': rr['places'], 'lit_ways': rr['lit_ways'],
                        'places_incomplete_tiles': len(rr['places_incomplete_tiles']),
                        'lighting_incomplete_tiles': len(rr['lighting_incomplete_tiles'])})
    return {
        'schema': 1,
        'name': 'saferoute-osm-data',
        # The release this manifest belongs to: an unpinned reader fetches the
        # packs from this tag, not from "latest" (which may move between its
        # requests).
        'tag': tag,
        'built_at': iso(report['built_at']),
        'data_as_of': iso(report['data_as_of']),
        'data_as_of_epoch': report['data_as_of'],
        'licence': 'ODbL-1.0',
        'attribution': ATTRIBUTION,
        'format': {'pack_version': 1, 'tile_size_e7': 200000, 'spec': 'pipeline/packfmt.py'},
        'build': {'repository': REPO, 'commit': commit, 'run': run_url, 'tools': tool_versions()},
        'index': index_info,
        'sources': [source_entry(sid, s) for sid, s in sorted(sources.items())],
        'assets': assets,
        'regions': regions,
        'reviewed_empty': report.get('reviewed_empty'),
        'golden': golden,
        'sanity': {k: sanity.get(k) for k in ('ok', 'errors', 'warnings', 'notes', 'sentinels_checked', 'strict')},
    }


def latest(manifest, tag, manifest_sha256):
    return {'tag': tag, 'built_at': manifest['built_at'], 'data_as_of': manifest['data_as_of'],
            'data_as_of_epoch': manifest['data_as_of_epoch'], 'manifest_sha256': manifest_sha256,
            'assets': {k: {'bytes': v['bytes'], 'sha256': v['sha256']} for k, v in manifest['assets'].items()},
            'counts': {'lit_ways': manifest['assets']['lighting.pack']['ways'],
                       'places': manifest['assets']['places.pack']['elements']}}


def release_notes(manifest, tag):
    commit = manifest['build']['commit'] or 'unknown'
    lines = [
        f"OpenStreetMap data for SafeRoute street lighting and Find Help, {tag}.",
        '',
        f"- Data as of {manifest['data_as_of']} (the oldest of {len(manifest['sources'])} Geofabrik extracts); "
        f"built {manifest['built_at']}.",
        f"- lighting.pack: {manifest['assets']['lighting.pack']['ways']:,} lit ways, "
        f"{manifest['assets']['lighting.pack']['bytes']:,} bytes.",
        f"- places.pack: {manifest['assets']['places.pack']['elements']:,} places, "
        f"{manifest['assets']['places.pack']['bytes']:,} bytes.",
        f"- Checksums, sources and counts: manifest.json. Build provenance: GitHub artifact attestation.",
        '',
        f"{ATTRIBUTION} The packs are a Derivative Database under the ODbL 1.0 (LICENSE-DATA.md).",
        f"The scripts that built them, at this build's commit: {REPO}/tree/{commit}",
    ]
    return '\n'.join(lines) + '\n'


def write_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write('\n')
