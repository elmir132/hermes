import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import signin  # noqa: E402


class RefreshError(Exception):
    """Stands in for google.auth.exceptions.RefreshError (same class name), so no Google install is needed."""


class TransportError(Exception):
    pass


class FakeCreds:
    def __init__(self, expired=True, refresh_token="r", valid_after=True, fail=None):
        self.expired, self.refresh_token, self.valid, self._after, self._fail = expired, refresh_token, not expired, valid_after, fail

    @classmethod
    def factory(cls, **kw):
        class Factory:
            @staticmethod
            def from_authorized_user_file(path, scopes):
                return cls(**kw)
        return Factory

    def refresh(self, request):
        if self._fail:
            raise self._fail
        self.valid, self.expired = self._after, False

    def to_json(self):
        return '{"token": "new"}'


class ClassifyTests(unittest.TestCase):
    def test_invalid_grant_means_sign_in_again_and_network_trouble_does_not(self):
        self.assertEqual(signin.classify(RefreshError("('invalid_grant: Bad Request', {...})")), signin.EXPIRED)
        self.assertEqual(signin.classify(RefreshError("Token has been expired or revoked.")), signin.EXPIRED)
        self.assertEqual(signin.classify(TransportError("connection reset")), signin.TRANSIENT)
        self.assertEqual(signin.classify(TimeoutError()), signin.TRANSIENT)
        self.assertEqual(signin.classify(ValueError("bad json")), signin.BROKEN)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "token_ea.json"
        self.path.write_text('{"token": "old"}')

    def load(self, **kw):
        return signin.load(self.path, [], FakeCreds.factory(**kw), lambda: None)

    def test_a_refresh_is_saved_atomically_and_privately(self):
        creds, problem = self.load()
        self.assertIsNotNone(creds)
        self.assertIsNone(problem)
        self.assertEqual(self.path.read_text(), '{"token": "new"}')
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["token_ea.json"])     # no temporary file left behind

    def test_an_expired_sign_in_and_a_network_failure_are_told_apart(self):
        self.assertEqual(self.load(fail=RefreshError("invalid_grant")), (None, signin.EXPIRED))
        self.assertEqual(self.load(fail=TransportError("down")), (None, signin.TRANSIENT))
        self.assertEqual(self.path.read_text(), '{"token": "old"}')                # untouched when the refresh failed

    def test_a_missing_file_or_no_refresh_token(self):
        self.assertEqual(signin.load(self.dir / "nope.json", [], FakeCreds.factory(), lambda: None), (None, signin.MISSING))
        self.assertEqual(self.load(refresh_token=None), (None, signin.EXPIRED))


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.state = signin.AlertState(Path(tempfile.mkdtemp()) / "signin_state.json")

    def test_a_break_is_announced_once_and_the_fix_once(self):
        self.assertEqual(self.state.changes({"ea": None, "bl14": signin.EXPIRED}), (["bl14"], []))
        self.assertEqual(self.state.changes({"ea": None, "bl14": signin.EXPIRED}), ([], []))     # next morning: quiet
        self.assertEqual(self.state.changes({"ea": None, "bl14": None}), ([], ["bl14"]))
        self.assertEqual(self.state.changes({"ea": None, "bl14": None}), ([], []))

    def test_a_network_blip_is_not_news(self):
        self.assertEqual(self.state.changes({"ea": signin.TRANSIENT}), ([], []))

    def test_a_deleted_token_file_is_not_reported_as_fixed(self):
        self.state.changes({"bl14": signin.EXPIRED})
        self.assertEqual(self.state.changes({"ea": None}), ([], []))
        self.assertIn("bl14", json.loads(self.state.path.read_text()))

    def test_the_message_says_what_to_run_and_when_everything_is_down(self):
        text = signin.alert_text(["bl14", "grin"], [], working=["ea"])
        self.assertIn("bl14, grin", text)
        self.assertIn(signin.RESIGN, text)
        self.assertNotIn("paused", text)
        self.assertIn("paused", signin.alert_text(["ea"], [], working=[]))
        self.assertEqual(signin.alert_text([], [], working=["ea"]), "")
        self.assertIn("working again: ea", signin.alert_text([], ["ea"], working=["ea"]))


if __name__ == "__main__":
    unittest.main()
