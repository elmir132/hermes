"""Run once per account to authorize Google Calendar + Gmail access."""
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

account = input("Account name (e.g. personal / bloody14 / elmirabdullaiev): ").strip()
if not account:
    print("Aborted.")
    sys.exit(1)

token_path = Path(f"token_{account}.json")
print(f"Saving to {token_path}...")

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Run: /tmp/gcal-auth/bin/pip install google-auth google-auth-oauthlib")
    sys.exit(1)

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
print("Opening browser — sign in with the correct Google account...")
creds = flow.run_local_server(port=0, open_browser=True)
token_path.write_text(creds.to_json())
print(f"✅ Saved {token_path}")
print(f"Upload with: scp {token_path} <server>:/home/hermes/app/")
