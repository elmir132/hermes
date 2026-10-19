"""Google sign-in health: tell an expired sign-in apart from a passing network problem, save refreshed
tokens safely, and say once (not every day) when an account needs signing in again.

No Google import at module level, so the tests run without the Google libraries installed.
"""
import json
import os
import tempfile
from pathlib import Path

EXPIRED, TRANSIENT, MISSING, BROKEN = "expired", "transient", "missing", "broken"
RESIGN = "cd ~/Desktop/ai/hermes && ~/.venvs/hermes-auth/bin/python auth_calendar.py"


def classify(exc) -> str:
    """'expired' needs a new sign-in (Google said invalid_grant or the token was revoked); 'transient' will pass by itself."""
    name, text = type(exc).__name__, str(exc).lower()
    if name == "RefreshError" and any(k in text for k in ("invalid_grant", "revoked", "expired", "unauthorized_client", "invalid_scope")):
        return EXPIRED
    if name in ("TransportError", "ConnectionError", "Timeout", "TimeoutError", "ReadTimeout", "ConnectTimeout") or isinstance(exc, OSError):
        return TRANSIENT
    if name == "RefreshError":                       # a refresh error with an unusual message: treat as needing a new sign-in
        return EXPIRED
    return BROKEN


def save_atomic(path: Path, text: str) -> None:
    """Write next to the file, then rename over it, so a crash never leaves half a token. Keeps it private (mode 600)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(token_path: Path, scopes, credentials_cls=None, request_cls=None):
    """Returns (creds or None, problem or None). Google classes can be passed in for tests."""
    token_path = Path(token_path)
    if not token_path.exists():
        return None, MISSING
    try:
        if credentials_cls is None or request_cls is None:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            credentials_cls, request_cls = credentials_cls or Credentials, request_cls or Request
        creds = credentials_cls.from_authorized_user_file(str(token_path), scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(request_cls())
            try:
                save_atomic(token_path, creds.to_json())
            except OSError:
                pass                                  # the refreshed token still works for this run
        if creds.valid:
            return creds, None
        return None, EXPIRED if not creds.refresh_token else BROKEN
    except Exception as exc:                          # noqa: BLE001 - every failure is classified, never raised
        return None, classify(exc)


class AlertState:
    """Remembers which accounts were already reported broken, so each change is announced once."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def changes(self, health: dict):
        """health = {account: None (works) or a problem}. Transient problems are not news.
        Returns (newly_broken, recovered) and saves the new state."""
        before = self._read()
        now = {name: problem for name, problem in health.items() if problem in (EXPIRED, BROKEN)}
        newly = sorted(name for name in now if name not in before)
        recovered = sorted(name for name in before if name in health and health[name] is None)
        keep = {name: now[name] for name in now}
        keep.update({name: before[name] for name in before if name not in health})      # a removed token file is not "fixed"
        if keep != before:
            try:
                save_atomic(self.path, json.dumps(keep, sort_keys=True))
            except OSError:
                pass
        return newly, recovered


def alert_text(newly_broken, recovered, working) -> str:
    """The Telegram message, or '' when nothing changed."""
    if not newly_broken and not recovered:
        return ""
    lines = []
    if newly_broken:
        lines.append("🔑 Google sign-in needs renewing: " + ", ".join(newly_broken) + ".")
        lines.append("The morning digest and job-alert emails cannot read " + ("these inboxes" if len(newly_broken) > 1 else "this inbox")
                     + " until you sign in again. On the Mac run:")
        lines.append(RESIGN)
        lines.append("then type the account name it asks for, and copy the new token file to the server.")
        if not working:
            lines.append("No Google account works right now, so the digest is paused.")
    if recovered:
        lines.append("✅ Google sign-in working again: " + ", ".join(recovered) + ".")
    return "\n".join(lines)
