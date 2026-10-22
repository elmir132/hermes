import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_job_alert_worker import main_functions  # noqa: E402

FULL = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/gmail.readonly"]
READ = ["https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/gmail.readonly"]
NAMES = {"_is_read_only", "_scopes_for", "_account_of", "_token_files_for", "_all_token_files", "_load_creds", "_all_creds",
         "_job_alert_accounts", "calendar_add_event"}


class ReadOnlySigninTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name in ("bl13", "bl13-read", "ea", "grin-read"):
            (self.dir / f"token_{name}.json").write_text("{}")
        self.loaded = []

        def load(path, scopes):
            self.loaded.append((Path(path).stem, tuple(scopes)))
            return SimpleNamespace(token=Path(path).stem), None
        self.env = main_functions(NAMES, dict(Path=Path, _HERE=self.dir, _GOOGLE_SCOPES=FULL, _GOOGLE_READ_SCOPES=READ,
                                              signin=SimpleNamespace(load=load),
                                              job_alert_worker=SimpleNamespace(account_names=lambda: {"bl13"})))

    def stems(self, purpose):
        return [p.stem for p in self.env["_token_files_for"](purpose)]

    def test_reading_prefers_the_read_only_sign_in_and_never_reads_an_account_twice(self):
        self.assertEqual(self.stems("read"), ["token_bl13-read", "token_ea", "token_grin-read"])

    def test_writing_never_uses_a_read_only_sign_in(self):
        self.assertEqual(self.stems("write"), ["token_bl13", "token_ea"])

    def test_each_file_is_loaded_with_its_own_permissions(self):
        self.env["_all_creds"]("read")
        self.assertIn(("token_bl13-read", tuple(READ)), self.loaded)
        self.assertIn(("token_ea", tuple(FULL)), self.loaded)
        self.assertEqual([n for n, _ in self.env["_all_creds"]("read")], ["bl13", "ea", "grin"])

    def test_the_job_alert_reader_uses_the_read_only_sign_in_for_its_account(self):
        self.assertEqual(self.env["_job_alert_accounts"](), [("bl13-read", "token_bl13-read")])


class NewEventTimeTests(unittest.TestCase):
    def test_a_time_without_a_zone_is_new_york_time(self):
        sent = {}

        class Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, headers=None, json=None):
                sent.update(json)
                return SimpleNamespace(status_code=200, json=lambda: {"summary": json["summary"], "start": json["start"]})
        from datetime import datetime, timedelta, timezone
        env = main_functions({"calendar_add_event"}, dict(
            _all_creds=lambda purpose="read": [("bl13", SimpleNamespace(token="t"))] if purpose == "write" else [],
            datetime=datetime, timedelta=timedelta, timezone=timezone, _GCAL_API="https://example.test",
            httpx=SimpleNamespace(AsyncClient=Client)))
        asyncio.run(env["calendar_add_event"]("Game", "2026-10-22T16:00", 120))
        self.assertTrue(sent["start"]["dateTime"].endswith("-04:00"), sent["start"])   # EDT in October


if __name__ == "__main__":
    unittest.main()
