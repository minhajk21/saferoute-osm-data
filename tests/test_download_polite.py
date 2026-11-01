"""Politeness to Geofabrik: preflight before any download, Retry-After, Range resume."""
import datetime as dt
import hashlib
import http.server
import os
import tempfile
import threading
import unittest
import urllib.error

from tests import helpers
from pipeline import download

download.RETRY_DELAY_S = 0


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    files = {}
    redirects = {}
    cut_once = set()        # paths whose first full GET is cut off half-way
    gets = []               # (path, Range header)

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(False)

    def do_GET(self):
        self._serve(True)

    def _serve(self, body):
        if self.path in self.redirects:
            self.send_response(307)
            self.send_header('Location', self.redirects[self.path])
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        data = self.files.get(self.path)
        if data is None:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        rng = self.headers.get('Range')
        if body:
            self.gets.append((self.path, rng))
        if body and rng and rng.startswith('bytes='):
            start = int(rng[6:].split('-')[0])
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{len(data) - 1}/{len(data)}')
            self.send_header('Content-Length', str(len(data) - start))
            self.end_headers()
            self.wfile.write(data[start:])
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Accept-Ranges', 'bytes')
        if body and self.path in self.cut_once:
            self.cut_once.discard(self.path)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data[:len(data) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.end_headers()
        if body:
            self.wfile.write(data)


class PoliteDownloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.base = f'http://127.0.0.1:{cls.srv.server_address[1]}'
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
        p = os.path.join(cls.tmp.name, 'a.osm.pbf')
        helpers.write_osm(p, [(i, 5 + i, 5, {'amenity': 'police', 'name': f'p{i}'}) for i in range(1, 400)],
                          timestamp=stamp)
        with open(p, 'rb') as f:
            cls.data = f.read()
        for name in ('a', 'cut'):
            _Handler.files[f'/x/{name}-261030.osm.pbf'] = cls.data
            _Handler.files[f'/x/{name}-261030.osm.pbf.md5'] = (
                hashlib.md5(cls.data).hexdigest() + f'  {name}-261030.osm.pbf\n').encode()
            _Handler.redirects[f'/x/{name}-latest.osm.pbf'] = f'{cls.base}/x/{name}-261030.osm.pbf'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def test_preflight_names_every_bad_url_and_downloads_nothing(self):
        _Handler.gets.clear()
        urls = [f'{self.base}/x/a-latest.osm.pbf', f'{self.base}/x/gone-latest.osm.pbf',
                f'{self.base}/x/also-gone-latest.osm.pbf']
        with self.assertRaisesRegex(download.DownloadError, r'2 of 3 extracts did not resolve.*gone.*also-gone'):
            download.preflight(urls)
        self.assertEqual(_Handler.gets, [], 'a HEAD only: no file was fetched')
        ok = download.preflight(urls[:1])
        self.assertEqual(ok, {urls[0]: f'{self.base}/x/a-261030.osm.pbf'})

    def test_download_uses_the_preflight_url(self):
        d = os.path.join(self.tmp.name, 'pre')
        rec = download.download('https://unused.invalid/x/a-latest.osm.pbf', d,
                                dated=f'{self.base}/x/a-261030.osm.pbf')
        self.assertEqual(rec['sha256'], hashlib.sha256(self.data).hexdigest())

    def test_a_cut_off_download_resumes_with_range(self):
        _Handler.gets.clear()
        _Handler.cut_once.add('/x/cut-261030.osm.pbf')
        d = os.path.join(self.tmp.name, 'cut')
        rec = download.download(f'{self.base}/x/cut-latest.osm.pbf', d)
        self.assertEqual(rec['md5'], hashlib.md5(self.data).hexdigest())
        self.assertEqual(rec['bytes'], len(self.data))
        pbf_gets = [g for g in _Handler.gets if g[0] == '/x/cut-261030.osm.pbf']
        self.assertEqual(len(pbf_gets), 2)
        self.assertIsNone(pbf_gets[0][1])
        self.assertEqual(pbf_gets[1][1], f'bytes={len(self.data) // 2}-')
        self.assertEqual(sorted(os.listdir(d)), ['cut-261030.osm.pbf'])

    def test_retry_after_is_parsed(self):
        self.assertEqual(download.retry_after_s({'Retry-After': '120'}), 120)
        now = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(download.retry_after_s({'Retry-After': 'Sun, 01 Nov 2026 12:01:30 GMT'}, now), 90)
        self.assertIsNone(download.retry_after_s({'Retry-After': 'soon'}))
        self.assertIsNone(download.retry_after_s({}))

    def test_retry_after_is_waited_out_and_capped(self):
        waits = []
        real = download.time.sleep
        download.time.sleep = waits.append
        try:
            calls = []

            def busy():
                calls.append(1)
                if len(calls) < 3:
                    raise urllib.error.HTTPError('u', 429, 'busy', {'Retry-After': '45'}, None)
                return 'ok'
            self.assertEqual(download._retry(busy, 'GET u'), 'ok')
            self.assertEqual(waits, [45, 45])

            def banned():
                raise urllib.error.HTTPError('u', 503, 'down', {'Retry-After': '86400'}, None)
            with self.assertRaisesRegex(download.DownloadError, 'more than we wait'):
                download._retry(banned, 'GET u')
        finally:
            download.time.sleep = real


if __name__ == '__main__':
    unittest.main()
