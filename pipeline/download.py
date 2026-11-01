#!/usr/bin/env python3
"""Download one Geofabrik extract, verified.

Usage:
  python3 -I pipeline/download.py --url https://download.geofabrik.de/...-latest.osm.pbf \
      --dir DIR [--previous-timestamp ISO] [--max-age-days 7] [--date YYMMDD]

1. HEAD the -latest URL without following redirects: Geofabrik answers 307
   to the dated file (district-of-columbia-261030.osm.pbf), so the file and
   its .md5 are the same snapshot. --date asks for a given day's file instead
   (the golden step's London file must match the UK file's day).
2. GET the dated .md5, then stream the dated file to disk, computing its MD5
   and SHA-256; the MD5 must match.
3. Read osmosis_replication_timestamp from the file header; fail if it is
   missing, older than --previous-timestamp or more than --max-age-days old.
Prints a JSON record (url, dated_url, bytes, md5, sha256, timestamp, path).
Redirects may only stay on the same host. User-Agent per config/sources.json.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

USER_AGENT = 'SafeRoute/1.0 (https://safe-route.app; minhaj@safe-route.app)'
CHUNK = 1 << 20
RETRIES = 3
RETRY_DELAY_S = 10      # tests set 0


class DownloadError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(url, method='GET', follow=True, timeout=60):
    req = urllib.request.Request(url, method=method, headers={'User-Agent': USER_AGENT})
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    return opener.open(req, timeout=timeout)


def _retry(fn, what):
    last = None
    for attempt in range(RETRIES):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise DownloadError(f'{what}: HTTP {e.code}') from e
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if attempt + 1 < RETRIES:
            time.sleep(RETRY_DELAY_S * (attempt + 1))
    raise DownloadError(f'{what}: {last}')


def resolve_dated(latest_url, date=None):
    """The dated URL the -latest URL stands for (or for `date`, YYMMDD)."""
    if date:
        if not re.fullmatch(r'\d{6}', date):
            raise DownloadError(f'bad date {date!r}')
        if not latest_url.endswith('-latest.osm.pbf'):
            raise DownloadError(f'{latest_url} is not a -latest URL')
        return latest_url[:-len('-latest.osm.pbf')] + f'-{date}.osm.pbf'

    def head():
        try:
            with _open(latest_url, method='HEAD', follow=False) as r:
                return r.status, r.headers
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e.code, e.headers
            raise
    status, headers = _retry(head, f'HEAD {latest_url}')
    if status == 200:
        return latest_url
    loc = headers.get('Location')
    if not loc:
        raise DownloadError(f'HEAD {latest_url}: {status} without Location')
    dated = urllib.parse.urljoin(latest_url, loc)
    a, b = urllib.parse.urlsplit(latest_url), urllib.parse.urlsplit(dated)
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        raise DownloadError(f'{latest_url} redirects off host to {dated}')
    if not re.search(r'-\d{6}\.osm\.pbf$', b.path):
        raise DownloadError(f'{latest_url} redirects to an unexpected name {dated}')
    return dated


def fetch_text(url, limit=1 << 16):
    def get():
        with _open(url) as r:
            return r.read(limit + 1)
    data = _retry(get, f'GET {url}')
    if len(data) > limit:
        raise DownloadError(f'{url}: larger than {limit} bytes')
    return data.decode('utf-8', 'replace')


def fetch_file(url, dest):
    """Stream url to dest; returns (bytes, md5, sha256)."""
    def get():
        md5, sha = hashlib.md5(), hashlib.sha256()
        n = 0
        tmp = dest + '.part'
        with _open(url, timeout=120) as r, open(tmp, 'wb') as f:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                md5.update(chunk)
                sha.update(chunk)
                n += len(chunk)
            expected = r.headers.get('Content-Length')
        if expected is not None and int(expected) != n:
            raise ConnectionError(f'short read: {n} of {expected} bytes')
        os.replace(tmp, dest)
        return n, md5.hexdigest(), sha.hexdigest()
    return _retry(get, f'GET {url}')


def file_timestamp(path):
    import osmium
    with osmium.io.Reader(path, osmium.osm.NOTHING) as rd:
        return rd.header().get('osmosis_replication_timestamp') or None


def parse_iso(text):
    return dt.datetime.fromisoformat(text.replace('Z', '+00:00'))


def check_timestamp(stamp, previous=None, max_age_days=7, now=None):
    if not stamp:
        raise DownloadError('no osmosis_replication_timestamp in the header')
    t = parse_iso(stamp)
    now = now or dt.datetime.now(dt.timezone.utc)
    if previous and t < parse_iso(previous):
        raise DownloadError(f'data timestamp {stamp} is older than the previous build ({previous})')
    if now - t > dt.timedelta(days=max_age_days):
        raise DownloadError(f'data timestamp {stamp} is more than {max_age_days} days old')
    if t - now > dt.timedelta(hours=6):
        raise DownloadError(f'data timestamp {stamp} is in the future')


def download(latest_url, dest_dir, previous=None, max_age_days=7, date=None, now=None):
    os.makedirs(dest_dir, exist_ok=True)
    dated = resolve_dated(latest_url, date)
    name = os.path.basename(urllib.parse.urlsplit(dated).path)
    md5_text = fetch_text(dated + '.md5')
    m = re.match(r'\s*([0-9a-f]{32})\b', md5_text)
    if not m:
        raise DownloadError(f'{dated}.md5: no MD5 in {md5_text[:80]!r}')
    path = os.path.join(dest_dir, name)
    size, md5, sha = fetch_file(dated, path)
    if md5 != m.group(1):
        os.remove(path)
        raise DownloadError(f'{name}: MD5 {md5} does not match the published {m.group(1)}')
    stamp = file_timestamp(path)
    try:
        check_timestamp(stamp, previous, max_age_days, now)
    except DownloadError:
        os.remove(path)
        raise
    return {'url': latest_url, 'dated_url': dated, 'bytes': size, 'md5': md5, 'sha256': sha,
            'timestamp': stamp, 'path': path}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', required=True)
    ap.add_argument('--dir', required=True)
    ap.add_argument('--previous-timestamp')
    ap.add_argument('--max-age-days', type=float, default=7)
    ap.add_argument('--date')
    a = ap.parse_args(argv)
    print(json.dumps(download(a.url, a.dir, a.previous_timestamp, a.max_age_days, a.date), indent=1))


if __name__ == '__main__':
    main()
