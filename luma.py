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
    status, data = await get_json(CALENDAR_ITEMS + urlencode({"calendar_api_id": cal_id, "period": "future", "pagination_limit": 50}))
    if status != 200 or not isinstance(data, dict):
        return []
    return [entry_to_event(e) for e in data.get("entries", []) if isinstance(e, dict)]


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
    return {"description": re.sub(r"\s+", " ", _pm_text(data.get("description_mirror"))).strip()[:6000],
            "free": ticket.get("is_free"), "sold_out": bool(ticket.get("is_sold_out")), "near_capacity": bool(ticket.get("is_near_capacity")),
            "needs_approval": bool(ticket.get("require_approval")), "availability": data.get("registration_availability"),
            "guests": data.get("guest_count"), "hosts": [h.get("name", "") for h in data.get("hosts", []) if isinstance(h, dict)]}


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


def clashes(start, end, cfg: dict) -> list:
    """Names of your fixed commitments that overlap [start, end] (local times)."""
    found = []
    for block in cfg.get("busy_weekly", []):
        if start.weekday() in block["days"]:
            b0 = start.replace(hour=int(block["from"][:2]), minute=int(block["from"][3:]), second=0, microsecond=0)
            b1 = start.replace(hour=int(block["to"][:2]), minute=int(block["to"][3:]), second=0, microsecond=0)
            if start < b1 and end > b0:
                found.append(block["name"])
    for block in cfg.get("busy_dates", []):
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
    conflicts = clashes(start, end, cfg)
    if conflicts:
        score -= 2; warnings.append("clashes with " + ", ".join(conflicts))
    return score, reasons, warnings


async def find_hackathons(cfg: dict, get_json, get_html, now=None, limit=5):
    """Returns (ranked list of dicts, notes). Never raises on a failing source."""
    notes, events = [], []
    events += await fetch_feed(cfg, get_json)
    for cal in cfg.get("calendars", []):
        found = await fetch_calendar(cal, get_json, get_html)
        if not found:
            notes.append(f"couldn't read the calendar {cal.get('slug') or cal.get('calendar_api_id')}")
        events += found
    seen, candidates = set(), []
    for ev in events:
        key = ev["id"] or ev["slug"]
        if key in seen or not ev["name"] or not ev["slug"]:
            continue
        seen.add(key)
        from_calendar = ev["host"] in cfg.get("trusted_hosts", [])
        if HACK_WORDS.search(ev["name"]) or from_calendar:
            candidates.append(ev)
    ranked = []
    for ev in candidates[:MAX_DETAIL_PAGES]:
        detail = {}
        if get_html:
            status, page = await get_html(EVENT_PAGE + ev["slug"])
            detail = parse_event_page(page) if status == 200 else {}
        scored = score_event(ev, detail, cfg, now)
        if scored:
            ranked.append({**ev, "detail": detail, "score": scored[0], "reasons": scored[1], "warnings": scored[2]})
    ranked.sort(key=lambda r: (-r["score"], r["start"]))
    return ranked[:limit], notes


def format_hackathons(ranked: list, notes: list) -> str:
    if not ranked:
        return "I found no upcoming hackathons in Luma's New York list right now." + (" (" + "; ".join(notes) + ")" if notes else "")
    lines = [f"{len(ranked)} hackathon{'s' if len(ranked) != 1 else ''} worth a look, best first:"]
    for n, r in enumerate(ranked, 1):
        start, end = local(r["start"]), local(r["end"])
        when = start.strftime("%a %b %d, %-I:%M %p") + (" to " + end.strftime("%-I:%M %p") if end.date() == start.date() else " to " + end.strftime("%a %-I:%M %p"))
        price = "free" if r["detail"].get("free") else ("paid" if r["detail"].get("free") is False else "price not read")
        place = "online" if r["online"] else (r["place"] or "New York")
        guests = r["detail"].get("guests") or r.get("guests")
        lines.append(f"\n{n}. {r['name']}\n   {when} · {place} · {price}" + (f" · {guests} signed up" if guests else "") +
                     (f"\n   why: {'; '.join(r['reasons'])}" if r["reasons"] else "") +
                     (f"\n   watch out: {'; '.join(r['warnings'])}" if r["warnings"] else "") + f"\n   https://luma.com/{r['slug']}")
    lines.append("\nThe score is a rough guide from the event page. Read it before you join. I never register you for anything.")
    return ("\n".join(lines))[:OUTPUT_CHARS] + (("\n(" + "; ".join(notes) + ")") if notes else "")
