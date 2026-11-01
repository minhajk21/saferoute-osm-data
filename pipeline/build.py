#!/usr/bin/env python3
"""One whole build: download, extract, pack, golden, sanity, manifest.

Usage (CI):
  python3 -I pipeline/build.py --sources config/sources.json --regions config/regions.json \
      --work "$RUNNER_TEMP/work" --out dist [--previous prev/manifest.json] [--allow-count-change]

Usage (local, files already on disk; no downloads):
  python3 -I pipeline/build.py --sources config/sources.local.json --regions config/regions.json \
      --index /path/index-v1.json --local us/district-of-columbia=/path/dc.osm.pbf \
      --local greater-london=/path/london.osm.pbf --work W --out O --partial

Extracts are downloaded and processed ONE AT A TIME and deleted as soon as
they are in the store, except the ones the golden step reads (DC, and the
Greater London file of the same day as the UK file). Exit status: 0 when the
build, the golden step and the sanity checks all pass; 1 otherwise (the
outputs are still written, so a failed run can be inspected).
"""
import argparse
import hashlib
import json
import os
import resource
import shutil
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import download, extract, golden, manifest, pack, regions, sanity, store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_FREE_BYTES = 6 * 10**9


def peak_rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / 2**20 if sys.platform == 'darwin' else r / 1024, 1)


def log(msg):
    print(f'[build {time.strftime("%H:%M:%S")}] {msg}', flush=True)


def sha256_and_size(path):
    h = hashlib.sha256()
    md5 = hashlib.md5()
    n = 0
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
            md5.update(chunk)
            n += len(chunk)
    return n, md5.hexdigest(), h.hexdigest()


def golden_pair(name, pbf, polygon_id, lighting_mode, margin, args, index_path, work, main_out, cross):
    """Same-file check (a pack built from this file alone, margin 0) and, when
    `cross`, the build's own packs against this file (margin `margin`)."""
    res = {}
    gdir = os.path.join(work, f'golden-{name}')
    shutil.rmtree(gdir, ignore_errors=True)
    os.makedirs(gdir)
    db = store.open_store(os.path.join(gdir, 'store.sqlite'))
    mask = regions.lighting_mask(regions.load(args.regions)) if lighting_mode == 'boxes' else None
    extract.extract(pbf, polygon_id, db, lighting_mode, mask)
    db.close()
    pack.build(os.path.join(gdir, 'store.sqlite'), index_path, args.regions, os.path.join(gdir, 'out'))
    boxes = args.regions if lighting_mode == 'boxes' else None
    for label, out, m in (('same_file', os.path.join(gdir, 'out'), 0), ('build', main_out, margin)):
        if label == 'build' and not cross:
            continue
        ns = argparse.Namespace(pbf=pbf, index=index_path, polygon_id=polygon_id,
                                places_pack=os.path.join(out, 'places.pack'),
                                lighting_pack=os.path.join(out, 'lighting.pack'),
                                lighting_boxes=boxes, margin_tiles=m, box=None)
        rep = golden.run(ns)
        res[label] = rep
        log(f"golden {name}/{label}: ok={rep['ok']} domain={rep['domain_tiles']} failures={rep['failure_count']} "
            f"({rep['seconds']['total']} s)")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sources', required=True)
    ap.add_argument('--regions', required=True)
    ap.add_argument('--work', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--index', help='local index-v1.json instead of downloading it')
    ap.add_argument('--local', action='append', default=[], metavar='ID=PATH',
                    help='use this file for source/golden ID instead of downloading it')
    ap.add_argument('--previous', help="the previous release's manifest.json")
    ap.add_argument('--allow-count-change', action='store_true')
    ap.add_argument('--partial', action='store_true', help='local test build of a few extracts (see sanity.py)')
    ap.add_argument('--no-golden', action='store_true')
    ap.add_argument('--keep-downloads', action='store_true')
    ap.add_argument('--tag', help='release tag to record (default data-YYYY-MM-DD of the build)')
    args = ap.parse_args(argv)

    t_all = time.monotonic()
    with open(args.sources, encoding='utf-8') as f:
        cfg = json.load(f)
    regions_cfg = regions.load(args.regions)
    download.USER_AGENT = cfg.get('user_agent', download.USER_AGENT)
    local = dict(kv.split('=', 1) for kv in args.local)
    previous = None
    if args.previous and os.path.exists(args.previous):
        with open(args.previous, encoding='utf-8') as f:
            previous = json.load(f)
    prev_ts = {s['id']: s['timestamp'] for s in (previous or {}).get('sources', [])}
    os.makedirs(args.work, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)
    dl_dir = os.path.join(args.work, 'downloads')
    timings = {}

    free = shutil.disk_usage(args.work).free
    if not local and free < MIN_FREE_BYTES:
        raise SystemExit(f'only {free / 1e9:.1f} GB free in {args.work}; need {MIN_FREE_BYTES / 1e9:.0f} GB')

    # ---- index (the extracts' polygons)
    if args.index:
        index_path = args.index
        idx_url = cfg['index_url'] + ' (local copy)'
    else:
        index_path = os.path.join(args.work, 'index-v1.json')
        size, md5, sha = download.fetch_file(cfg['index_url'], index_path)
        idx_url = cfg['index_url']
    size, _, sha = sha256_and_size(index_path)
    index_info = {'url': idx_url, 'bytes': size, 'sha256': sha}

    # ---- extracts, one at a time
    store_path = os.path.join(args.work, 'store.sqlite')
    if os.path.exists(store_path):
        os.remove(store_path)
    db = store.open_store(store_path)
    mask = regions.lighting_mask(regions_cfg)
    golden_cfg = [] if args.no_golden else cfg.get('golden', [])
    keep_for_golden = {g['source'] for g in golden_cfg if 'source' in g}
    kept = {}
    dated = {}
    for src in cfg['sources']:
        sid = src['id']
        t = time.monotonic()
        if sid in local:
            path = local[sid]
            n, md5, sha = sha256_and_size(path)
            info = {'url': src['url'], 'dated_url': None, 'bytes': n, 'md5': md5, 'sha256': sha, 'local': True}
            download.check_timestamp(extract.header_timestamp(path), prev_ts.get(sid), max_age_days=36500)
        else:
            rec = download.download(src['url'], dl_dir, previous=prev_ts.get(sid))
            path = rec.pop('path')
            info = rec
            dated[sid] = rec['dated_url']
        t_dl = time.monotonic() - t
        rec = extract.extract(path, sid, db, src['lighting'], mask if src['lighting'] == 'boxes' else None, info)
        timings[sid] = {'download_s': round(t_dl, 1), **rec['timings']}
        log(f"{sid}: {info['bytes'] / 1e6:.0f} MB, data {rec['timestamp']}, lit ways {rec['stats']['lit_ways_kept']}, "
            f"places {sum(rec['stats']['places'].values())}, {time.monotonic() - t:.1f} s, peak RSS {peak_rss_mb()} MB")
        if sid in keep_for_golden or sid in local or args.keep_downloads:
            kept[sid] = path
        else:
            os.remove(path)
    db.close()

    # ---- pack
    t = time.monotonic()
    rep = pack.build(store_path, index_path, args.regions, args.out)
    timings['pack_s'] = round(time.monotonic() - t, 1)
    log(f"packs: lighting {rep['lighting']['bytes']:,} B ({rep['lighting']['ways']:,} ways), "
        f"places {rep['places']['bytes']:,} B; {timings['pack_s']} s")

    # ---- golden
    golden_res = {}
    ok = True
    srcs_by_id = {s['id']: s for s in cfg['sources']}
    for g in golden_cfg:
        t = time.monotonic()
        if 'source' in g:
            if g['source'] not in kept:
                log(f"golden {g['name']}: source {g['source']} not in this build, skipped")
                continue
            pbf, pid, mode, cross = kept[g['source']], g['source'], srcs_by_id[g['source']]['lighting'], True
        else:
            pid = g['id']
            if pid in local:
                pbf = local[pid]
            else:
                stamp = dated.get(g.get('same_date_as'))
                if not stamp:
                    log(f"golden {g['name']}: {g.get('same_date_as')} not downloaded in this build, skipped")
                    continue
                day = stamp.rsplit('-', 1)[1][:6]
                pbf = download.download(g['url'], dl_dir, date=day)['path']
            # Compare the build's packs with this other file only when it is the
            # same OSM snapshot as the source it stands in for; otherwise any
            # edit between the two would show as a failure.
            twin = g.get('same_date_as')
            cross = False
            if twin in srcs_by_id:
                db = store.open_store(store_path)
                twin_ts = store.sources(db).get(twin, {}).get('timestamp')
                db.close()
                own_ts = extract.header_timestamp(pbf)
                cross = twin_ts is not None and twin_ts == own_ts
                if not cross:
                    log(f"golden {g['name']}: {own_ts} is not the snapshot of {twin} ({twin_ts}); "
                        f"only the same-file check runs")
            mode = 'whole' if srcs_by_id.get(twin, {}).get('lighting') == 'whole' else 'boxes'
        res = golden_pair(g['name'], pbf, pid, mode, g.get('margin_tiles', 1), args, index_path, args.work,
                          args.out, cross)
        golden_res[g['name']] = {k: {kk: v.get(kk) for kk in ('ok', 'domain_tiles', 'places', 'lighting',
                                                              'failure_count', 'failures', 'seconds')}
                                 for k, v in res.items()}
        ok = ok and all(v['ok'] for v in res.values())
        timings[f"golden_{g['name']}_s"] = round(time.monotonic() - t, 1)
        if pbf not in local.values() and not args.keep_downloads and os.path.exists(pbf):
            os.remove(pbf)

    # ---- sanity
    db = store.open_store(store_path)
    srcs = store.sources(db)
    db.close()
    sources_report = {sid: manifest.source_entry(sid, s) for sid, s in srcs.items()}
    with open(os.path.join(args.work, 'sources-report.json'), 'w', encoding='utf-8') as f:
        json.dump(sources_report, f, indent=1)
    sanity_args = argparse.Namespace(
        report=os.path.join(args.out, 'pack-report.json'), places_pack=os.path.join(args.out, 'places.pack'),
        lighting_pack=os.path.join(args.out, 'lighting.pack'),
        sources_report=os.path.join(args.work, 'sources-report.json'), previous=args.previous,
        sentinels=os.path.join(ROOT, 'config', 'sentinels.json'),
        known=os.path.join(ROOT, 'config', 'known_incomplete.json'),
        uk_outline=os.path.join(ROOT, 'config', 'uk_outline.json'), regions=args.regions,
        allow_count_change=args.allow_count_change, partial=args.partial)
    t = time.monotonic()
    san = sanity.run(sanity_args)
    timings['sanity_s'] = round(time.monotonic() - t, 1)
    log(f"sanity: ok={san['ok']} errors={len(san['errors'])} warnings={len(san['warnings'])}")
    for e in san['errors']:
        log(f'  ERROR {e}')
    ok = ok and san['ok']

    # ---- manifest, notes, keepalive record
    for name in ('ATTRIBUTION.txt', 'LICENSE-DATA.md'):
        shutil.copyfile(os.path.join(ROOT, name), os.path.join(args.out, name))
    commit = os.environ.get('GITHUB_SHA')
    run_url = None
    if os.environ.get('GITHUB_RUN_ID'):
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    man = manifest.build_manifest(args.out, srcs, rep, regions_cfg, index_info, golden_res, san, commit, run_url)
    man_path = os.path.join(args.out, 'manifest.json')
    manifest.write_json(man_path, man)
    tag = args.tag or 'data-' + man['built_at'][:10]
    with open(os.path.join(args.work, 'RELEASE_NOTES.md'), 'w', encoding='utf-8') as f:
        f.write(manifest.release_notes(man, tag))
    manifest.write_json(os.path.join(args.work, 'LATEST.json'), manifest.latest(man, tag, manifest.sha256_file(man_path)))
    timings['total_s'] = round(time.monotonic() - t_all, 1)
    timings['peak_rss_mb'] = peak_rss_mb()
    manifest.write_json(os.path.join(args.work, 'build-report.json'),
                        {'ok': ok, 'tag': tag, 'timings': timings, 'sanity': san, 'golden': golden_res})
    log(f"done: ok={ok}, {timings['total_s']} s, peak RSS {timings['peak_rss_mb']} MB, tag {tag}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
