import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402

REQ_CLEAR = "⚠️ security clearance required"
REQ_CITIZEN = "⚠️ U.S. citizenship required"
REQ_PERSON = "⚠️ U.S. person requirement"
MENTION = "🔎 clearance mentioned; requirement unclear"
EXPORT = "🔎 export-control wording; check role eligibility"


class EligibilityTests(unittest.TestCase):
    def check(self, text, expected):
        self.assertEqual(jobs.eligibility_flags(text), expected, text)

    def test_explicit_requirements_are_flagged(self):
        self.check("Must be able to obtain and maintain a Secret clearance.", [REQ_CLEAR])
        self.check("Active TS/SCI clearance required.", [REQ_CLEAR])
        self.check("Must be a U.S. citizen.", [REQ_CITIZEN])
        self.check("Applicants must be U.S. citizens due to government contract requirements.", [REQ_CITIZEN])
        self.check("This role requires access to ITAR-controlled data; applicants must be U.S. persons.", [REQ_PERSON])

    def test_sentences_that_say_it_is_not_needed_are_not_flagged(self):
        # The costly mistake: warning someone off a role they can take.
        self.check("No security clearance required.", [])
        self.check("Clearance not required for this role.", [])
        self.check("U.S. citizenship is not required.", [])
        self.check("Candidates must be eligible to work in the US; no clearance needed.", [])
        self.check("We sponsor visas. This role does not require U.S. citizenship.", [])

    def test_a_requirement_elsewhere_in_the_posting_still_counts(self):
        self.check("No clearance needed. Some roles on the team require an active Secret clearance.", [REQ_CLEAR])

    def test_mentions_and_export_wording_are_hints_not_requirements(self):
        self.check("Our team supports customers with Top Secret programs.", [MENTION])
        self.check("Experience with export controls (EAR) is a plus.", [EXPORT])

    def test_ordinary_words_are_not_mistaken_for_rules(self):
        self.check("We are looking for someone with an ear for detail.", [])
        self.check("Build backend services in Go. 2+ years of experience.", [])
        self.check("", [])
        self.check(None, [])

    def test_the_short_visa_summary_follows_the_same_rules(self):
        self.assertNotIn("⚠️ citizenship or clearance required", jobs.visa_flags("No security clearance required."))
        self.assertIn("⚠️ citizenship or clearance required", jobs.visa_flags("Must be a U.S. citizen."))


class SameNameCityTests(unittest.TestCase):
    def test_a_bay_area_city_name_in_another_state_is_not_the_bay_area(self):
        cfg = jobs.load_config()
        self.assertFalse(jobs.location_ok("USA - Berkeley, MO", False, cfg))      # a real Boeing listing
        self.assertFalse(jobs.location_ok("Sunnyvale, TX", False, cfg))
        self.assertTrue(jobs.location_ok("Berkeley, CA", False, cfg))
        self.assertTrue(jobs.location_ok("Mountain View, California", False, cfg))
        self.assertTrue(jobs.location_ok("Oakland, CA; Austin, TX", False, cfg))  # one good segment is enough


class SourceStatusTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "t.db")
        self.t0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

    def result(self, name, err=None, n=3):
        company = {"name": name, "provider": "greenhouse", "slug": name.lower()}
        return company, "greenhouse", name.lower(), name, [{}] * n, err

    def test_a_failure_keeps_the_last_good_time_and_count(self):
        jobs._record_source_status(self.db, [self.result("Stripe")], self.t0)
        later = self.t0 + timedelta(days=1)
        status = jobs._record_source_status(self.db, [self.result("Stripe", "HTTP 429", 0)], later)
        row = status["greenhouse:stripe"]
        self.assertEqual(row["last_success_at"], self.t0.isoformat())
        self.assertEqual(row["last_attempt_at"], later.isoformat())
        self.assertEqual((row["last_error"], row["last_result_count"]), ("HTTP 429", 3))

    def test_a_source_that_never_worked_says_never(self):
        status = jobs._record_source_status(self.db, [self.result("Citadel", "HTTP 403", 0)], self.t0)
        summary = jobs._source_status_summary([self.result("Citadel", "HTTP 403", 0)], status)
        self.assertIn("0/1 succeeded", summary)
        self.assertIn("Citadel failed; last good never (HTTP 403)", summary)

    def test_the_summary_counts_successes_and_lists_failures(self):
        results = [self.result("Stripe"), self.result("Salesforce", "HTTP 429", 0)]
        jobs._record_source_status(self.db, [self.result("Salesforce")], self.t0 - timedelta(days=2))
        status = jobs._record_source_status(self.db, results, self.t0)
        summary = jobs._source_status_summary(results, status)
        self.assertIn("Sources: 1/2 succeeded at Oct 19 12:00 UTC.", summary)
        self.assertIn("Salesforce failed; last good Oct 17 12:00 UTC (HTTP 429)", summary)

    def test_the_table_is_created_in_the_jobs_database(self):
        jobs._record_source_status(self.db, [self.result("Stripe")], self.t0)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs_source_status").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
