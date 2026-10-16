"""Daily job watcher for a list of companies, using their public job-board feeds.

Greenhouse, Lever and Ashby publish each company's open roles as public JSON for exactly this kind of use.
No login, no scraping behind a login, no account, and no model call: titles and locations are matched with
plain rules, so nothing written in a posting can steer the bot. It never applies to anything.
Visa wording is only a hint (the posting may say nothing, or be out of date), so always read the posting.
"""
import json
import re
import sqlite3
from urllib.parse import urlencode
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "jobs_config.json"
OUTPUT_CHARS = 3600
DETAIL_LIMIT = 25          # Greenhouse needs one extra request per candidate to read the description

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

NO_SPONSOR = re.compile(
    r"(will not|won't|unable to|cannot|can't|do not|does not|not able to|not currently)\s+(be able to\s+)?(provide|offer|sponsor|support)\b[^.]{0,60}(visa|sponsorship|work authori[sz]ation)"
    r"|no\s+(visa\s+)?sponsorship|sponsorship\s+(is\s+)?not\s+(available|offered|provided)"
    r"|without\s+(the\s+need\s+for\s+)?(visa\s+)?sponsorship", re.I)
CITIZEN_ONLY = re.compile(r"u\.?s\.?\s+citizen(s|ship)?\s+(only|required)|must\s+be\s+a\s+u\.?s\.?\s+citizen|security\s+clearance|ts/sci|top\s+secret", re.I)
SPONSORS = re.compile(r"(visa\s+sponsorship\s+(is\s+)?(available|offered|provided))|((we|will)\s+(provide\s+|offer\s+)?sponsor)|sponsor(s|ship)?\s+(for\s+)?(h-?1b|visas?)", re.I)


AMAZON = "https://www.amazon.jobs/en/search.json"
AMAZON_QUERIES = ["software development engineer", "machine learning engineer", "software engineer new grad"]
WORKDAY_PAGE = 20
WORKDAY_PAGES = 5            # at most 100 roles per company and run


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
                "text": "", "gh_id": raw.get("id"), "employment": "", "pay": None}
    if provider == "lever":
        cats = raw.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        created = raw.get("createdAt")
        published = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if created else ""
        return {"key": f"lv:{company}:{raw.get('id')}", "company": company, "title": raw.get("text", ""),
                "location": "; ".join(x for x in locs if x), "remote": (raw.get("workplaceType") or "").lower() == "remote",
                "url": raw.get("hostedUrl", ""), "published": published,
                "text": raw.get("descriptionPlain", "") + " " + raw.get("additionalPlain", ""),
                "employment": cats.get("commitment", ""), "pay": _lever_pay(raw.get("salaryRange"))}
    if provider == "ashby":
        locs = [raw.get("location", "")] + [s.get("location", "") if isinstance(s, dict) else str(s) for s in raw.get("secondaryLocations") or []]
        return {"key": f"ab:{company}:{raw.get('id')}", "company": company, "title": (raw.get("title") or "").strip(),
                "location": "; ".join(x for x in locs if x), "remote": bool(raw.get("isRemote")) or (raw.get("workplaceType") or "").lower() == "remote",
                "url": _first(raw.get("jobUrl"), raw.get("applyUrl")), "published": raw.get("publishedAt") or "",
                "text": raw.get("descriptionPlain", ""), "listed": raw.get("isListed", True),
                "employment": raw.get("employmentType", ""),
                "pay": pay_from_text((raw.get("compensation") or {}).get("scrapeableCompensationSalarySummary", ""))}
    raise ValueError(f"unknown provider {provider}")


def _lever_pay(rng):
    if isinstance(rng, dict) and "year" in str(rng.get("interval", "")).lower() and rng.get("min") and rng.get("max"):
        return int(rng["min"]), int(rng["max"])
    return None


def jobs_from_feed(provider: str, payload, company: str) -> list:
    if provider == "greenhouse":
        raws = (payload or {}).get("jobs", [])
    elif provider == "lever":
        raws = payload if isinstance(payload, list) else []
    else:
        raws = (payload or {}).get("jobs", [])
    jobs = [normalize(provider, r, company) for r in raws if isinstance(r, dict)]
    return [j for j in jobs if j.get("listed", True) and j["title"] and j["url"]]


# ---------- Amazon and Workday (the sites' own job data; not published as APIs, so they can change) ----------
def _html_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "").replace("&nbsp;", " ")).strip()


def _amazon_published(text: str) -> str:
    try:
        return datetime.strptime(text, "%B %d, %Y").replace(tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def amazon_jobs(payload, company: str) -> list:
    out = []
    for raw in (payload or {}).get("jobs", []) if isinstance(payload, dict) else []:
        if not isinstance(raw, dict) or not raw.get("job_path"):
            continue
        schedule = (raw.get("job_schedule_type") or "").replace("-", "")
        out.append({"key": f"am:{raw.get('id_icims') or raw.get('id')}", "company": company, "title": (raw.get("title") or "").strip(),
                    "location": raw.get("normalized_location") or raw.get("location", ""), "remote": False,
                    "url": "https://www.amazon.jobs" + raw["job_path"], "published": _amazon_published(raw.get("posted_date")),
                    "text": " ".join([_html_text(raw.get("description", "")), _html_text(raw.get("basic_qualifications", "")),
                                      _html_text(raw.get("preferred_qualifications", ""))]),
                    "employment": "intern" if raw.get("is_intern") else schedule, "pay": None})
    return [j for j in out if j["title"]]


def workday_age_days(text: str):
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+?\s*days?", t)
    return int(m.group(1)) if m else None


def _facet_ids(node, parent=None, found=None):
    """Collect (facet name, id) for every New York entry in Workday's filter list."""
    found = {} if found is None else found
    if isinstance(node, dict):
        if str(node.get("descriptor", "")).lower().startswith(("us, ny, new york", "new york")) and "id" in node and parent:
            found.setdefault(parent, []).append(node["id"])
        for key, value in node.items():
            _facet_ids(value, node.get("facetParameter", parent) if key == "values" else parent, found)
    elif isinstance(node, list):
        for item in node:
            _facet_ids(item, parent, found)
    return found


def workday_jobs(company: dict, postings: list, now=None) -> list:
    host, site = company["host"], company["site"]
    now = now or datetime.now(timezone.utc)
    out = []
    for p in postings:
        path = p.get("externalPath", "")
        if not path or not p.get("title"):
            continue
        days = workday_age_days(p.get("postedOn", ""))
        where = p.get("locationsText", "")
        if re.fullmatch(r"\d+ locations?", where.strip(), re.I) or not where:
            where = f"New York (one of {where.strip() or 'several locations'})"      # we asked Workday for New York roles only
        out.append({"key": f"wd:{company['name']}:{path}", "company": company["name"], "title": p["title"].strip(), "location": where, "remote": False,
                    "url": f"https://{host}/{site}{path}", "published": (now - timedelta(days=days)).isoformat() if days is not None else "",
                    "text": "", "employment": "", "pay": None, "wd_path": path})
    return out


async def fetch_workday(company: dict, get_json, now=None):
    """Two steps: find the New York filter ids, then page through the New York roles. Returns (jobs, error)."""
    base = f"https://{company['host']}/wday/cxs/{company['tenant']}/{company['site']}"
    status, first = await get_json(base + "/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    if status != 200 or not isinstance(first, dict):
        return [], f"HTTP {status}"
    ids = _facet_ids(first.get("facets", []))
    if not ids:
        return [], "no New York filter found"
    postings = []
    for page in range(WORKDAY_PAGES):
        status, data = await get_json(base + "/jobs", {"appliedFacets": ids, "limit": WORKDAY_PAGE, "offset": page * WORKDAY_PAGE,
                                                       "searchText": company.get("search", "")})
        if status != 200 or not isinstance(data, dict):
            break
        batch = data.get("jobPostings", [])
        postings += batch
        if len(batch) < WORKDAY_PAGE or (page + 1) * WORKDAY_PAGE >= data.get("total", 0):
            break
    return workday_jobs(company, postings, now), None


async def fetch_amazon(company: dict, get_json):
    jobs_, seen = [], set()
    for q in company.get("queries", AMAZON_QUERIES):
        params = urlencode({"base_query": q, "normalized_location[]": "New York, New York, USA", "result_limit": 50, "sort": "recent"})
        status, payload = await get_json(f"{AMAZON}?{params}")
        if status != 200:
            return jobs_, f"HTTP {status}"
        for j in amazon_jobs(payload, company["name"]):
            if j["key"] not in seen:
                seen.add(j["key"])
                jobs_.append(j)
    return jobs_, None


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
    head = re.split(r"[,(\-–:|]", title)[0].lower().strip()
    if re.search(r"(?:^|\s)(iii|iv|v|vi|[4-9])$", head):          # "Engineer 4", "Developer IV": a senior level number
        return False, 0, []
    t = re.sub(r"member of technical staff", " mts ", t)       # an ordinary engineering title, not the senior "staff" level
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


# ---------- pay ----------
MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?\s?[kK])"
RANGE = re.compile(MONEY + r"\s*(?:-|–|—|to|and)\s*\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?\s?[kK])")
ONE = re.compile(MONEY)


def _amount(text: str):
    text = text.replace(",", "").replace(" ", "")
    return float(text[:-1]) * 1000 if text[-1] in "kK" else float(text)


def pay_from_text(text: str):
    """(low, high) yearly USD from the first '$a - $b' range in the posting, or None. Hourly or tiny numbers are ignored."""
    for m in RANGE.finditer(text or ""):
        lo, hi = sorted((_amount(m.group(1)), _amount(m.group(2))))
        if 40_000 <= lo and hi <= 3_000_000:
            return int(lo), int(hi)
    return None


def pay_label(pay) -> str:
    if not pay:
        return "pay: not stated"
    f = lambda v: f"${v / 1000:.0f}k" if v % 1000 == 0 or v >= 100_000 else f"${v / 1000:.1f}k"
    return f"pay: {f(pay[0])} to {f(pay[1])} base"


def pay_ok(pay, cfg) -> bool:
    """Unknown pay passes (the bot cannot tell); a known range whose top is below the minimum does not."""
    floor = cfg.get("min_pay")
    return not floor or not pay or pay[1] >= floor


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


def age_days(job: dict, now=None):
    """Whole days since the posting was published, or None when the feed gives no usable date."""
    try:
        published = datetime.fromisoformat((job.get("published") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return max((now or datetime.now(timezone.utc)) - published, timedelta(0)).days


def score_job(job: dict, cfg: dict, now=None):
    ok, score, reasons = title_matches(job["title"], cfg)
    if not ok:
        return None
    if has(re.sub(r"[^a-z0-9 ]", " ", (job.get("employment") or "").lower()), cfg.get("exclude_employment", [])):
        return None                                           # the feed says intern, contract or part time
    if not location_ok(job["location"], job["remote"], cfg):
        return None
    score += cfg.get("tier_bonus", {}).get(str(job.get("tier", 0)), 0)
    if has(job["location"].lower(), cfg.get("home_locations", [])):
        score += 2
    elif job["remote"]:
        score += 1
    now = now or datetime.now(timezone.utc)
    age = age_days(job, now)
    if age is not None:
        if cfg.get("max_age_days") and age > cfg["max_age_days"]:
            return None                                       # probably filled or forgotten
        if age <= 14:
            score += 1; reasons.append("new")
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
async def scan(cfg: dict, get_json, db_path, mark=True, include_seen=False, limit=None, now=None, collect=None):
    """get_json(url) -> (status_code, parsed_json). Returns (text_to_send, found_count, errors)."""
    candidates, errors = [], []
    for company in cfg.get("companies", []):
        provider, name = company["provider"], company.get("name", company.get("slug"))
        slug = company.get("slug", company.get("tenant", ""))
        if provider == "amazon":
            found, err = await fetch_amazon(dict(company, name=name), get_json)
        elif provider == "workday":
            found, err = await fetch_workday(dict(company, name=name), get_json, now)
        else:
            url = {"greenhouse": GREENHOUSE, "lever": LEVER, "ashby": ASHBY}[provider].format(slug=slug)
            status, payload = await get_json(url)
            found, err = (jobs_from_feed(provider, payload, name), None) if status == 200 else ([], f"HTTP {status}")
        if err:
            errors.append(f"{name}: {err}")
            if not found:
                continue
        for job in found:
            job["tier"] = company.get("tier", 0)
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
            status, detail = await get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job['gh_id']}?pay_transparency=true")
            if status == 200 and isinstance(detail, dict):
                job["text"] = re.sub(r"<[^>]+>", " ", detail.get("content", "")).replace("&nbsp;", " ")
                for r in detail.get("pay_input_ranges") or []:
                    if r.get("currency_type") == "USD" and r.get("min_cents") and r.get("max_cents"):
                        job["pay"] = (int(r["min_cents"]) // 100, int(r["max_cents"]) // 100)
                        break
        if provider == "workday" and not job["text"]:
            if fetched >= DETAIL_LIMIT * 2:
                continue
            fetched += 1
            status, detail = await get_json(f"https://{slug_host(cfg, job['company'])}/wday/cxs/{slug_path(cfg, job['company'])}{job['wd_path']}")
            info = (detail or {}).get("jobPostingInfo", {}) if status == 200 and isinstance(detail, dict) else {}
            job["text"] = _html_text(info.get("jobDescription", ""))
            if "part" in (info.get("timeType") or "").lower() or "intern" in (info.get("timeType") or "").lower():
                skipped_keys.append(job["key"])
                continue
        job["pay"] = job.get("pay") or pay_from_text(job["text"])
        if not pay_ok(job["pay"], cfg):
            skipped_keys.append(job["key"])                      # known pay below your minimum
            continue
        years = required_years(job["text"])
        if max_years and years is not None and years >= max_years:
            skipped_keys.append(job["key"])                      # asks for too much experience: never show it again
            continue
        job["age"] = age_days(job, now)
        job["flags"] = [pay_label(job["pay"])] + visa_flags(job["text"]) + ([f"asks for {years}+ years"] if years and years >= 3 else [])
        shown.append((provider, slug, job))
        per_company[job["company"]] = per_company.get(job["company"], 0) + 1
    if mark:
        mark_seen(db_path, skipped_keys)
    if not shown:
        return "No new matching roles today." + _errs(errors), 0, errors
    remaining = len(fresh) - len(shown) - len(skipped_keys)      # roughly; capped companies keep their extras for later
    if collect is not None:                                      # lets the bot answer "/fit 3" and "/applied 3" later
        collect[:] = [{"company": j["company"], "title": j["title"], "location": j["location"], "url": j["url"],
                       "text": j["text"][:5000], "pay": j["pay"], "tier": j.get("tier", 0)} for _, _, j in shown]
    lines = [f"{len(shown)} new role{'s' if len(shown) != 1 else ''} (about {max(remaining, 0)} more waiting):"]
    for n, (_, _, j) in enumerate(shown, 1):
        where = j["location"] if len(j["location"]) <= 60 else j["location"][:57] + "..."
        tier = f"  [tier {j['tier']}]" if j.get("tier") else ""
        posted = f" · posted {j['age']}d ago" if j.get("age") is not None else ""
        lines.append(f"\n• {j['company']}{tier}: {j['title']}  #{n}\n  {where}{' (remote ok)' if j['remote'] else ''}{posted}\n  {'; '.join(j['flags'])}\n  {j['url']}")
    lines.append("\nVisa and experience notes are only hints from the posting text. Read the posting before relying on them.")
    text = "\n".join(lines)[:OUTPUT_CHARS] + _errs(errors)
    if mark:
        mark_seen(db_path, [j["key"] for _, _, j in shown])
    return text, len(shown), errors


def _company(cfg, name):
    return next((c for c in cfg.get("companies", []) if c.get("name") == name), {})


def slug_host(cfg, name):
    return _company(cfg, name).get("host", "")


def slug_path(cfg, name):
    c = _company(cfg, name)
    return f"{c.get('tenant', '')}/{c.get('site', '')}"


def _errs(errors):
    return ("\n\n⚠️ Couldn't read: " + "; ".join(errors[:6])) if errors else ""
