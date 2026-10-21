"""Pay evidence tests, including the real scan path with deterministic ATS feeds."""
import asyncio
import tempfile
import unittest
from pathlib import Path

import job_compensation as comp
import jobs
from test_jobs import FakeFeeds, ab_job, cfg_with, NOW


class CompensationTests(unittest.TestCase):
    def test_empty_and_unpriced_components_are_unknown(self):
        for text in ('', 'Competitive salary, annual bonus and equity.',
                     'Base salary: $135k. Equity: $200k over four years.',
                     'Total compensation includes base salary $135k and benefits.'):
            with self.subTest(text=text):
                self.assertEqual(comp.analyze(text)['status'], 'unknown')

    def test_explicit_total_ranges(self):
        for text, status in [('Total annual compensation: $170,000–$220,000.', 'meets'),
                             ('Annual total compensation: $150k to $190k.', 'possible'),
                             ('Total compensation: $150k-$165k annually.', 'below'),
                             ('Total compensation: $170.5k annually.', 'meets')]:
            with self.subTest(text=text):
                self.assertEqual(comp.analyze(text)['status'], status)

    def test_sum_only_stated_recurring_components(self):
        result = comp.analyze('Base salary: $135k. Annual bonus: $20k. Annualized equity: $15k.')
        self.assertEqual(result['subtotal'], (170000, 170000))
        self.assertEqual(result['status'], 'meets')
        result = comp.analyze('Base salary: $150k. Annual target bonus: 10%–20% of base salary.')
        self.assertEqual(result['bonus'], (15000, 30000))
        self.assertEqual(result['status'], 'possible')

    def test_annual_equity_and_signon_separate(self):
        result = comp.analyze('Base salary: $140k. Equity: $30k per year. Signing bonus: $50k.')
        self.assertEqual(result['equity'], (30000, 30000))
        self.assertEqual(result['signon'], (50000, 50000))
        self.assertEqual(result['subtotal'], (170000, 170000))
        self.assertIn('one-time', ' '.join(comp.notes(result)))
        result = comp.analyze('Total compensation: $160k annually. Signing bonus: $50k.')
        self.assertEqual(result['status'], 'below')

    def test_first_year_foreign_and_nonannual_not_proof(self):
        for text in ('First-year total compensation: $200k.',
                     'Total compensation: $200k monthly.',
                     'Total compensation: $200k CAD annually.',
                     'Base salary: $200k per month.',
                     'Base salary: $130k. Stock compensation: $200k over four years.'):
            with self.subTest(text=text):
                self.assertEqual(comp.analyze(text)['status'], 'unknown')

    def test_ambiguous_totals_not_cherry_picked(self):
        result = comp.analyze('Total compensation: $150k. Total compensation: $250k.')
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['total'])

    def test_only_trusted_structured_base_supports_target(self):
        self.assertEqual(comp.analyze('', (180000, 200000))['status'], 'unknown')
        self.assertEqual(comp.analyze('', (180000, 200000), True)['status'], 'meets')
        self.assertEqual(comp.analyze('', (100000, 120000), True)['status'], 'unknown')

    def test_config_target_and_format(self):
        cfg = jobs.load_config()
        self.assertEqual(cfg['min_total_comp'], 170000)
        self.assertEqual(cfg['min_pay'], 135000)                      # both targets apply: base and total
        import json
        self.assertEqual(Path(jobs.CONFIG_PATH).read_text(), json.dumps(cfg, indent=1, ensure_ascii=False)+'\n')

    def test_lever_foreign_and_null_currency_not_trusted(self):
        from test_jobs import lv_job
        for currency in ('CAD', None):
            raw = dict(lv_job(1, 'Software Engineer'), salaryRange={
                'currency': currency, 'interval': 'per-year', 'min': 200000, 'max': 250000})
            job = jobs.normalize('lever', raw, 'Acme')
            result = comp.analyze(job['text'], job['pay'], job['pay_basis'] == 'base')
            self.assertEqual(result['status'], 'unknown')

    def test_target_is_configurable_and_bonus_not_guaranteed(self):
        result = comp.analyze('Base salary: $150k. Annual bonus: $20k.', target=180000)
        self.assertEqual(result['status'], 'unknown')
        result = comp.analyze('Base salary: $150k. Annual bonus: $20k.')
        self.assertIn('not guaranteed', ' '.join(comp.notes(result)))


class CompensationScanTests(unittest.TestCase):
    def scan(self, postings):
        feeds = FakeFeeds({'job-board/acme': {'jobs': postings}})
        cfg = cfg_with([{'name': 'Acme', 'provider': 'ashby', 'slug': 'acme'}], max_per_company=20)
        with tempfile.TemporaryDirectory() as directory:
            return asyncio.run(jobs.scan(cfg, feeds, str(Path(directory)/'jobs.db'), now=NOW))

    def test_real_scan_keeps_unknown_and_components_rejects_known_low_total(self):
        text, count, errors = self.scan([
            ab_job(1, 'Software Engineer Unknown'),
            ab_job(2, 'Software Engineer Components', text='Base salary: $140k. Annual bonus: $20k. Annualized equity: $20k.'),
            ab_job(3, 'Software Engineer Low', text='Total annual compensation: $150k–$165k.'),
            ab_job(4, 'Software Engineer Low Base', text='Base salary: $120k. Annual bonus: $30k. Annualized equity: $30k.')])
        self.assertEqual(errors, [])
        self.assertEqual(count, 2)
        self.assertIn('Unknown', text)
        self.assertIn('total pay: unknown', text)
        self.assertIn('Components', text)
        self.assertNotIn('Engineer Low', text)                   # stated total below $170k
        self.assertNotIn('Low Base', text)                       # stated base below $135k, whatever the package

    def test_total_not_mislabelled_as_base(self):
        text, count, _ = self.scan([ab_job(1, 'Software Engineer', text='Total compensation: $180k annually.')])
        self.assertEqual(count, 1)
        self.assertIn('$180,000 stated total', text)
        self.assertNotIn('$180k base', text)

    def test_nonpay_exclusions_unchanged(self):
        text, count, _ = self.scan([
            ab_job(1, 'Senior Software Engineer', text='Total compensation: $250k.'),
            ab_job(2, 'Software Engineer', text='Requires 3+ years of experience. Total compensation: $250k.'),
            ab_job(3, 'Software Engineer Intern', text='Total compensation: $250k.')])
        self.assertEqual(count, 0)

    def test_foreign_structured_salary_not_us_total_evidence(self):
        post = dict(ab_job(1, 'Software Engineer'), compensation={'scrapeableCompensationSalarySummary': '$200K–$250K CAD'})
        text, count, _ = self.scan([post])
        self.assertEqual(count, 1)
        self.assertIn('total pay: unknown', text)
