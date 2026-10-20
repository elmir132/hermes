import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402

CFG = jobs.load_config()


def shown(title):
    return jobs.title_matches(title, CFG)[0]


class EntryLevelTests(unittest.TestCase):
    """The owner wants entry-level, post-graduate roles only: no internship experience, no mid or senior levels."""

    def test_mid_and_senior_levels_are_dropped(self):
        for title in ("Software Engineer 2", "Software Engineer II", "Software Engineer III", "Machine Learning Engineer, Level 3",
                      "Senior Software Engineer", "Staff ML Engineer", "Experienced Software Development Engineer",
                      "Mid-Level Backend Engineer", "Software Engineer Intern"):
            self.assertFalse(shown(title), title)

    def test_entry_level_titles_pass_and_rank_above_plain_ones(self):
        plain = jobs.title_matches("Software Engineer", CFG)
        self.assertTrue(plain[0])
        for title in ("Software Engineer, Early Career (NYC)", "Software Engineer - New Grad", "Software Engineer I",
                      "Campus Graduate Masters Full-Time Engineer 2027 AI Engineer I",
                      "Software Engineer, Entry-Level (Grad Fall 2026-Summer 2027)"):
            ok, score, reasons = jobs.title_matches(title, CFG)
            self.assertTrue(ok, title)
            self.assertIn("entry level", reasons)
            self.assertGreater(score, plain[1])

    def test_a_level_two_title_that_says_new_grad_is_kept(self):
        self.assertTrue(shown("Software Engineer II, New Grad"))

    def test_engineer_i_does_not_match_inside_other_words(self):
        self.assertNotIn("entry level", jobs.title_matches("Software Engineer, Infrastructure", CFG)[2])

    def test_three_years_of_experience_is_too_many(self):
        self.assertLessEqual(CFG["max_years"], 3)
        self.assertEqual(jobs.required_years("Requires 3+ years of experience in Python."), 3)


if __name__ == "__main__":
    unittest.main()
