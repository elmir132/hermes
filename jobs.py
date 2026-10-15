"""Daily job watcher for a list of companies, using their public job-board feeds.

Greenhouse, Lever and Ashby publish each company's open roles as public JSON for exactly this kind of use.
No login, no scraping behind a login, no account, and no model call: titles and locations are matched with
plain rules, so nothing written in a posting can steer the bot. It never applies to anything.
Visa wording is only a hint (the posting may say nothing, or be out of date), so always read the posting.
"""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "jobs_config.json"
OUTPUT_CHARS = 3600
DETAIL_LIMIT = 25          # Greenhouse needs one extra request per candidate to read the description

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

NO_SPONSOR = re.compile(
    r"(will not|won't|unable to|cannot|can't|do not|does not|not able to|not currently)\s+(be able to\s+)?(provide|offer|sponsor|support)\b[^.]{0,60}(visa|sponsorship|work authori[sz]ation)"
    r"|no\s+(visa\s+)?sponsorship|sponsorship\s+(is\s+)?not\s+(available|offered|provided)"
    r"|without\s+(the\s+need\s+for\s+)?(visa\s+)?sponsorship", re.I)
CITIZEN_ONLY = re.compile(r"u\.?s\.?\s+citizen(s|ship)?\s+(only|required)|must\s+be\s+a\s+u\.?s\.?\s+citizen|security\s+clearance|ts/sci|top\s+secret", re.I)
SPONSORS = re.compile(r"(visa\s+sponsorship\s+(is\s+)?(available|offered|provided))|((we|will)\s+(provide\s+|offer\s+)?sponsor)|sponsor(s|ship)?\s+(for\s+)?(h-?1b|visas?)", re.I)


def load_config(path=CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text())


# ---------- reading the three feeds ----------
def _first(*values):
    for v in values:
        if v:
            return v
    return ""


def normalize(provider: str, raw: dict, company: str) -> dict:
    """One common shape for all feeds. `text` is filled when the feed already includes the description."""
    if provider == "greenhouse":
        return {"key": f"gh:{company}:{raw.get('id')}", "company": company, "title": raw.get("title", ""),
                "location": (raw.get("location") or {}).get("name", ""), "remote": False,
                "url": raw.get("absolute_url", ""), "published": raw.get("first_published") or raw.get("updated_at") or "",
                "text": "", "gh_id": raw.get("id"), "employment": ""}
    if provider == "lever":
        cats = raw.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        created = raw.get("createdAt")
        published = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else ""
        return {"key": f"lv:{company}:{raw.get('id')}", "company": company, "title": raw.get("text", ""),
                "location": "; ".join(x for x in locs if x), "remote": (raw.get("workplaceType") or "").lower() == "remote",
                "url": raw.get("hostedUrl", ""), "published": published,
                "text": raw.get("descriptionPlain", "") + " " + raw.get("additionalPlain", ""),
                "employment": cats.get("commitment", "")}
    if provider == "ashby":
        locs = [raw.get("location", "")] + [s.get("location", "") if isinstance(s, dict) else str(s) for s in raw.get("secondaryLocations") or []]
        return {"key": f"ab:{company}:{raw.get('id')}", "company": company, "title": (raw.get("title") or "").strip(),
                "location": "; ".join(x for x in locs if x), "remote": bool(raw.get("isRemote")) or (raw.get("workplaceType") or "").lower() == "remote",
                "url": _first(raw.get("jobUrl"), raw.get("applyUrl")), "published": raw.get("publishedAt") or "",
                "text": raw.get("descriptionPlain", ""), "listed": raw.get("isListed", True),
                "employment": raw.get("employmentType", "")}
    raise ValueError(f"unknown provider {provider}")


def jobs_from_feed(provider: str, payload, company: str) -> list:
    if provider == "greenhouse":
        raws = (payload or {}).get("jobs", [])
    elif provider == "lever":
        raws = payload if isinstance(payload, list) else []
    else:
        raws = (payload or {}).get("jobs", [])
    jobs = [normalize(provider, r, company) for r in raws if isinstance(r, dict)]
    return [j for j in jobs if j.get("listed", True) and j["title"] and j["url"]]


# ---------- matching ----------
def has(text: str, words) -> bool:
    """True when any word or phrase appears as a whole word ('intern' does not match 'internal')."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", text) for w in words)


def location_ok(location: str, remote: bool, cfg: dict) -> bool:
    text = location.lower()
    if has(text, cfg.get("locations", [])):
        return True
    if has(text, cfg.get("exclude_locations", [])):
        return False
    if remote or has(text, ["remote"]):
        return not text.strip() or text.strip() == "remote" or has(text, cfg.get("remote_regions", []))
    return False


def title_matches(title: str, cfg: dict):
    """Returns (ok, score, reasons)."""
    t = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+ ]", " ", title.lower()))
    if has(t, cfg.get("exclude_title", [])):
        return False, 0, []
    junior = has(t, cfg.get("junior_words", []))
    strong, plain = has(t, cfg.get("strong_title", [])), has(t, cfg.get("include_title", []))
    if not strong and not plain:
        return False, 0, []
    score, reasons = (3, ["role match"]) if strong else (1, [])
    if junior:
        score += 2
        reasons.append("entry level")
    return True, score, reasons


YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to\s*\d{1,2}\s*)?(?:\d{1,2}\s*)?\+?\s*(?:years?|yrs?)\b[^.\n]{0,80}?experience"
                   r"|experience[^.\n]{0,60}?(\d{1,2})(?:\s*(?:-|–|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b", re.I)


def required_years(text: str):
    """First 'N years of experience' in the posting, or None. A hint only: postings word this loosely."""
    m = YEARS.search(text or "")
    return int(m.group(1) or m.group(2)) if m else None


def visa_flags(text: str) -> list:
    flags = []
    if CITIZEN_ONLY.search(text):
        flags.append("⚠️ citizenship or clearance required")
    if NO_SPONSOR.search(text):
        flags.append("⚠️ says no sponsorship")
    if SPONSORS.search(text) and not flags:
        flags.append("✅ mentions sponsorship")
    return flags or ["visa: not stated"]


def score_job(job: dict, cfg: dict, now=None):
    ok, score, reasons = title_matches(job["title"], cfg)
    if not ok:
        return None
    if has(re.sub(r"[^a-z0-9 ]", " ", (job.get("employment") or "").lower()), cfg.get("exclude_employment", [])):
        return None                                           # the feed says intern, contract or part time
    if not location_ok(job["location"], job["remote"], cfg):
        return None
    if has(job["location"].lower(), cfg.get("home_locations", [])):
        score += 2
    elif job["remote"]:
        score += 1
    now = now or datetime.now(timezone.utc)
    try:
        published = datetime.fromisoformat(job["published"].replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if now - published <= timedelta(days=14):
            score += 1; reasons.append("new")
    except ValueError:
        pass
    return score, reasons


# ---------- memory ----------
def _db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS jobs_seen (job_key TEXT PRIMARY KEY, seen_at TEXT)")
    return con


def seen_keys(path, keys):
    with closing(_db(path)) as con:
        have = {r[0] for r in con.execute("SELECT job_key FROM jobs_seen")}
    return {k for k in keys if k in have}


def mark_seen(path, keys, now=None):
    now = now or datetime.now(timezone.utc)
    with closing(_db(path)) as con, con:
        con.executemany("INSERT OR IGNORE INTO jobs_seen VALUES (?, ?)", [(k, now.isoformat()) for k in keys])
        con.execute("DELETE FROM jobs_seen WHERE seen_at < ?", ((now - timedelta(days=120)).isoformat(),))


# ---------- the scan ----------
async def scan(cfg: dict, get_json, db_path, mark=True, include_seen=False, limit=None, now=None):
    """get_json(url) -> (status_code, parsed_json). Returns (text_to_send, found_count, errors)."""
    candidates, errors = [], []
    for company in cfg.get("companies", []):
        provider, slug, name = company["provider"], company["slug"], company.get("name", company["slug"])
        url = {"greenhouse": GREENHOUSE, "lever": LEVER, "ashby": ASHBY}[provider].format(slug=slug)
        status, payload = await get_json(url)
        if status != 200:
            errors.append(f"{name}: HTTP {status}")
            continue
        for job in jobs_from_feed(provider, payload, name):
            scored = score_job(job, cfg, now)
            if scored:
                job["score"], job["reasons"] = scored
                candidates.append((provider, slug, job))
    keys = [j["key"] for _, _, j in candidates]
    old = set() if include_seen else seen_keys(db_path, keys)
    fresh = [(p, s, j) for p, s, j in candidates if j["key"] not in old]
    fresh.sort(key=lambda x: (-x[2]["score"], x[2]["company"], x[2]["title"]))
    shown, skipped_keys, fetched, per_company = [], [], 0, {}
    cap = cfg.get("max_per_company") or 10**6
    wanted = limit or cfg.get("max_per_message", 12)
    max_years = cfg.get("max_years")
    for provider, slug, job in fresh:
        if len(shown) >= wanted:
            break
        if per_company.get(job["company"], 0) >= cap:           # keep one big company from filling the list
            continue
        if provider == "greenhouse" and not job["text"]:        # the list has no description: read it only when needed
            if fetched >= DETAIL_LIMIT * 2:
                continue
            fetched += 1
            status, detail = await get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job['gh_id']}")
            if status == 200 and isinstance(detail, dict):
                job["text"] = re.sub(r"<[^>]+>", " ", detail.get("content", "")).replace("&nbsp;", " ")
        years = required_years(job["text"])
        if max_years and years is not None and years >= max_years:
            skipped_keys.append(job["key"])                      # asks for too much experience: never show it again
            continue
        job["flags"] = visa_flags(job["text"]) + ([f"asks for {years}+ years"] if years and years >= 3 else [])
        shown.append((provider, slug, job))
        per_company[job["company"]] = per_company.get(job["company"], 0) + 1
    if mark:
        mark_seen(db_path, skipped_keys)
    if not shown:
        return "No new matching roles today." + _errs(errors), 0, errors
    remaining = len(fresh) - len(shown) - len(skipped_keys)      # roughly; capped companies keep their extras for later
    lines = [f"{len(shown)} new role{'s' if len(shown) != 1 else ''} (about {max(remaining, 0)} more waiting):"]
    for _, _, j in shown:
        where = j["location"] if len(j["location"]) <= 60 else j["location"][:57] + "..."
        lines.append(f"\n• {j['company']}: {j['title']}\n  {where}{' (remote ok)' if j['remote'] else ''}\n  {'; '.join(j['flags'])}\n  {j['url']}")
    lines.append("\nVisa and experience notes are only hints from the posting text. Read the posting before relying on them.")
    text = "\n".join(lines)[:OUTPUT_CHARS] + _errs(errors)
    if mark:
        mark_seen(db_path, [j["key"] for _, _, j in shown])
    return text, len(shown), errors


def _errs(errors):
    return ("\n\n⚠️ Couldn't read: " + "; ".join(errors[:6])) if errors else ""
