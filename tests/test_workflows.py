"""The workflow and the watchdog template keep their review fixes (text checks: no YAML parser in CI)."""
import os
import re
import unittest

from tests import helpers

ROOT = helpers.ROOT


def read(*p):
    with open(os.path.join(ROOT, *p), encoding='utf-8') as f:
        return f.read()


class BuildWorkflowTest(unittest.TestCase):
    def setUp(self):
        text = read('.github', 'workflows', 'build.yml')
        self.build, self.publish = text.split('\n  publish:\n')
        self.text = text

    def test_schedule_is_sunday_evening_utc(self):
        self.assertIn("- cron: '17 20 * * 0'", self.text)

    def test_the_build_job_holds_no_write_token(self):
        job = self.build.split('\n  build:\n')[1]
        perms = re.search(r'\n    permissions:\n((?:      .*\n)+)', job).group(1)
        self.assertEqual(perms.strip(), 'contents: read')
        self.assertIn('persist-credentials: false', job)
        step = job.split('- name: Build')[1].split('\n      - ')[0]
        self.assertNotIn('GH_TOKEN', step)
        self.assertNotIn('GH_TOKEN', job.split('    steps:')[0], 'no job-level token')

    def test_the_publish_job_never_reads_a_pbf(self):
        self.assertIn('needs: build', self.publish)
        self.assertNotIn('pipeline/', self.publish)
        self.assertNotIn('.pbf', self.publish)
        for p in ('contents: write', 'id-token: write', 'attestations: write'):
            self.assertIn(p, self.publish)

    def test_retention_is_by_age_and_published_date(self):
        self.assertIn("182 days ago", self.publish)
        self.assertIn('publishedAt', self.publish)
        code = '\n'.join(l for l in self.text.splitlines() if not l.lstrip().startswith('#'))
        self.assertNotIn('createdAt', code)


class WatchdogTemplateTest(unittest.TestCase):
    def setUp(self):
        self.text = read('contrib', 'backend-osm-data-watchdog.yml')
        self.steps = self.text.split('\n    steps:\n')[1]

    def test_age_comes_from_the_manifest_not_created_at(self):
        self.assertIn('data_as_of_epoch', self.steps)
        self.assertNotIn('createdAt --jq', self.steps)
        self.assertIn('-gt 204', self.steps)           # 8.5 days, in hours

    def test_runs_every_6_hours_and_spares_geofabrik_after_a_failure(self):
        self.assertIn("- cron: '41 */6 * * *'", self.text)
        self.assertIn('-lt 72', self.steps)
        self.assertIn('in_progress', self.steps)

    def test_pin_step_survives_a_dead_token_and_verifies_strictly(self):
        pin = self.steps.split('- name: Pin a newer release')[1].split('- name: Fail')[0]
        self.assertIn('if: ${{ !cancelled() }}', pin)
        self.assertIn('secrets.OSM_DATA_TOKEN', self.steps.split('- name: Pin a newer release')[0])
        self.assertNotIn('OSM_DATA_TOKEN', pin)
        for s in ('--source-ref refs/heads/main', '--deny-self-hosted-runners', 'data/osm-hold',
                  'is not newer than the pinned data', "man.get('tag') != tag", 'OSM_PIN_MODE'):
            self.assertIn(s, self.text)


if __name__ == '__main__':
    unittest.main()
