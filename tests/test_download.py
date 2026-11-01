"""Download checks against a local HTTP server: redirect to the dated file, MD5, SHA-256, timestamps."""
import datetime as dt
import hashlib
import http.server
import os
import tempfile
import threading
import unittest

from tests import helpers
from pipeline import download

download.RETRY_DELAY_S = 0


class _Handler(http.server.BaseHTTPRequestHandler):
    files = {}            # path -> bytes
    redirects = {}        # path -> Location
    seen_agents = []

    def log_message(self, *a):
        pass

    def _serve(self, body):
        self.seen_agents.append(self.headers.get('User-Agent'))
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
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_HEAD(self):
        self._serve(False)

    def do_GET(self):
        self._serve(True)


class DownloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.base = f'http://127.0.0.1:{cls.srv.server_address[1]}'
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        now = dt.datetime.now(dt.timezone.utc)
        cls.fresh = (now - dt.timedelta(hours=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
        cls.stale = (now - dt.timedelta(days=9)).strftime('%Y-%m-%dT%H:%M:%SZ')
        for name, stamp in (('fresh', cls.fresh), ('stale', cls.stale)):
            p = os.path.join(cls.tmp.name, name + '.osm.pbf')
            helpers.write_osm(p, [(1, 5, 5, {'amenity': 'police'})], timestamp=stamp)
            with open(p, 'rb') as f:
                data = f.read()
            _Handler.files[f'/x/{name}-261030.osm.pbf'] = data
            _Handler.files[f'/x/{name}-261030.osm.pbf.md5'] = (
                hashlib.md5(data).hexdigest() + f'  {name}-261030.osm.pbf\n').encode()
            _Handler.redirects[f'/x/{name}-latest.osm.pbf'] = f'{cls.base}/x/{name}-261030.osm.pbf'
            setattr(cls, name + '_bytes', data)
        _Handler.files['/x/bad-261030.osm.pbf'] = cls.fresh_bytes
        _Handler.files['/x/bad-261030.osm.pbf.md5'] = b'0' * 32 + b'  bad-261030.osm.pbf\n'
        _Handler.redirects['/x/bad-latest.osm.pbf'] = f'{cls.base}/x/bad-261030.osm.pbf'
        _Handler.redirects['/x/away-latest.osm.pbf'] = 'http://example.invalid/x/away-261030.osm.pbf'

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def test_ok(self):
        d = os.path.join(self.tmp.name, 'ok')
        rec = download.download(f'{self.base}/x/fresh-latest.osm.pbf', d)
        self.assertEqual(rec['dated_url'], f'{self.base}/x/fresh-261030.osm.pbf')
        self.assertEqual(rec['sha256'], hashlib.sha256(self.fresh_bytes).hexdigest())
        self.assertEqual(rec['bytes'], len(self.fresh_bytes))
        self.assertEqual(rec['timestamp'], self.fresh)
        self.assertTrue(os.path.exists(rec['path']))
        self.assertIn(download.USER_AGENT, _Handler.seen_agents)

    def test_bad_md5_is_refused_and_removed(self):
        d = os.path.join(self.tmp.name, 'bad')
        with self.assertRaisesRegex(download.DownloadError, 'MD5'):
            download.download(f'{self.base}/x/bad-latest.osm.pbf', d)
        self.assertEqual(os.listdir(d), [])

    def test_stale_and_older_than_previous(self):
        with self.assertRaisesRegex(download.DownloadError, 'days old'):
            download.download(f'{self.base}/x/stale-latest.osm.pbf', os.path.join(self.tmp.name, 's'))
        with self.assertRaisesRegex(download.DownloadError, 'older than the previous'):
            later = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            download.download(f'{self.base}/x/fresh-latest.osm.pbf', os.path.join(self.tmp.name, 'p'), previous=later)

    def test_off_host_redirect_refused(self):
        with self.assertRaisesRegex(download.DownloadError, 'off host'):
            download.resolve_dated(f'{self.base}/x/away-latest.osm.pbf')

    def test_dated_for_a_given_day(self):
        self.assertEqual(download.resolve_dated('https://h/e/greater-london-latest.osm.pbf', '261031'),
                         'https://h/e/greater-london-261031.osm.pbf')
        rec = download.download(f'{self.base}/x/fresh-latest.osm.pbf', os.path.join(self.tmp.name, 'd'), date='261030')
        self.assertEqual(rec['md5'], hashlib.md5(self.fresh_bytes).hexdigest())

    def test_missing_file_is_an_error(self):
        with self.assertRaises(download.DownloadError):
            download.download(f'{self.base}/x/none-latest.osm.pbf', os.path.join(self.tmp.name, 'n'), date='261030')


if __name__ == '__main__':
    unittest.main()
