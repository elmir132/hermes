import asyncio
import importlib.util
import io
import os
import re
import sqlite3
import struct
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import google.generativeai as genai
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Document, BufferedInputFile
from bs4 import BeautifulSoup
from pypdf import PdfReader

# ── Markdown → Telegram HTML ──────────────────────────────────────────────────
def md_to_html(text: str) -> str:
    """Convert Gemini markdown to Telegram-safe HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # fenced code blocks
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", r"<pre>\1</pre>", text, flags=re.S)
    # headers
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # ***bold italic*** — must come before ** and *
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    # **bold** — no re.S so it never spans lines (avoids unclosed tag rejections)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # *italic* — single asterisk, no spanning lines
    text = re.sub(r"\*([^\s*\n][^*\n]*[^\s*\n])\*", r"<i>\1</i>", text)
    # `inline code`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # checkboxes
    text = text.replace("[ ]", "☐").replace("[x]", "✅").replace("[X]", "✅")
    # bullet lists
    text = re.sub(r"^[*\-]\s+", "• ", text, flags=re.MULTILINE)
    # horizontal rules
    text = re.sub(r"^---+$", "─" * 20, text, flags=re.MULTILINE)
    return text.strip()

async def safe_reply(msg: Message, text: str) -> None:
    """Send with HTML formatting; fall back to plain text on parse error."""
    try:
        await msg.answer(md_to_html(text), parse_mode=ParseMode.HTML)
    except Exception:
        await msg.answer(text)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ALLOWED_USER   = int(os.environ["ALLOWED_USER_ID"])
HONCHO_API_KEY      = os.environ.get("HONCHO_API_KEY", "")
TODOIST_TOKEN       = os.environ.get("TODOIST_TOKEN", "")
TAVILY_API_KEY      = os.environ.get("TAVILY_API_KEY", "")
TMDB_TOKEN          = os.environ.get("TMDB_TOKEN", "")
PANDASCORE_TOKEN    = os.environ.get("PANDASCORE_TOKEN", "")
MAX_RECENT      = 20
MAX_SEMANTIC   = 5
DB_PATH        = Path(__file__).parent / "hermes.db"
SKILLS_DIR     = Path(__file__).parent / "skills"
SKILLS_DIR.mkdir(exist_ok=True)

# ── Gemini ────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)

TEXT_MODEL_FALLBACKS = [
    "gemini-3.5-flash",       # best quality      — 20 RPD
    "gemini-3-flash",         # 2nd best          — 20 RPD
    "gemini-2.5-flash",       # 3rd best          — 20 RPD
    "gemini-3.1-flash-lite",  # high-volume       — 500 RPD
    "gemini-2.5-flash-lite",  # high-volume       — 20 RPD
    "gemini-2.0-flash",       # final safety net  — legacy, always-on
]

IMAGE_MODEL_FALLBACKS = [
    "imagen-4.0-ultra-generate-001",   # Imagen 4 Ultra — best quality (25/day)
    "imagen-4.0-generate-001",         # Imagen 4 Standard (25/day)
    "imagen-4.0-fast-generate-001",    # Imagen 4 Fast (25/day)
]

MODEL_SMART = TEXT_MODEL_FALLBACKS[0]   # gemini-3.5-flash — best, tried first
MODEL_LITE  = TEXT_MODEL_FALLBACKS[3]   # gemini-3.1-flash-lite — high-volume fallback

# ── Quota-aware model routing ───────────────────────────────────────────────────
# chat() and one_shot() always try the BEST model first and fall through the chain
# on quota/429, ending at gemini-2.0-flash. To avoid hammering a model that just hit
# its limit, we remember when each model became exhausted and skip it until cooldown
# elapses — respecting the API's own retry_delay (per-minute limits), backing off
# ~1h for per-day exhaustion.
_MODEL_COOLDOWN: dict[str, float] = {}   # model_name -> unix ts until skippable

def _cooldown_secs(err: str) -> float:
    m = re.search(r"retry_delay\s*\{\s*seconds:\s*(\d+)", err)
    if m:
        return float(m.group(1)) + 2
    if "perday" in err.lower().replace(" ", "") or "per day" in err.lower():
        return 3600.0          # daily quota — back off an hour
    return 60.0                # default — ride out a per-minute window

def _is_quota_error(err: str) -> bool:
    e = err.lower()
    return ("429" in err or "quota" in e or "exhausted" in e
            or "resource_exhausted" in e or "rate limit" in e)

# Models that returned "not found / not supported / 404" (renamed, removed, or not
# on this API tier) are disabled for this process so we never hard-fail on them again.
_DEAD_MODELS: set[str] = set()

def _is_model_unavailable(err: str) -> bool:
    e = err.lower()
    return ("404" in e or "not found" in e or "not supported" in e
            or "is not found for api version" in e)

def _mark_exhausted(model_name: str, err: str = ""):
    _MODEL_COOLDOWN[model_name] = time.time() + _cooldown_secs(err)

def _models_to_try(mode: str = "auto") -> list[str]:
    """Ordered model list by mode, skipping DEAD models and quota-cooldown models.
      'smart' → full premium chain, ignore cooldowns (force best models, e.g. '!!')
      'easy'  → high-volume tier only (3.1-flash-lite onward)
      'auto'  → best-first, skipping cooldowns (default)
    If everything is filtered out, fall back to the whole live pool."""
    if mode == "easy":
        pool = TEXT_MODEL_FALLBACKS[TEXT_MODEL_FALLBACKS.index(MODEL_LITE):]
    else:
        pool = list(TEXT_MODEL_FALLBACKS)
    pool = [m for m in pool if m not in _DEAD_MODELS]      # drop unavailable models
    if not pool:                                           # everything dead -> retry all
        pool = list(TEXT_MODEL_FALLBACKS)
    if mode == "smart":
        return pool                                        # ignore cooldowns
    now = time.time()
    ready = [m for m in pool if _MODEL_COOLDOWN.get(m, 0) <= now]
    return ready or pool

# Casual one-liners that don't deserve a 20-RPD premium call — routed to 'easy'.
_TRIVIAL = {
    "hi", "hey", "hello", "yo", "wassup", "sup", "wsg", "ok", "okay", "kk", "k",
    "thanks", "thank you", "ty", "thx", "lol", "lmao", "cool", "nice", "great",
    "gm", "gn", "good morning", "good night", "hru", "how are you",
    "what's up", "whats up", "hey hermes", "hi hermes", "yes", "no", "np",
}

def _is_trivial(text: str) -> bool:
    t = text.strip().lower().rstrip("!?. ")
    if t in _TRIVIAL:
        return True
    return len(t.split()) <= 2 and "?" not in text

def _extract_text(response) -> str:
    """Concatenate text parts from a Gemini response (ignores function-call parts)."""
    out = ""
    try:
        for p in response.candidates[0].content.parts:
            try:
                out += p.text
            except Exception:
                pass
    except Exception:
        pass
    return out.strip()

BASE_SYSTEM = (
    "You are Hermes, a sharp and concise personal AI assistant for Elmir. "
    "Give direct, useful answers. Avoid filler phrases. "
    "TOOLS — use these proactively, never answer from memory:\n"
    "- web_search: ALWAYS use for current info — news, weather, visa rules, prices, anything time-sensitive.\n"
    "- get_esports_matches: ALWAYS use when user asks if a specific team is playing, esports schedules, or upcoming/live CS2 or Dota 2 matches (any team — NaVi, B8, Spirit, etc.). Never answer esports schedules from memory.\n"
    "- get_ukrainian_matches: use when user asks if ANY Ukrainian team is playing or for a Ukrainian esports schedule (checks all UA teams at once).\n"
    "- get_todoist_tasks: user asks what to do, their list, priorities.\n"
    "- add_todoist_task: user wants to add/remember a task.\n"
    "- get_calendar_events: user asks about schedule, meetings, plans.\n"
    "- create_calendar_event: user wants to schedule something.\n"
    "- get_unread_emails: user asks about emails, inbox, messages.\n"
    "- search_emails: user asks to find emails from someone or about a topic.\n"
    "- tmdb_search: ALWAYS use when user asks about a specific movie — never answer from memory.\n"
    "- tmdb_recommend: ALWAYS use when user asks for movies like X, similar to X, or recommendations.\n"
    "- tmdb_discover: ALWAYS use when user asks what to watch, good movies in a genre, top films.\n"
    "ALWAYS call the relevant tool first, then answer based on the real data."
)

_base_model = genai.GenerativeModel(
    model_name=MODEL_LITE,
    system_instruction=BASE_SYSTEM,
)

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                role      TEXT    NOT NULL,
                content   TEXT    NOT NULL,
                embedding BLOB,
                ts        TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS skills (
                name        TEXT PRIMARY KEY,
                description TEXT,
                code        TEXT NOT NULL,
                ts          TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS navi_notified (
                match_id INTEGER PRIMARY KEY,
                ts       TEXT DEFAULT (datetime('now'))
            );
        """)

# ── Embeddings ────────────────────────────────────────────────────────────────
def _embed(text: str):
    try:
        r = genai.embed_content(
            model="models/text-embedding-004",
            content=text,
            task_type="RETRIEVAL_DOCUMENT",
        )
        return r["embedding"]
    except Exception:
        return None

def _pack(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)

def _unpack(blob: bytes):
    n = len(blob) // 4
    return struct.unpack(f"{n}f", blob)

def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((sum(x*x for x in a)**0.5) * (sum(x*x for x in b)**0.5) + 1e-9)

# ── Memory ────────────────────────────────────────────────────────────────────
def store_message(role: str, content: str):
    blob = None
    emb = _embed(content)
    if emb:
        blob = _pack(emb)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO messages (role, content, embedding) VALUES (?,?,?)",
            (role, content, blob),
        )

def get_recent(n: int = MAX_RECENT) -> list[dict]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return [{"role": r, "parts": [c]} for r, c in reversed(rows)]

def get_recent_ids(n: int = MAX_RECENT) -> set:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT id FROM messages ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return {r[0] for r in rows}

def get_semantic(query: str, exclude_ids: set, k: int = MAX_SEMANTIC) -> str:
    qvec = _embed(query)
    if not qvec:
        return ""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT id, role, content, embedding FROM messages WHERE embedding IS NOT NULL"
        ).fetchall()
    scored = [
        (_cosine(qvec, _unpack(blob)), role, content)
        for row_id, role, content, blob in rows
        if row_id not in exclude_ids
    ]
    scored.sort(reverse=True)
    top = scored[:k]
    if not top:
        return ""
    return "Relevant past context:\n" + "\n".join(f"[{r}]: {c}" for _, r, c in top)

def clear_memory():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM messages")

# ── Honcho ────────────────────────────────────────────────────────────────────
_honcho = None  # (user_peer, ai_peer, session) or None

def init_honcho():
    global _honcho
    if not HONCHO_API_KEY:
        return
    try:
        from honcho import Honcho
        c         = Honcho(api_key=HONCHO_API_KEY, workspace_id="hermes")
        user_peer = c.peer("user")
        ai_peer   = c.peer("assistant")
        session   = c.session("main")
        session.add_peers([user_peer, ai_peer])
        _honcho = (user_peer, ai_peer, session)
        print("Honcho active.")
    except Exception as e:
        print(f"Honcho unavailable: {e}")

def honcho_store(is_user: bool, content: str):
    if not _honcho:
        return
    user_peer, ai_peer, session = _honcho
    try:
        peer = user_peer if is_user else ai_peer
        session.add_messages([peer.message(content)])
    except Exception:
        pass

def honcho_context() -> str:
    return ""  # disabled until enough history is built up

# ── Todoist ───────────────────────────────────────────────────────────────────
_TD_BASE = "https://api.todoist.com/api/v1"
_TD_HDR  = lambda: {"Authorization": f"Bearer {TODOIST_TOKEN}"}
_P_LABEL = {4: "🔴 Urgent", 3: "🟠 High", 2: "🔵 Medium", 1: "⚪ Normal"}

async def todoist_get_tasks() -> str:
    if not TODOIST_TOKEN:
        return "Todoist not configured (add TODOIST_TOKEN to .env)."
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_TD_BASE}/tasks", headers=_TD_HDR())
        if not r.content:
            return f"Todoist empty response (HTTP {r.status_code}) — token may be invalid."
        if r.status_code != 200:
            return f"Todoist error HTTP {r.status_code}: {r.text[:200]}"
        data  = r.json()
        tasks = data.get("results", data) if isinstance(data, dict) else data
        if not tasks:
            return "No active tasks in Todoist."
        lines = []
        for t in sorted(tasks, key=lambda x: -x.get("priority", 1)):
            due = (t.get("due") or {}).get("string", "no due date")
            pri = _P_LABEL.get(t.get("priority", 1), "Normal")
            lines.append(f"[{pri}] {t['content']} — {due}")
        return f"Todoist tasks ({len(tasks)} active):\n" + "\n".join(lines)
    except Exception as e:
        return f"Todoist error: {e}"

async def todoist_add_task(content: str, due_string: str = "", priority: int = 1) -> str:
    if not TODOIST_TOKEN:
        return "Todoist not configured."
    try:
        payload: dict = {"content": content, "priority": max(1, min(4, priority))}
        if due_string:
            payload["due_string"] = due_string
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{_TD_BASE}/tasks", json=payload, headers=_TD_HDR())
        if r.status_code in (200, 204):
            name = (r.json().get("content", content) if r.content else content)
            return f"✅ Added: {name}"
        return f"Error {r.status_code}: {r.text}"
    except Exception as e:
        return f"Todoist error: {e}"

# ── Google account display names ────────────────────────────���────────────────
_ACCOUNT_LABEL = {
    "personal":        "👤 Personal",
    "elmirabdullaiev": "🎓 Education",
    "twink":           "🎮 Twink",
    "kubg":            "🏫 KUBG",
}

def _acct_label(name: str) -> str:
    return _ACCOUNT_LABEL.get(name, f"📧 {name.capitalize()}")

def _fmt_sender(raw: str) -> str:
    """'John Doe <john@example.com>' → 'John Doe'"""
    return raw.split("<")[0].strip().strip('"') or raw

def _fmt_date(raw: str) -> str:
    """Parse email Date header → 'May 22, 20:35'"""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%b %d, %H:%M")
    except Exception:
        return raw[:16]

def _fmt_email_list(msgs: list[tuple[str, str, str, str]], title: str) -> str:
    """Format grouped email list. msgs = [(account, subject, sender, date)]"""
    if not msgs:
        return f"{title}\n\nInbox clear ✓"
    grouped: dict[str, list] = {}
    for acct, subj, sender, date in msgs:
        grouped.setdefault(acct, []).append((subj, sender, date))
    lines = [f"{title} — {len(msgs)} email{'s' if len(msgs) != 1 else ''}"]
    for acct, items in grouped.items():
        lines.append(f"\n{_acct_label(acct)} · {len(items)}")
        for subj, sender, date in items:
            subj_short = subj[:55] + "…" if len(subj) > 55 else subj
            lines.append(f"  <b>{subj_short}</b>")
            lines.append(f"  {_fmt_sender(sender)} · {_fmt_date(date)}\n")
    return "\n".join(lines).strip()

# ── Google (Calendar + Gmail) — multi-account ─────────────────────────────────
_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
_HERE      = Path(__file__).parent
_GCAL_API  = "https://www.googleapis.com/calendar/v3"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"

def _load_creds(token_path: Path):
    if not token_path.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(token_path), _GOOGLE_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            try:
                token_path.write_text(creds.to_json())   # cache refresh — non-fatal if disk is read-only
            except Exception:
                pass
        return creds if creds.valid else None
    except Exception:
        return None

def _all_token_files() -> list[Path]:
    """Return all token_*.json files in bot directory."""
    tokens = sorted(_HERE.glob("token_*.json"))
    if not tokens:
        legacy = _HERE / "token.json"
        if legacy.exists():
            tokens = [legacy]
    return tokens

def _all_creds() -> list[tuple[str, object]]:
    """Return list of (account_name, creds) for all token files."""
    result = []
    for p in _all_token_files():
        name = p.stem.replace("token_", "") or p.stem
        c = _load_creds(p)
        if c:
            result.append((name, c))
    return result

def _creds_diag() -> list[str]:
    """Per-account credential health — surfaces the REAL load error, scope state,
    and a live Gmail API ping so we can tell exactly what's failing."""
    out = []
    pinged = False
    for p in _all_token_files():
        name = p.stem.replace("token_", "") or p.stem
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            creds = Credentials.from_authorized_user_file(str(p), _GOOGLE_SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            scopes = creds.scopes or []
            gmail  = "gmail✓" if any("gmail" in s for s in scopes) else "gmail✗"
            line = f"✅ {name} ({gmail})" if creds.valid else f"❌ {name}: invalid"
            if creds.valid and not pinged:        # ping Gmail API once to confirm it works
                pinged = True
                try:
                    import httpx as _hx
                    rr = _hx.get(f"{_GMAIL_API}/users/me/profile",
                                 headers={"Authorization": f"Bearer {creds.token}"}, timeout=10)
                    line += f"  [Gmail API HTTP {rr.status_code}{'' if rr.status_code==200 else ': '+rr.text[:60]}]"
                except Exception as ee:
                    line += f"  [Gmail ping err: {str(ee)[:40]}]"
            out.append(line)
        except Exception as e:
            out.append(f"❌ {name}: {str(e)[:70]}")
    return out or ["❌ no token files"]

async def calendar_get_events(days: int = 7) -> str:
    accounts = await asyncio.to_thread(_all_creds)
    if not accounts:
        return "Google Calendar not set up — no token files found."
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    all_lines = []
    async with httpx.AsyncClient(timeout=10) as client:
        for name, creds in accounts:
            try:
                r = await client.get(
                    f"{_GCAL_API}/calendars/primary/events",
                    headers={"Authorization": f"Bearer {creds.token}"},
                    params={
                        "timeMin": now.isoformat(),
                        "timeMax": end.isoformat(),
                        "orderBy": "startTime",
                        "singleEvents": "true",
                        "maxResults": "15",
                    },
                )
                if r.status_code != 200:
                    continue
                for e in r.json().get("items", []):
                    start = e.get("start", {})
                    dt = start.get("dateTime", start.get("date", "?"))
                    if "T" in dt:
                        dt = datetime.fromisoformat(dt).strftime("%a %b %d, %H:%M")
                    all_lines.append(f"• [{name}] {dt} — {e.get('summary', '(no title)')}")
            except Exception:
                continue
    if not all_lines:
        return f"No events in the next {days} days."
    return f"Upcoming {days}d ({len(all_lines)} events):\n" + "\n".join(all_lines)

async def calendar_add_event(title: str, start_iso: str, duration_minutes: int = 60, description: str = "") -> str:
    accounts = await asyncio.to_thread(_all_creds)
    if not accounts:
        return "Google Calendar not set up."
    _, creds = accounts[0]  # create in primary account
    try:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone(timedelta(hours=4)))
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        body = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat()},
            "end":   {"dateTime": end_dt.isoformat()},
        }
        if description:
            body["description"] = description
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{_GCAL_API}/calendars/primary/events",
                headers={"Authorization": f"Bearer {creds.token}"},
                json=body,
            )
        if r.status_code in (200, 201):
            ev = r.json()
            return f"✅ Created: {ev.get('summary')} on {ev['start']['dateTime']}"
        return f"Calendar error {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"Calendar error: {e}"

async def gmail_get_unread(max_results: int = 10) -> str:
    accounts = await asyncio.to_thread(_all_creds)
    if not accounts:
        return "Gmail not set up — no token files found."
    all_msgs = []
    errors = []
    async with httpx.AsyncClient(timeout=15) as client:
        for name, creds in accounts:
            hdrs = {"Authorization": f"Bearer {creds.token}"}
            try:
                r = await client.get(
                    f"{_GMAIL_API}/users/me/messages",
                    headers=hdrs,
                    params={"q": "is:unread", "maxResults": max_results},
                )
                if r.status_code != 200:
                    errors.append(f"{name}: HTTP {r.status_code} — {r.text[:100]}")
                    continue
                msg_ids = [m["id"] for m in r.json().get("messages", [])]
                for mid in msg_ids[:max_results]:
                    mr = await client.get(
                        f"{_GMAIL_API}/users/me/messages/{mid}",
                        headers=hdrs,
                        params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                    )
                    if mr.status_code != 200:
                        continue
                    h = {x["name"]: x["value"] for x in mr.json().get("payload", {}).get("headers", [])}
                    all_msgs.append((name, h.get("Subject","(no subject)"), h.get("From","?"), h.get("Date","")))
            except Exception as e:
                errors.append(f"{name}: {e}")
    result = _fmt_email_list(all_msgs, "📬 Unread")
    if errors:
        result += "\n\n⚠️ " + "; ".join(errors)
    return result

async def gmail_search(query: str, max_results: int = 10) -> str:
    accounts = await asyncio.to_thread(_all_creds)
    if not accounts:
        return "Gmail not set up — no token files found."
    all_msgs = []
    errors = []
    async with httpx.AsyncClient(timeout=15) as client:
        for name, creds in accounts:
            hdrs = {"Authorization": f"Bearer {creds.token}"}
            try:
                r = await client.get(
                    f"{_GMAIL_API}/users/me/messages",
                    headers=hdrs,
                    params={"q": query, "maxResults": max_results},
                )
                if r.status_code != 200:
                    errors.append(f"{name}: HTTP {r.status_code} — {r.text[:100]}")
                    continue
                for m in r.json().get("messages", [])[:max_results]:
                    mr = await client.get(
                        f"{_GMAIL_API}/users/me/messages/{m['id']}",
                        headers=hdrs,
                        params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                    )
                    if mr.status_code != 200:
                        continue
                    h = {x["name"]: x["value"] for x in mr.json().get("payload", {}).get("headers", [])}
                    all_msgs.append((name, h.get("Subject","(no subject)"), h.get("From","?"), h.get("Date","")))
            except Exception as e:
                errors.append(f"{name}: {e}")
    result = _fmt_email_list(all_msgs, f"🔍 '{query}'")
    if errors:
        result += "\n\n⚠️ " + "; ".join(errors)
    return result

# ── TMDB Movies ───────────────────────────────────────────────────────────────
_TMDB_API = "https://api.themoviedb.org/3"
_TMDB_HDR = lambda: {"Authorization": f"Bearer {TMDB_TOKEN}"}

async def tmdb_search(query: str) -> str:
    if not TMDB_TOKEN:
        return "TMDB not configured (add TMDB_TOKEN to .env)."
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_TMDB_API}/search/movie",
                headers=_TMDB_HDR(),
                params={"query": query, "language": "en-US"},
            )
        results = r.json().get("results", [])
        if not results:
            return f"No movies found for: {query}"
        lines = [f"🎬 {query}\n"]
        for m in results[:5]:
            year    = m.get("release_date", "")[:4]
            rating  = m.get("vote_average", 0)
            overview = m.get("overview", "")[:120]
            lines.append(f"• {m['title']} ({year}) ⭐ {rating:.1f}/10\n  {overview}")
        return "\n".join(lines)
    except Exception as e:
        return f"TMDB error: {e}"

async def tmdb_recommend(movie_title: str) -> str:
    if not TMDB_TOKEN:
        return "TMDB not configured."
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_TMDB_API}/search/movie",
                headers=_TMDB_HDR(),
                params={"query": movie_title, "language": "en-US"},
            )
            results = r.json().get("results", [])
            if not results:
                return f"Movie not found: {movie_title}"
            movie_id   = results[0]["id"]
            base_title = results[0]["title"]
            r2 = await client.get(
                f"{_TMDB_API}/movie/{movie_id}/recommendations",
                headers=_TMDB_HDR(),
                params={"language": "en-US"},
            )
        recs = r2.json().get("results", [])
        if not recs:
            return f"No recommendations found for {base_title}."
        lines = [f"🎬 If you liked {base_title}:\n"]
        for m in recs[:7]:
            year   = m.get("release_date", "")[:4]
            rating = m.get("vote_average", 0)
            lines.append(f"• {m['title']} ({year}) ⭐ {rating:.1f}/10")
        return "\n".join(lines)
    except Exception as e:
        return f"TMDB error: {e}"

async def tmdb_discover(genre: str, sort_by: str = "popularity") -> str:
    if not TMDB_TOKEN:
        return "TMDB not configured."
    _GENRES = {
        "action": 28, "adventure": 12, "animation": 16, "comedy": 35,
        "crime": 80, "documentary": 99, "drama": 18, "horror": 27,
        "romance": 10749, "sci-fi": 878, "thriller": 53, "mystery": 9648,
    }
    genre_id = _GENRES.get(genre.lower())
    sort_map = {"popularity": "popularity.desc", "rating": "vote_average.desc", "new": "release_date.desc"}
    sort_param = sort_map.get(sort_by, "popularity.desc")
    try:
        params = {"language": "en-US", "sort_by": sort_param, "vote_count.gte": 100}
        if genre_id:
            params["with_genres"] = genre_id
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_TMDB_API}/discover/movie", headers=_TMDB_HDR(), params=params)
        results = r.json().get("results", [])
        if not results:
            return f"No movies found for genre: {genre}"
        lines = [f"🎬 Top {genre} movies:\n"]
        for m in results[:7]:
            year   = m.get("release_date", "")[:4]
            rating = m.get("vote_average", 0)
            lines.append(f"• {m['title']} ({year}) ⭐ {rating:.1f}/10")
        return "\n".join(lines)
    except Exception as e:
        return f"TMDB error: {e}"

# ── NaVi Match Tracker ────────────────────────────────────────────────────────
_PS_BASE         = "https://api.pandascore.co"
_PS_HDR          = lambda: {"Authorization": f"Bearer {PANDASCORE_TOKEN}"}
_NAVI_TEAM_IDS: dict[str, int] = {}

async def _ps_find_navi_id(game: str) -> int | None:
    if game in _NAVI_TEAM_IDS:
        return _NAVI_TEAM_IDS[game]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_PS_BASE}/{game}/teams",
                headers=_PS_HDR(),
                params={"search[name]": "natus vincere", "per_page": 5},
            )
        for team in (r.json() if r.status_code == 200 else []):
            slug = team.get("slug", "").lower()
            name = team.get("name", "").lower()
            if "natus" in name or "navi" in slug:
                _NAVI_TEAM_IDS[game] = team["id"]
                return team["id"]
    except Exception:
        pass
    return None

async def navi_get_upcoming(game: str) -> list[dict]:
    if not PANDASCORE_TOKEN:
        return []
    team_id = await _ps_find_navi_id(game)
    if not team_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_PS_BASE}/{game}/matches/upcoming",
                headers=_PS_HDR(),
                params={"filter[opponent_id]": team_id, "sort": "begin_at", "per_page": 5},
            )
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []

def _ps_notified_ids() -> set:
    with sqlite3.connect(DB_PATH) as con:
        return {r[0] for r in con.execute("SELECT match_id FROM navi_notified").fetchall()}

def _ps_mark_notified(match_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO navi_notified (match_id) VALUES (?)", (match_id,))

def _fmt_navi_match(match: dict, game: str) -> str:
    game_label = "CS2" if game == "csgo" else "Dota 2"
    teams = [o.get("opponent", {}).get("name", "?") for o in match.get("opponents", [])]
    vs = " vs ".join(teams) if teams else "TBD"
    begin_at = match.get("begin_at") or ""
    if begin_at:
        try:
            dt = datetime.fromisoformat(begin_at.replace("Z", "+00:00"))
            time_str = dt.astimezone(timezone(timedelta(hours=-4))).strftime("%a %b %d, %H:%M NYC")
        except Exception:
            time_str = begin_at
    else:
        time_str = "TBD"
    tournament = (match.get("tournament") or {}).get("name") or (match.get("league") or {}).get("name") or "Unknown tournament"
    return f"🎮 [{game_label}] {vs}\n📅 {time_str}\n🏆 {tournament}"

async def navi_check_and_notify(user_id: int):
    if not PANDASCORE_TOKEN:
        return
    notified = _ps_notified_ids()
    for game in ["csgo", "dota2"]:
        for match in await navi_get_upcoming(game):
            mid = match.get("id")
            if mid and mid not in notified:
                text = "🔔 NaVi match scheduled!\n\n" + _fmt_navi_match(match, game)
                try:
                    await bot.send_message(user_id, text)
                    _ps_mark_notified(mid)
                except Exception:
                    pass

async def _navi_poll_loop():
    await asyncio.sleep(15)  # let bot finish startup
    while True:
        try:
            await navi_check_and_notify(ALLOWED_USER)
        except Exception:
            pass
        await asyncio.sleep(4 * 60 * 60)  # poll every 4 hours

async def navi_upcoming_summary() -> str:
    if not PANDASCORE_TOKEN:
        return "PandaScore not configured (add PANDASCORE_TOKEN to .env)."
    lines = []
    for game in ["csgo", "dota2"]:
        matches = await navi_get_upcoming(game)
        for m in matches:
            lines.append(_fmt_navi_match(m, game))
    if not lines:
        return "No upcoming NaVi matches found."
    return "🏹 Upcoming NaVi matches:\n\n" + "\n\n".join(lines)

# ── Generalized esports lookup (ANY team, via PandaScore) ───────────────────────
_PS_TEAM_IDS: dict[tuple, list[int]] = {}

def _ps_norm_game(game: str) -> str:
    g = (game or "").lower()
    if "dota" in g:                      return "dota2"
    if "cs" in g or "counter" in g:      return "csgo"   # CS2 lives under the csgo slug
    return ""                            # unknown -> caller checks both

async def _ps_find_team_ids(name: str, game: str) -> list[int]:
    """Resolve a team name to PandaScore team IDs.

    PandaScore's search[name] is fuzzy: e.g. 'Monte' returns Montenegro/Monte Gen
    first, 'B8' returns 'B8 Academy' first. Taking teams[0] therefore picks the
    WRONG team. We instead match on exact name / slug / acronym so the real team
    is found, and return every strong match (handles duplicate entries)."""
    key = (game, name.lower())
    if key in _PS_TEAM_IDS:
        return _PS_TEAM_IDS[key]
    ids: list[int] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_PS_BASE}/{game}/teams",
                headers=_PS_HDR(),
                params={"search[name]": name, "per_page": 20},
            )
        teams = r.json() if r.status_code == 200 else []
        q      = name.lower()
        q_slug = q.replace(" ", "-")
        for t in teams:                              # exact name / slug / acronym
            nm   = (t.get("name") or "").lower()
            slug = (t.get("slug") or "").lower()
            acr  = (t.get("acronym") or "").lower()
            if (q in (nm, slug, acr) or slug == q_slug
                    or slug.startswith(q_slug + "-cs")      # natus-vincere-cs-go
                    or slug.startswith(q_slug + "-dota")):  # not 'monte-gen'/'b8-academy'
                if t.get("id") and t["id"] not in ids:
                    ids.append(t["id"])
        if not ids and teams and teams[0].get("id"):  # fallback: don't lose a team
            ids.append(teams[0]["id"])
    except Exception:
        pass
    if ids:
        _PS_TEAM_IDS[key] = ids
    return ids

async def _ps_find_team_id(name: str, game: str) -> int | None:
    ids = await _ps_find_team_ids(name, game)
    return ids[0] if ids else None

async def ps_team_matches(team: str, game: str = "") -> str:
    """Live + upcoming matches for ANY esports team (CS2 / Dota 2)."""
    if not PANDASCORE_TOKEN:
        return "PandaScore not configured (add PANDASCORE_TOKEN to .env)."
    g     = _ps_norm_game(game)
    games = [g] if g else ["csgo", "dota2"]
    out   = []
    for gm in games:
        tid = await _ps_find_team_id(team, gm)
        if not tid:
            continue
        for status in ("running", "upcoming"):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        f"{_PS_BASE}/{gm}/matches/{status}",
                        headers=_PS_HDR(),
                        params={"filter[opponent_id]": tid, "sort": "begin_at", "per_page": 5},
                    )
                for m in (r.json() if r.status_code == 200 else []):
                    prefix = "🔴 LIVE  " if status == "running" else ""
                    out.append(prefix + _fmt_navi_match(m, gm))
            except Exception:
                pass
    if not out:
        return f"No live or upcoming matches found for '{team}' (checked CS2 and Dota 2)."
    return f"Matches for {team}:\n\n" + "\n\n".join(out)

# Curated Ukrainian esports teams (CS2 + Dota 2)
_UA_TEAMS = [
    "Natus Vincere", "B8", "Monte", "Passion UA", "Natus Vincere Junior",
    "Team Hryvnia", "Dragon Esports", "Team Lynx",
]

async def ps_ukrainian_matches() -> str:
    """Live + upcoming matches for ALL curated Ukrainian teams (CS2 + Dota 2)."""
    if not PANDASCORE_TOKEN:
        return "PandaScore not configured (add PANDASCORE_TOKEN to .env)."
    out, seen = [], set()
    for gm in ("csgo", "dota2"):
        ids = {}
        for t in _UA_TEAMS:
            for tid in await _ps_find_team_ids(t, gm):
                ids[tid] = t
        if not ids:
            continue
        id_csv = ",".join(str(i) for i in ids)
        for status in ("running", "upcoming"):
            try:
                async with httpx.AsyncClient(timeout=12) as client:
                    r = await client.get(
                        f"{_PS_BASE}/{gm}/matches/{status}",
                        headers=_PS_HDR(),
                        params={"filter[opponent_id]": id_csv, "sort": "begin_at", "per_page": 50},
                    )
                for m in (r.json() if r.status_code == 200 else []):
                    mid = m.get("id")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    prefix = "🔴 LIVE  " if status == "running" else ""
                    out.append(prefix + _fmt_navi_match(m, gm))
            except Exception:
                pass
    if not out:
        return "No live or upcoming matches for Ukrainian teams right now (CS2 + Dota 2)."
    return "🇺🇦 Ukrainian teams - live & upcoming:\n\n" + "\n\n".join(out)

async def _dispatch(name: str, args: dict) -> str:
    if name == "get_todoist_tasks":
        return await todoist_get_tasks()
    if name == "add_todoist_task":
        return await todoist_add_task(
            content    = args.get("content", ""),
            due_string = args.get("due_string", ""),
            priority   = int(args.get("priority", 1)),
        )
    if name == "get_calendar_events":
        return await calendar_get_events(days=int(args.get("days", 7)))
    if name == "create_calendar_event":
        return await calendar_add_event(
            title            = args.get("title", ""),
            start_iso        = args.get("start_iso", ""),
            duration_minutes = int(args.get("duration_minutes", 60)),
            description      = args.get("description", ""),
        )
    if name == "get_unread_emails":
        return await gmail_get_unread(max_results=int(args.get("max_results", 10)))
    if name == "search_emails":
        return await gmail_search(
            query       = args.get("query", ""),
            max_results = int(args.get("max_results", 10)),
        )
    if name == "web_search":
        return await web_search(query=args.get("query", ""))
    if name == "tmdb_search":
        return await tmdb_search(query=args.get("query", ""))
    if name == "tmdb_recommend":
        return await tmdb_recommend(movie_title=args.get("movie_title", ""))
    if name == "tmdb_discover":
        return await tmdb_discover(genre=args.get("genre", ""), sort_by=args.get("sort_by", "popularity"))
    if name == "get_esports_matches":
        return await ps_team_matches(team=args.get("team", ""), game=args.get("game", ""))
    if name == "get_ukrainian_matches":
        return await ps_ukrainian_matches()
    return "Unknown tool"

# ── Web Search ────────────────────────────────────────────────────────────────
async def web_search(query: str, max_results: int = 5) -> str:
    if not TAVILY_API_KEY:
        return "Web search not configured (add TAVILY_API_KEY to .env)."
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": query, "max_results": max_results},
            )
        if r.status_code != 200:
            return f"Search error HTTP {r.status_code}: {r.text[:200]}"
        data    = r.json()
        results = data.get("results", [])
        if not results:
            return f"No results for: {query}"
        lines = [f"Search: {query}\n"]
        if data.get("answer"):
            lines.append(f"Answer: {data['answer']}\n")
        for item in results[:max_results]:
            title   = item.get("title", "")
            content = item.get("content", "")[:200]
            lines.append(f"• {title}\n  {content}")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"

_ALL_TOOLS = genai.protos.Tool(
    function_declarations=[
        # ── Web search ──────────────────────────────────────────────────────
        genai.protos.FunctionDeclaration(
            name="web_search",
            description=(
                "Search the web for current information. ALWAYS call for: weather, news, "
                "visa rules, prices, current events, or anything time-sensitive."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "query": genai.protos.Schema(type=genai.protos.Type.STRING, description="Search query"),
                },
                required=["query"],
            ),
        ),
        # ── Todoist ─────────────────────────────────────────────────────────
        genai.protos.FunctionDeclaration(
            name="get_todoist_tasks",
            description="Fetch all active Todoist tasks. Call when user asks what to do, their list, or how to prioritize.",
        ),
        genai.protos.FunctionDeclaration(
            name="add_todoist_task",
            description="Add a new task to Todoist.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "content":    genai.protos.Schema(type=genai.protos.Type.STRING, description="Task title"),
                    "due_string": genai.protos.Schema(type=genai.protos.Type.STRING, description="Due date e.g. 'today', 'June 1'"),
                    "priority":   genai.protos.Schema(type=genai.protos.Type.INTEGER, description="4=urgent 3=high 2=medium 1=normal"),
                },
                required=["content"],
            ),
        ),
        # ── Calendar ────────────────────────────────────────────────────────
        genai.protos.FunctionDeclaration(
            name="get_calendar_events",
            description="Fetch upcoming Google Calendar events. Call when user asks about schedule, meetings, or plans.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "days": genai.protos.Schema(type=genai.protos.Type.INTEGER, description="Days ahead to look. Default 7."),
                },
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="create_calendar_event",
            description="Create a new Google Calendar event.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "title":            genai.protos.Schema(type=genai.protos.Type.STRING, description="Event title"),
                    "start_iso":        genai.protos.Schema(type=genai.protos.Type.STRING, description="ISO 8601 e.g. 2026-05-25T15:00:00"),
                    "duration_minutes": genai.protos.Schema(type=genai.protos.Type.INTEGER, description="Duration in minutes. Default 60."),
                    "description":      genai.protos.Schema(type=genai.protos.Type.STRING, description="Optional notes"),
                },
                required=["title", "start_iso"],
            ),
        ),
        # ── Gmail ───────────────────────────────────────────────────────────
        genai.protos.FunctionDeclaration(
            name="get_unread_emails",
            description="Fetch unread emails across all Gmail accounts. Call when user asks about emails, inbox, or messages.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "max_results": genai.protos.Schema(type=genai.protos.Type.INTEGER, description="Max per account. Default 10."),
                },
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="search_emails",
            description="Search emails across all Gmail accounts. Call when user asks to find emails from someone or about a topic.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "query":       genai.protos.Schema(type=genai.protos.Type.STRING, description="Gmail search query e.g. 'from:cornell.edu'"),
                    "max_results": genai.protos.Schema(type=genai.protos.Type.INTEGER, description="Max results per account. Default 10."),
                },
                required=["query"],
            ),
        ),
        # ── TMDB Movies ─────────────────────────────────────────────────────────
        genai.protos.FunctionDeclaration(
            name="tmdb_search",
            description="Search for a movie by title. Returns ratings, year, and overview. Call when user asks about a specific movie.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "query": genai.protos.Schema(type=genai.protos.Type.STRING, description="Movie title to search"),
                },
                required=["query"],
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="tmdb_recommend",
            description="Get movie recommendations similar to a given movie. Call when user asks for movies like X or similar to X.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "movie_title": genai.protos.Schema(type=genai.protos.Type.STRING, description="Movie to base recommendations on"),
                },
                required=["movie_title"],
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="tmdb_discover",
            description="Discover top movies by genre or mood. Call when user asks for good movies in a genre or what to watch tonight.",
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "genre":   genai.protos.Schema(type=genai.protos.Type.STRING, description="Genre: action, comedy, thriller, horror, sci-fi, drama, romance, crime, animation, documentary, mystery"),
                    "sort_by": genai.protos.Schema(type=genai.protos.Type.STRING, description="Sort: popularity, rating, new"),
                },
                required=["genre"],
            ),
        ),
        # ── Esports (PandaScore) ────────────────────────────────────────────
        genai.protos.FunctionDeclaration(
            name="get_esports_matches",
            description=(
                "Get LIVE and UPCOMING matches for an esports team (CS2/Counter-Strike or "
                "Dota 2) by name. ALWAYS call when the user asks whether a team is playing, "
                "their schedule, or upcoming/live games. Works for ANY team — NaVi, B8, "
                "Spirit, etc. Do not answer esports schedules from memory."
            ),
            parameters=genai.protos.Schema(
                type=genai.protos.Type.OBJECT,
                properties={
                    "team": genai.protos.Schema(type=genai.protos.Type.STRING, description="Team name, e.g. 'B8', 'Natus Vincere', 'Team Spirit'"),
                    "game": genai.protos.Schema(type=genai.protos.Type.STRING, description="Optional: 'cs2'/'csgo' or 'dota2'. Leave empty to check both."),
                },
                required=["team"],
            ),
        ),
        genai.protos.FunctionDeclaration(
            name="get_ukrainian_matches",
            description=(
                "Get LIVE and UPCOMING matches for ALL Ukrainian esports teams across CS2 and "
                "Dota 2 (NaVi, B8, Monte, Passion UA, NaVi Junior, Team Hryvnia, Dragon Esports, "
                "Team Lynx). Call when the user asks if ANY Ukrainian team is playing, or for a "
                "Ukrainian esports schedule."
            ),
        ),
    ]
)

_WEB_KEYWORDS = {
    "weather", "forecast", "temperature", "news", "latest", "current events",
    "right now", "today", "price", "exchange rate", "stock", "usd", "visa rules",
    "f-1", "f1 visa", "trump", "breaking", "happened", "update",
}

def _needs_search(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in _WEB_KEYWORDS)

# ── Chat ──────────────────────────────────────────────────────────────────────
async def chat(user_message: str, mode: str = "auto") -> str:
    # build context outside try so except can reference these
    history    = get_recent()
    recent_ids = get_recent_ids()
    sem_ctx    = get_semantic(user_message, recent_ids)
    peer_card  = honcho_context()

    # inject pre-calculated times so model never has to do timezone math
    now_utc    = datetime.now(timezone.utc)
    _baku   = now_utc.astimezone(timezone(timedelta(hours=4)))
    _kyiv   = now_utc.astimezone(timezone(timedelta(hours=3)))
    _london = now_utc.astimezone(timezone(timedelta(hours=1)))
    _nyc    = now_utc.astimezone(timezone(timedelta(hours=-4)))
    time_ctx = (
        f"CURRENT TIME (server-verified, do not guess):\n"
        f"• Baku:   {_baku.strftime('%H:%M, %A %d %b %Y')} (UTC+4)\n"
        f"• Kyiv:   {_kyiv.strftime('%H:%M')} (UTC+3)\n"
        f"• London: {_london.strftime('%H:%M')} (UTC+1)\n"
        f"• NYC:    {_nyc.strftime('%H:%M')} (UTC-4)\n"
        f"• UTC:    {now_utc.strftime('%H:%M')}"
    )

    # pre-fetch for time-sensitive or tool-dependent queries
    pre_search = ""
    if _needs_search(user_message):
        pre_search = await web_search(user_message)

    # pre-fetch movie data so lite model doesn't answer from training data
    _MOVIE_KEYWORDS = {"movie", "film", "watch", "recommend", "similar to", "like inception",
                       "what to watch", "good thriller", "good comedy", "imdb", "tmdb", "rating"}
    low = user_message.lower()
    if any(kw in low for kw in _MOVIE_KEYWORDS):
        if any(kw in low for kw in {"recommend", "similar", "like ", "movies like"}):
            words = user_message.split()
            pre_search = (pre_search + "\n\n" + await tmdb_recommend(user_message)).strip()
        elif any(kw in low for kw in {"watch", "good ", "top ", "best "}):
            genre = next((kw for kw in ["thriller","comedy","horror","action","drama","sci-fi","romance","crime","animation"] if kw in low), "")
            if genre:
                pre_search = (pre_search + "\n\n" + await tmdb_discover(genre)).strip()
        else:
            pre_search = (pre_search + "\n\n" + await tmdb_search(user_message)).strip()

    extra  = "\n\n".join(x for x in [time_ctx, peer_card, sem_ctx, pre_search] if x)
    system = f"{BASE_SYSTEM}\n\n{extra}" if extra else BASE_SYSTEM
    tools  = [_ALL_TOOLS]

    # model order by mode (smart/auto/easy), skipping any on quota cooldown
    models_to_try = _models_to_try(mode)

    last_error = None
    for model_name in models_to_try:
        try:
            m       = genai.GenerativeModel(model_name=model_name, system_instruction=system, tools=tools or None)
            session = m.start_chat(history=history)
            response = session.send_message(user_message)

            # ── function-calling loop (max 6 rounds) ─────────────────────────
            tool_results = []
            for _ in range(6):
                fc_part = None
                for p in response.candidates[0].content.parts:
                    try:
                        if p.function_call.name:
                            fc_part = p
                            break
                    except Exception:
                        pass
                if not fc_part:
                    break
                fc     = fc_part.function_call
                result = await _dispatch(fc.name, dict(fc.args))
                tool_results.append(f"{fc.name}: {result}")
                response = session.send_message(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=fc.name,
                            response={"result": str(result)},
                        )
                    )
                )

            answer = _extract_text(response)
            # Edge case: model ran tools but ended its turn with no text part
            # (it kept wanting to call tools instead of answering). Force a
            # plain-text answer with a TOOLS-FREE model using the results we
            # already gathered — it can't dodge into another tool call.
            if not answer and tool_results:
                try:
                    ctx   = "\n\n".join(tool_results)
                    plain = genai.GenerativeModel(model_name=model_name)
                    r2    = plain.generate_content(
                        f"User asked: {user_message}\n\n"
                        f"Information gathered from tools:\n{ctx}\n\n"
                        "Answer the user directly and concisely in plain text using this "
                        "information. If it doesn't answer the question, say so briefly."
                    )
                    answer = _extract_text(r2)
                except Exception:
                    pass
            answer = answer or "⚠️ No response generated — try rephrasing."

            store_message("user",  user_message)
            store_message("model", answer)
            honcho_store(True,  user_message)
            honcho_store(False, answer)
            return answer

        except Exception as e:
            err = str(e)
            if _is_model_unavailable(err):
                _DEAD_MODELS.add(model_name)   # never try this model again this run
            elif _is_quota_error(err):
                _mark_exhausted(model_name, err)
            # any error (unavailable / quota / other): bounce to the next model
            # instead of hard-failing, so one bad model never freezes the bot.
            last_error = err
            continue

    return (
        "⚠️ Couldn't get a response from any model right now.\n"
        f"Tried: {', '.join(models_to_try)}.\n"
        + (f"(last error: {last_error[:200]})" if last_error else "Try again shortly.")
    )

async def one_shot(prompt: str) -> str:
    last_error = ""
    for model_name in _models_to_try():
        try:
            return genai.GenerativeModel(model_name=model_name).generate_content(prompt).text
        except Exception as e:
            err = str(e)
            if _is_model_unavailable(err):
                _DEAD_MODELS.add(model_name)
            elif _is_quota_error(err):
                _mark_exhausted(model_name, err)
            last_error = err          # bounce to next model on ANY error
            continue
    return f"⚠️ No model could respond right now. ({last_error[:200]})" if last_error else "⚠️ No model could respond right now."

# ── Image generation ──────────────────────────────────────────────────────────
_PREDICT_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:predict"

async def _pollinations(prompt: str) -> bytes | None:
    """Fallback: Pollinations.ai (Flux, free, no key)."""
    import urllib.parse
    encoded = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?model=flux&width=1024&height=1024&nologo=true&enhance=true"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(url)
    return r.content if r.status_code == 200 and r.content else None

async def generate_image(prompt: str) -> bytes | None:
    """Try Imagen 4 models first (25/day), fall back to Pollinations (free)."""
    import base64
    for model_name in IMAGE_MODEL_FALLBACKS:
        try:
            url = _PREDICT_URL.format(model=model_name)
            payload = {
                "instances": [{"prompt": prompt}],
                "parameters": {"sampleCount": 1},
            }
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(url, json=payload, params={"key": GEMINI_API_KEY})
            if r.status_code in (429, 404, 403, 400):
                continue
            if r.status_code != 200:
                continue
            data = r.json()
            b64 = data["predictions"][0]["bytesBase64Encoded"]
            return base64.b64decode(b64)
        except Exception:
            continue
    # all Imagen models failed — use free Pollinations fallback
    return await _pollinations(prompt)

# ── Skills ────────────────────────────────────────────────────────────────────
def list_skills() -> list[tuple]:
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT name, description FROM skills").fetchall()

def save_skill(name: str, description: str, code: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO skills (name, description, code) VALUES (?,?,?)",
            (name, description, code),
        )

def delete_skill(name: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM skills WHERE name=?", (name,))

async def run_skill(name: str, args: str, message: Message) -> str:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT code FROM skills WHERE name=?", (name,)).fetchone()
    if not row:
        return f"Skill '{name}' not found. Use /skills to list available skills."
    path = SKILLS_DIR / f"{name}.py"
    path.write_text(row[0])
    try:
        spec   = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return await module.run(bot, message, args) or "Done."
    except Exception as e:
        return f"Skill '{name}' error: {e}"

# ── Bot ───────────────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

def allowed(msg: Message) -> bool:
    return msg.from_user.id == ALLOWED_USER

# ── Handlers ──────────────────────────────────────────────────────────────────
@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not allowed(msg): return
    diag = await asyncio.to_thread(_creds_diag)
    td   = "✅" if TODOIST_TOKEN else "❌ no token"
    await msg.answer(
        f"Models:   🧠 {MODEL_SMART} → … → ⚡ {MODEL_LITE} → gemini-2.0-flash\n"
        f"Todoist:  {td}\n"
        f"Google accounts (load test):\n  " + "\n  ".join(diag) + "\n"
        f"DB path:  {DB_PATH}\n"
        f"Bot dir:  {_HERE}"
    )

@dp.message(Command("models"))
async def cmd_models(msg: Message):
    if not allowed(msg): return
    try:
        models = [m.name for m in genai.list_models()
                  if "generateContent" in getattr(m, "supported_generation_methods", [])]
        text = "Available models:\n" + "\n".join(f"• {n}" for n in sorted(models))
    except Exception as e:
        text = f"Error listing models: {e}"
    await msg.answer(text)

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not allowed(msg): return
    honcho  = "✅" if _honcho  else "❌ (add HONCHO_API_KEY)"
    todoist = "✅" if TODOIST_TOKEN else "❌ (add TODOIST_TOKEN)"
    await msg.answer(
        "Hermes v2 online.\n\n"
        f"⚡ Default:      {MODEL_SMART} (best, 20 RPD) → … → gemini-2.0-flash\n"
        f"💨 Easy/casual: {MODEL_LITE} (500 RPD) — auto for small talk, or /easy\n"
        f"🧠 !! prefix:   force premium chain (ignore cooldowns)\n"
        f"Memory:          SQLite + semantic search\n"
        f"Honcho:          {honcho}\n"
        f"Todoist:         {todoist}\n\n"
        "Commands: /easy /search /tasks /addtask /skills /skill /learn /forget /clear\n\n"
        "Tiers: ⚡ auto  💨 easy (/easy or casual)  🧠 smart (!! prefix)"
    )

@dp.message(Command("clear"))
async def cmd_clear(msg: Message):
    if not allowed(msg): return
    clear_memory()
    await msg.answer("Memory cleared.")

@dp.message(Command("skills"))
async def cmd_skills(msg: Message):
    if not allowed(msg): return
    skills = list_skills()
    if not skills:
        await msg.answer("No skills yet. Use /learn &lt;name&gt; &lt;description&gt; to create one.")
        return
    lines = [f"• <code>{n}</code> — {d}" for n, d in skills]
    await msg.answer("<b>Skills:</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)

@dp.message(Command("skill"))
async def cmd_run_skill(msg: Message):
    if not allowed(msg): return
    parts = msg.text.removeprefix("/skill").strip().split(None, 1)
    if not parts:
        await msg.answer("Usage: /skill <name> [args]")
        return
    result = await run_skill(parts[0], parts[1] if len(parts) > 1 else "", msg)
    await safe_reply(msg, result)

@dp.message(Command("learn"))
async def cmd_learn(msg: Message):
    if not allowed(msg): return
    parts = msg.text.removeprefix("/learn").strip().split(None, 1)
    if len(parts) < 2:
        await msg.answer("Usage: /learn <name> <what it should do>")
        return
    name, description = parts[0], parts[1]
    await msg.answer(f"Writing skill `{name}`...")
    code = await one_shot(
        f"Write a Python async function `async def run(bot, message, args: str) -> str` "
        f"that does: {description}\n"
        "Only use stdlib and aiogram imports available in the environment. "
        "Return ONLY the Python code, no markdown fences, no explanation."
    )
    if "```" in code:
        code = code.split("```")[1]
        if code.startswith("python\n"):
            code = code[7:]
    save_skill(name, description.strip(), code.strip())
    await msg.answer(f"Skill `{name}` saved. Run it with /skill {name}")

@dp.message(Command("forget"))
async def cmd_forget(msg: Message):
    if not allowed(msg): return
    name = msg.text.removeprefix("/forget").strip()
    if not name:
        await msg.answer("Usage: /forget <skill_name>")
        return
    delete_skill(name)
    await msg.answer(f"Skill `{name}` deleted.")

@dp.message(Command("unread"))
async def cmd_unread(msg: Message):
    if not allowed(msg): return
    result = await gmail_get_unread(max_results=15)
    try:
        await msg.answer(result, parse_mode=ParseMode.HTML)
    except Exception:
        await msg.answer(result)

@dp.message(Command("gmail"))
async def cmd_gmail(msg: Message):
    if not allowed(msg): return
    query = msg.text.removeprefix("/gmail").strip()
    if not query:
        await msg.answer("Usage: /gmail <search query>")
        return
    result = await gmail_search(query, max_results=10)
    try:
        await msg.answer(result, parse_mode=ParseMode.HTML)
    except Exception:
        await msg.answer(result)

@dp.message(Command("tasks"))
async def cmd_tasks(msg: Message):
    if not allowed(msg): return
    if not TODOIST_TOKEN:
        await msg.answer("Todoist not configured. Add TODOIST_TOKEN to .env")
        return
    task_list = await todoist_get_tasks()
    await safe_reply(msg, task_list)
    if not task_list.startswith("Todoist"):  # only analyze if fetch succeeded
        await bot.send_chat_action(msg.chat.id, "typing")
        try:
            analysis = await one_shot(
                f"Here are Elmir's current Todoist tasks:\n\n{task_list}\n\n"
                "Give a sharp, prioritized action plan for today. "
                "Group by urgency. Flag any deadlines. Be direct — 1 sentence per task. "
                "End with one clear 'Start with:' recommendation."
            )
            await safe_reply(msg, analysis)
        except Exception:
            pass  # task list already sent above; analysis is optional

@dp.message(Command("addtask"))
async def cmd_addtask(msg: Message):
    if not allowed(msg): return
    text = msg.text.removeprefix("/addtask").strip()
    if not text:
        await msg.answer("Usage: /addtask Buy milk tomorrow p3")
        return
    # Let Gemini parse content / due / priority from free text
    parsed = await one_shot(
        f"Parse this task request into JSON with fields: content (string), "
        f"due_string (string or empty), priority (int 1-4, 4=urgent). "
        f"Task: \"{text}\"\n"
        "Reply ONLY with valid JSON, nothing else."
    )
    try:
        import json, re
        m = re.search(r'\{.*\}', parsed, re.S)
        data = json.loads(m.group()) if m else {}
    except Exception:
        data = {}
    result = await todoist_add_task(
        content    = data.get("content", text),
        due_string = data.get("due_string", ""),
        priority   = int(data.get("priority", 1)),
    )
    await msg.answer(result)

@dp.message(Command("search"))
async def cmd_search(msg: Message):
    if not allowed(msg): return
    query = msg.text.removeprefix("/search").strip()
    if not query:
        await msg.answer("Usage: /search your question")
        return
    await msg.answer("Searching...")
    try:
        snippets = await web_search(query)
        answer   = await one_shot(f"Using these search results, answer: '{query}'\n\n{snippets}")
        await safe_reply(msg, answer)
    except Exception as e:
        await msg.answer(f"Search failed: {e}")

@dp.message(Command("image"))
async def cmd_image(msg: Message):
    if not allowed(msg): return
    prompt = (msg.text or "").replace("/image", "", 1).strip()
    if not prompt:
        await msg.answer("Usage: /image <description>\nExample: /image a cyberpunk city at night")
        return
    wait = await msg.answer("🎨 Generating image...")
    try:
        image_bytes = await generate_image(prompt)
        if image_bytes:
            await msg.answer_photo(
                photo=BufferedInputFile(image_bytes, filename="image.jpg"),
                caption=f"🎨 {prompt}",
            )
        else:
            await msg.answer("⚠️ All image models quota exhausted. Try again tomorrow.")
    except Exception as e:
        await msg.answer(f"Image generation error: {e}")
    finally:
        await wait.delete()

@dp.message(Command("easy"))
async def cmd_easy(msg: Message):
    if not allowed(msg): return
    text = msg.text.partition(" ")[2].strip()
    if not text:
        await msg.answer("Usage: /easy <message> — answers with the fast high-rate-limit model (saves premium quota).")
        return
    await bot.send_chat_action(msg.chat.id, "typing")
    answer = await chat(text, mode="easy")
    await safe_reply(msg, f"💨 {answer}")

@dp.message(Command("navi"))
async def cmd_navi(msg: Message):
    if not allowed(msg): return
    await msg.answer("Checking NaVi schedule...")
    result = await navi_upcoming_summary()
    await msg.answer(result)

@dp.message(F.text.regexp(r"https?://\S+"))
async def handle_url(msg: Message):
    if not allowed(msg): return
    url = msg.text.split()[0]
    await msg.answer("Reading URL...")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text    = soup.get_text(separator="\n", strip=True)[:8000]
        summary = await one_shot(f"Summarize this content:\n\n{text}")
        await safe_reply(msg, f"**Summary:**\n\n{summary}")
    except Exception as e:
        await msg.answer(f"Could not fetch URL: {e}")

@dp.message(F.document)
async def handle_document(msg: Message):
    if not allowed(msg): return
    doc: Document = msg.document
    if not doc.file_name.lower().endswith(".pdf"):
        await msg.answer("Only PDF files supported.")
        return
    await msg.answer("Reading PDF...")
    try:
        file      = await bot.get_file(doc.file_id)
        raw_bytes = await bot.download_file(file.file_path)
        reader    = PdfReader(io.BytesIO(raw_bytes.read()))
        text      = "\n".join(p.extract_text() or "" for p in reader.pages)[:8000]
        summary   = await one_shot(f"Summarize this PDF:\n\n{text}")
        await safe_reply(msg, f"**PDF Summary:**\n\n{summary}")
    except Exception as e:
        await msg.answer(f"Could not read PDF: {e}")

_IMAGE_TRIGGERS = ("generate ", "imagine ")

@dp.message(F.text)
async def handle_text(msg: Message):
    if not allowed(msg): return
    text = msg.text
    low = text.lower()

    # image trigger words
    for trigger in _IMAGE_TRIGGERS:
        if low.startswith(trigger):
            prompt = text[len(trigger):].strip()
            if prompt:
                wait = await msg.answer("🎨 Generating image...")
                try:
                    image_bytes = await generate_image(prompt)
                    if image_bytes:
                        await msg.answer_photo(
                            photo=BufferedInputFile(image_bytes, filename="image.jpg"),
                            caption=f"🎨 {prompt}",
                        )
                    else:
                        await msg.answer("⚠️ Image generation failed. Try again.")
                except Exception as e:
                    await msg.answer(f"Image error: {e}")
                finally:
                    await wait.delete()
                return

    if text.startswith("!!"):
        text = text[2:].strip()
        mode = "smart"                       # force premium models
    elif _is_trivial(text):
        mode = "easy"                        # casual chat → high-volume model
    else:
        mode = "auto"                        # best-first with fallback
    await bot.send_chat_action(msg.chat.id, "typing")
    tag = {"smart": "🧠", "auto": "⚡", "easy": "💨"}[mode]
    answer = await chat(text, mode=mode)
    await safe_reply(msg, f"{tag} {answer}")

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    init_db()
    init_honcho()
    if PANDASCORE_TOKEN:
        asyncio.create_task(_navi_poll_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
