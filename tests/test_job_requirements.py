import asyncio
import tempfile
from pathlib import Path
import unittest

import job_requirements
import job_store
import jobs
from test_jobs import ab_job, cfg_with, FakeFeeds, NOW


class RequirementHintsTests(unittest.TestCase):
    def test_unknown_is_not_claimed_beginner(self):
        self.assertIn('unverified', ' '.join(job_requirements.notes('Software Engineer', '')))
        self.assertEqual(job_requirements.notes('Software Engineer, New Grad', ''), [])

    def test_one_and_two_years_visible(self):
        for years in (1, 2):
            self.assertIn(f'{years}+ years', ' '.join(job_requirements.notes('Software Engineer', '', years)))

    def test_graduation_window_start_and_degree(self):
        result = ' '.join(job_requirements.notes('Software Engineer',
            'Graduating December 2026 – June 2027. Start date: summer 2027. PhD required.'))
        for value in ('December 2026', 'June 2027', 'summer 2027', 'PhD explicitly required'):
            self.assertIn(value, result)
        self.assertIn('Class of 2026', ' '.join(job_requirements.notes('Software Engineer, Class of 2026', '')))

    def test_preferred_phd_and_no_dates_not_fabricated(self):
        result = ' '.join(job_requirements.notes('New Grad Software Engineer', 'PhD preferred. Start a new project.'))
        self.assertEqual(result, '')

    def test_bounded_literal_input(self):
        result = job_requirements.notes('Software Engineer', 'Class of 2027\x00 '+('x'*1000))
        self.assertTrue(all(len(note) < 170 for note in result))
        self.assertNotIn('\x00', ' '.join(result))

    def test_cached_flags_are_refreshed_without_duplicates(self):
        job = {'title':'Software Engineer', 'text':'Requires 2 years of experience.',
               'flags':['pay: not stated', 'Entry-level fit: unverified']}
        first = job_requirements.flags(job)
        self.assertNotIn('Entry-level fit: unverified', first)
        self.assertEqual(first, job_requirements.flags(dict(job, flags=first)))

    def test_scan_and_persistent_batch_show_hints(self):
        cfg = cfg_with([{'name':'Acme','provider':'ashby','slug':'acme'}])
        feeds = FakeFeeds({'job-board/acme': {'jobs': [ab_job(1, 'Software Engineer',
            text='Requires 2 years of experience. Graduating in 2027. Start date summer 2027.')]}})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'jobs.db'
            _, count, errors = asyncio.run(jobs.scan(cfg, feeds, path, now=NOW, mark=False, persist=True))
            text, shown, _ = job_store.batch(path, cfg, now=NOW)
        self.assertEqual(errors, [])
        self.assertEqual(count, 1)
        self.assertEqual(len(shown), 1)
        for value in ('2+ years', 'Graduation:', 'Start date:'):
            self.assertIn(value, text)
