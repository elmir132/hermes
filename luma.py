"""Find and rank hackathons in New York from Luma's public pages. It never registers you for anything.

Reads Luma's public New York feed (a curated list of upcoming events) and any public Luma calendars you list in
hackathons_config.json, keeps the ones that look like hackathons, reads each candidate's public page (description,
price, whether it is full or needs approval), and scores them with plain rules. No login, no model call: nothing
written in an event description can steer the bot. Search on Luma needs a login, so only listed events are found.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

BROWSER_FILE = Path(__file__).parent / "browser_jobs.json"        # also holds the Cerebral Valley list (browser_reader.py)
CONFIG_PATH = Path(__file__).parent / "hackathons_config.json"
EXAMPLE_PATH = Path(__file__).parent / "hackathons_config.example.json"
FEED = "https://api2.luma.com/discover/get-paginated-events?"
CALENDAR_ITEMS = "https://api2.luma.com/calendar/get-items?"
EVENT_PAGE = "https://luma.com/"
NY = ZoneInfo("America/New_York")
HACK_WORDS = re.compile(r"hack|buildathon|build[- ]?a[- ]?thon|build day|game ?jam|\bjam\b|sprint|challenge|competition|demo day|ship[- ]?it", re.I)
MAX_DETAIL_PAGES = 14
OUTPUT_CHARS = 3800


def load_config(path=None) -> dict:
    for candidate in ([Path(path)] if path else [CONFIG_PATH, EXAMPLE_PATH]):
        try:
            return json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
    return {}


# ---------- reading Luma ----------
def entry_to_event(entry: dict) -> dict:
    ev = entry.get("event") or {}
    geo = ev.get("geo_address_info") or {}
    return {"id": ev.get("api_id") or entry.get("api_id"), "name": (ev.get("name") or "").strip(), "slug": ev.get("url") or "",
            "start": ev.get("start_at") or "", "end": ev.get("end_at") or "", "online": ev.get("location_type") == "online",
            "place": geo.get("city_state") or geo.get("short_address") or "", "host": ((entry.get("calendar") or {}).get("name") or "").strip(),
            "guests": entry.get("guest_count"), "waitlist": bool(entry.get("waitlist_active"))}


async def fetch_feed(cfg: dict, get_json) -> list:
    events, cursor = [], None
    for _ in range(int(cfg.get("feed_pages", 3))):
        query = {"discover_place_api_id": cfg.get("place", "discplace-Izx1rQVSh8njYpP"), "pagination_limit": 50}
        if cursor:
            query["pagination_cursor"] = cursor
        status, data = await get_json(FEED + urlencode(query))
        if status != 200 or not isinstance(data, dict):
            break
        events += [entry_to_event(e) for e in data.get("entries", []) if isinstance(e, dict)]
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return events


async def fetch_calendar(calendar: dict, get_json, get_html) -> list:
    """A public Luma calendar: give its calendar_api_id, or its page slug and it is looked up."""
    cal_id = calendar.get("calendar_api_id")
    if not cal_id and calendar.get("slug") and get_html:
        status, page = await get_html(EVENT_PAGE + calendar["slug"])
        m = re.search(r'"calendar"\s*:\s*\{[^{}]*?"api_id"\s*:\s*"(cal-[A-Za-z0-9]+)"', page or "")
        cal_id = m.group(1) if m else None
    if not cal_id:
        return []
    events, cursor = [], None
    for _ in range(int(calendar.get("pages", 2))):
        query = {"calendar_api_id": cal_id, "period": "future", "pagination_limit": 50}
        if cursor:
            query["pagination_cursor"] = cursor
        status, data = await get_json(CALENDAR_ITEMS + urlencode(query))
        if status != 200 or not isinstance(data, dict):
            break
        events += [entry_to_event(e) for e in data.get("entries", []) if isinstance(e, dict)]
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return events


def _pm_text(node) -> str:
    if isinstance(node, dict):
        out = [node.get("text", "")] if node.get("type") == "text" else []
        out += [_pm_text(c) for c in node.get("content", []) or []]
        if node.get("type") in ("paragraph", "heading", "list_item", "listItem"):
            out.append("\n")
        return "".join(out)
    if isinstance(node, list):
        return "".join(_pm_text(c) for c in node)
    return ""


def parse_event_page(page: str) -> dict:
    """Description, price and status from an event page's embedded data. {} if it can't be read."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))["props"]["pageProps"]["initialData"]["data"]
    except (ValueError, KeyError, TypeError):
        return {}
    ticket = data.get("ticket_info") or {}
    event = data.get("event") or {}
    return {"start": event.get("start_at") or "", "end": event.get("end_at") or "", "description": re.sub(r"\s+", " ", _pm_text(data.get("description_mirror"))).strip()[:6000],
            "free": ticket.get("is_free"), "sold_out": bool(ticket.get("is_sold_out")), "near_capacity": bool(ticket.get("is_near_capacity")),
            "needs_approval": bool(ticket.get("require_approval")), "availability": data.get("registration_availability"),
            "guests": data.get("guest_count"), "hosts": [h.get("name", "") for h in data.get("hosts", []) if isinstance(h, dict)]}


# ---------- Cerebral Valley's New York hackathon list (read by browser_reader.py) ----------
WHEN = re.compile(r"([A-Z][a-z]{2}),?\s+([A-Z][a-z]{2})\s+(\d{1,2})\s*·\s*(\d{1,2}):(\d{2})\s*([AP]M)")


def parse_when(text: str, now=None):
    """'Fri, Oct 16 · 9:00 AM EDT' -> an aware New York datetime (the nearest such date), or None."""
    m = WHEN.search(text or "")
    if not m:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    try:
        for year in (now.year, now.year + 1):
            hour = int(m.group(4)) % 12 + (12 if m.group(6) == "PM" else 0)
            when = datetime.strptime(f"{year} {m.group(2)} {m.group(3)} {hour}:{m.group(5)}", "%Y %b %d %H:%M").replace(tzinfo=NY)
            if when >= now - timedelta(days=30):
                return when
    except ValueError:
        return None
    return None


def browser_events(cfg: dict, now=None, path=None):
    """Events from the browser file. Returns (events, notes). Luma links are opened later like any Luma event."""
    path = Path(path or cfg.get("browser_file") or BROWSER_FILE)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return [], ["no Cerebral Valley list yet (run browser_reader.py on the Mac)"]
    notes, out = [], []
    try:
        reference = now or datetime.now(timezone.utc)                 # use the caller's clock so the age is testable
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        age = (reference - datetime.fromisoformat(data.get("fetched_at", "").replace("Z", "+00:00"))).days
        if age >= 3:
            notes.append(f"the Cerebral Valley list is {age} days old")
    except ValueError:
        pass
    for source, block in (data.get("events") or {}).items():
        if block.get("error"):
            notes.append(f"{source}: {block['error']}")
        for raw in block.get("events", []):
            start = parse_when(raw.get("when", ""), now)
            url = raw.get("url", "")
            if not start or not raw.get("name") or not url.startswith("http"):
                continue
            m = re.match(r"https?://(?:www\.)?(?:luma\.com|lu\.ma)/([^/?#]+)", url)
            out.append({"id": url, "name": raw["name"].strip(), "slug": m.group(1) if m else "", "link": url, "start": start.astimezone(timezone.utc).isoformat(),
                        "end": (start + timedelta(hours=8)).astimezone(timezone.utc).isoformat(), "end_guessed": True, "online": False, "place": "New York, NY",
                        "host": source, "guests": None, "waitlist": False, "trusted": True})
    return out, notes


# ---------- scoring ----------
MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kKmM])?")


def largest_prize(text: str) -> int:
    """Biggest dollar amount that sits near a word like prize, award, credits or tokens."""
    best = 0
    for m in MONEY.finditer(text or ""):
        value = float(m.group(1).replace(",", ""))
        value *= {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1)
        around = text[max(0, m.start() - 70): m.end() + 70].lower()
        if re.search(r"prize|award|credit|token|worth|pool|grant|winner|cash", around) and value > best:
            best = int(value)
    return best


def local(iso: str):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(NY)
    except ValueError:
        return None


def signed_up_blocks(cfg: dict, skip: str = "") -> list:
    """Events you are going to (from the config's "registered" list) as busy blocks; `skip` leaves one out."""
    return [{"name": r["name"] + " (signed up)", "from": r["start"], "to": r["end"]} for r in cfg.get("registered", [])
            if r.get("status", "going") == "going" and r["name"] != skip]


def _words(name: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 2 and w not in {"nyc", "the", "and", "new", "york"}}


def is_registered(ev: dict, cfg: dict) -> bool:
    for r in cfg.get("registered", []):
        if r.get("slug") and r["slug"] == ev.get("slug"):
            return True
        a, b = _words(r["name"]), _words(ev.get("name", ""))
        if a and b and len(a & b) / min(len(a), len(b)) >= 0.7:
            return True
    return False


def clashes(start, end, cfg: dict, skip: str = "") -> list:
    """Names of your fixed commitments that overlap [start, end] (local times)."""
    found = []
    for block in cfg.get("busy_weekly", []):
        if start.weekday() in block["days"]:
            b0 = start.replace(hour=int(block["from"][:2]), minute=int(block["from"][3:]), second=0, microsecond=0)
            b1 = start.replace(hour=int(block["to"][:2]), minute=int(block["to"][3:]), second=0, microsecond=0)
            if start < b1 and end > b0:
                found.append(block["name"])
    for block in list(cfg.get("busy_dates", [])) + signed_up_blocks(cfg, skip):
        b0 = datetime.fromisoformat(block["from"]).replace(tzinfo=NY)
        b1 = datetime.fromisoformat(block["to"]).replace(tzinfo=NY)
        if start < b1 and end > b0:
            found.append(block["name"])
    return list(dict.fromkeys(found))


def score_event(ev: dict, detail: dict, cfg: dict, now=None):
    """Returns (score, reasons, warnings) or None when the event should not be listed."""
    now = now or datetime.now(timezone.utc)
    start, end = local(ev["start"]), local(ev["end"])
    if not start or not end or end < now.astimezone(NY):
        return None
    text = f"{ev['name']} {detail.get('description', '')}"
    low = text.lower()
    score, reasons, warnings = 0, [], []
    if re.search(r"\b(ai|agents?|agentic|llm|llms|machine learning|ml|genai|generative|model|rag)\b", low):
        score += 3; reasons.append("AI focus")
    if re.search(r"hackathon", ev["name"], re.I):
        score += 2
    elif HACK_WORDS.search(ev["name"]):
        score += 1
    sponsors = [s for s in cfg.get("sponsors", []) if re.search(r"\b" + re.escape(s.lower()) + r"\b", low + " " + ev.get("host", "").lower())]
    if sponsors:
        score += min(len(sponsors), 3); reasons.append("sponsors: " + ", ".join(sponsors[:4]))
    prize = largest_prize(detail.get("description", ""))
    if prize >= 10_000:
        score += 3; reasons.append(f"prizes about ${prize:,}")
    elif prize >= 1_000:
        score += 2; reasons.append(f"prizes about ${prize:,}")
    if detail.get("free"):
        score += 1; reasons.append("free")
    elif detail.get("free") is False:
        score -= 1; warnings.append("paid ticket")
    if not ev["online"]:
        score += 1
    if not ev.get("end_guessed"):                                    # an estimated end time proves nothing about length
        if (end - start) >= timedelta(hours=6):
            score += 1
        if (end - start) >= timedelta(hours=30):
            score += 1; reasons.append("multi-day")
    hours_away = (start - now.astimezone(NY)).total_seconds() / 3600
    if hours_away < 24:
        score -= 1; warnings.append("starts within a day")
    elif hours_away > 48:
        score += 1
    if detail.get("sold_out"):
        score -= 3; warnings.append("sold out")
    elif ev.get("waitlist") or detail.get("availability") not in (None, "open"):
        score -= 2; warnings.append("waitlist or closed")
    if detail.get("needs_approval"):
        warnings.append("needs host approval, apply early")
    conflicts = clashes(start, start + timedelta(hours=3) if ev.get("end_guessed") else end, cfg)
    if conflicts:
        score -= 2; warnings.append("clashes with " + ", ".join(conflicts))
    return score, reasons, warnings


async def find_hackathons(cfg: dict, get_json, get_html, now=None, limit=5):
    """Returns (ranked list of dicts, notes). Never raises on a failing source."""
    notes, events = [], []
    events += await fetch_feed(cfg, get_json)
    city = [w.lower() for w in cfg.get("city_words", ["new york", "brooklyn", "manhattan", "nyc"])]
    for cal in cfg.get("calendars", []):
        found = await fetch_calendar(cal, get_json, get_html)
        if not found:
            notes.append(f"couldn't read the calendar {cal.get('name') or cal.get('slug') or cal.get('calendar_api_id')}")
        if cal.get("only_city", True):                         # big community calendars cover the whole world
            found = [e for e in found if any(w in e["place"].lower() for w in city)]
        for e in found:
            e["trusted"] = bool(cal.get("all_events"))
        events += found
    extra, extra_notes = browser_events(cfg, now)
    events += extra
    notes += extra_notes
    seen, candidates = set(), []
    for ev in events:
        key = ev["slug"] or ev["id"]
        if key in seen or not ev["name"] or not (ev["slug"] or ev.get("link")):
            continue
        seen.add(key)
        if HACK_WORDS.search(ev["name"]) or ev.get("trusted") or ev["host"] in cfg.get("trusted_hosts", []):
            candidates.append(ev)
    ranked = []
    for ev in candidates[:MAX_DETAIL_PAGES]:
        detail = {}
        if get_html and ev["slug"]:
            status, page = await get_html(EVENT_PAGE + ev["slug"])
            detail = parse_event_page(page) if status == 200 else {}
            if detail.get("start") and detail.get("end"):          # the event page knows the real times
                ev = {**ev, "start": detail["start"], "end": detail["end"], "end_guessed": False}
        scored = score_event(ev, detail, cfg, now)
        if scored:
            ranked.append({**ev, "detail": detail, "score": scored[0], "reasons": scored[1], "warnings": scored[2]})
    hidden = [r for r in ranked if is_registered(r, cfg)]
    ranked = [r for r in ranked if not is_registered(r, cfg)]
    if hidden:
        notes.append(f"{len(hidden)} you already signed up for hidden; /hackathons mine lists them")
    ranked.sort(key=lambda r: (-r["score"], r["start"]))
    return ranked[:limit], notes


def format_hackathons(ranked: list, notes: list) -> str:
    if not ranked:
        return "I found no upcoming hackathons in Luma's New York list right now." + (" (" + "; ".join(notes) + ")" if notes else "")
    lines = [f"{len(ranked)} hackathon{'s' if len(ranked) != 1 else ''} worth a look, best first:"]
    for n, r in enumerate(ranked, 1):
        start, end = local(r["start"]), local(r["end"])
        when = start.strftime("%a %b %d, %-I:%M %p")
        if not r.get("end_guessed"):
            when += (" to " + end.strftime("%-I:%M %p") if end.date() == start.date() else " to " + end.strftime("%a %-I:%M %p"))
        price = "free" if r["detail"].get("free") else ("paid" if r["detail"].get("free") is False else "price not read")
        if not r["slug"]:
            price = "details not read (not a Luma page)"
        place = "online" if r["online"] else (r["place"] or "New York")
        guests = r["detail"].get("guests") or r.get("guests")
        lines.append(f"\n{n}. {r['name']}\n   {when} · {place} · {price}" + (f" · {guests} signed up" if guests else "") +
                     (f"\n   why: {'; '.join(r['reasons'])}" if r["reasons"] else "") +
                     (f"\n   watch out: {'; '.join(r['warnings'])}" if r["warnings"] else "") + f"\n   {r.get('link') or 'https://luma.com/' + r['slug']}")
    lines.append("\nThe score is a rough guide from the event page. Read it before you join. I never register you for anything.")
    return ("\n".join(lines))[:OUTPUT_CHARS] + (("\n(" + "; ".join(notes) + ")") if notes else "")


def my_hackathons(cfg: dict, now=None) -> str:
    """Your signed-up and invited events, soonest first, with clashes against classes, fixed dates and each other."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    rows = []
    for r in cfg.get("registered", []):
        start = datetime.fromisoformat(r["start"]).replace(tzinfo=NY)
        end = datetime.fromisoformat(r["end"]).replace(tzinfo=NY)
        if end < now:
            continue
        others = [x for x in clashes(start, end, cfg, skip=r["name"])]
        invited = [x for x in cfg.get("registered", []) if x.get("status") == "invited" and x["name"] != r["name"]]
        for x in invited:                                          # an event you are only invited to can still be a conflict to resolve
            xs, xe = datetime.fromisoformat(x["start"]).replace(tzinfo=NY), datetime.fromisoformat(x["end"]).replace(tzinfo=NY)
            if start < xe and end > xs:
                others.append(x["name"] + " (invited, not registered)")
        rows.append((start, end, r, list(dict.fromkeys(others))))
    if not rows:
        return "I don't have any upcoming hackathons of yours listed."
    rows.sort(key=lambda x: x[0])
    lines = [f"Your upcoming hackathons ({len(rows)}):"]
    for start, end, r, others in rows:
        span = start.strftime("%a %b %d, %-I:%M %p") + (" to " + end.strftime("%-I:%M %p") if end.date() == start.date() else " to " + end.strftime("%a %b %d, %-I:%M %p"))
        status = "" if r.get("status", "going") == "going" else f" ({r['status']}, not registered)"
        lines.append(f"\n• {r['name']}{status}\n  {span}" + (f" · {r['place']}" if r.get("place") else "") +
                     (f"\n  ⚠️ clashes with {', '.join(others)}" if others else ""))
    lines.append("\nFrom your Luma confirmation emails. If two clash, you can only do one, so decide early and cancel the other so a waitlisted person gets the spot.")
    return "\n".join(lines)[:OUTPUT_CHARS]
