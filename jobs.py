"""Daily job watcher (New York first, then the San Francisco Bay Area and US remote) for public job-board feeds and first-page Google/Apple search results.

Greenhouse, Lever and Ashby expose public job feeds. Google and Apple are read from their public search pages
using a separate HTML-only fetch; Bloomberg and Two Sigma (Avature search pages) and D. E. Shaw (JSON embedded in its careers page) are read the same way; Oracle Recruiting sites (Oracle, American Express), TikTok and Atlassian through the public job APIs their careers pages use. No login or model call is used by the watcher; plain rules match titles and
locations, so posting text cannot steer it. It never applies to anything. Search-page roles are only listings:
their descriptions, pay, visa terms and experience requirements are not checked.
"""
import asyncio
import html as html_lib
import json
import re
import sqlite3
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
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
GOOGLE_SEARCH = "https://www.google.com/about/careers/applications/jobs/results"
GOOGLE_QUERIES = ["software engineer", "machine learning engineer", "new grad software engineer"]
APPLE_SEARCH = "https://jobs.apple.com/en-us/search"
APPLE_QUERIES = ["software engineer", "machine learning engineer", "new grad software engineer"]
SEARCH_PAGE_LIMIT = 20
AVATURE_PAGE_LIMIT = 40      # Bloomberg is about 30 pages of 12 roles, Two Sigma about 10 pages of 10
PAGE_DELAY = 1.0             # seconds between page requests to one careers site
AVATURE_DETAIL_LIMIT = 20    # at most this many 'Multiple Locations' posting pages opened per company per scan
DESHAW_URL = "https://www.deshaw.com/careers"
ORACLE_QUERIES = ["software engineer", "machine learning"]
ORACLE_PAGE = 100           # Oracle Recruiting returns at most 100 per request
ORACLE_PAGES = 3
TIKTOK_SEARCH = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts"
TIKTOK_NYC = "CT_114"        # TikTok's own location codes: New York, and San Jose for the Bay Area
TIKTOK_PLACES = ["CT_114", "CT_1103355"]
TIKTOK_QUERIES = ["engineer", "machine learning", "data scientist"]
ATLASSIAN_LISTINGS = "https://www.atlassian.com/endpoint/careers/listings"
JIBE_QUERIES = ["software engineer", "machine learning"]
GEM = "https://api.gem.com/job_board/v0/{slug}/job_posts/"     # Gem's documented public Job Board API (Greenhouse-shaped)
RIPPLING = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"   # the public board API behind ats.rippling.com
CONCURRENCY = 4
BROWSER_FILE = Path(__file__).parent / "browser_jobs.json"      # written daily by browser_reader.py on the Mac
BROWSER_STALE_DAYS = 3
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date?" + urlencode({"query": "Ask HN: Who is hiring?", "tags": "story,author_whoishiring", "hitsPerPage": 5})
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
HN_TITLE_WORDS = ("engineer", "developer", "scientist", "researcher", "swe", "sde", "programmer", "architect", "devops", "sre", "mts", "technical staff")
WORKDAY_PAGE = 20
WORKDAY_PAGES = 5            # at most 100 roles per company and run
WORKDAY_EXTRA_PAGES = 2      # San Francisco Bay Area and remote filters: at most 40 more roles per filter
BAY_AREA = ("san francisco", "south san francisco", "oakland", "berkeley", "emeryville", "palo alto", "mountain view", "menlo park",
            "sunnyvale", "santa clara", "san jose", "cupertino", "redwood city", "san mateo", "foster city", "burlingame", "san bruno",
            "milpitas", "fremont", "bay area")
_EXTRA_PLACE = re.compile(r"\b(" + "|".join(BAY_AREA) + r")\b|\bremote\b|\bvirtual\b|work from home", re.I)
AMAZON_LOCATIONS = ["New York, New York, USA", "San Francisco, California, USA"]
GOOGLE_LOCATIONS = ["New York, NY, USA", "San Francisco, CA, USA"]
APPLE_LOCATIONS = {"new-york-city-NYC": "New York City, NY", "san-francisco-bay-area-SFMETRO": "San Francisco Bay Area, CA",
                   "santa-clara-valley-cupertino-SCV": "Santa Clara Valley (Cupertino), CA"}


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
def page_text(page: str) -> str:
    """Visible text of a posting page (scripts, styles and icons removed), capped."""
    page = re.sub(r"(?is)<(script|style|noscript|svg).*?>.*?</\1>", " ", page or "")
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", page))).strip()[:20000]


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


def _is_new_york(descriptor: str) -> bool:
    return descriptor.lower().startswith(("us, ny, new york", "new york"))


def _is_extra_place(descriptor: str) -> bool:
    """A San Francisco Bay Area or remote filter entry. Non-US remote roles are dropped later by the location rules."""
    return bool(_EXTRA_PLACE.search(descriptor)) and not _is_new_york(descriptor)


def _facet_ids(node, parent=None, found=None, match=_is_new_york):
    """Collect (facet name, id) for every matching entry in Workday's filter list (New York by default)."""
    found = {} if found is None else found
    if isinstance(node, dict):
        if match(str(node.get("descriptor", ""))) and "id" in node and parent:
            found.setdefault(parent, []).append(node["id"])
        for key, value in node.items():
            _facet_ids(value, node.get("facetParameter", parent) if key == "values" else parent, found, match)
    elif isinstance(node, list):
        for item in node:
            _facet_ids(item, parent, found, match)
    return found


def workday_jobs(company: dict, postings: list, now=None, label: str = "New York") -> list:
    host, site = company["host"], company["site"]
    now = now or datetime.now(timezone.utc)
    out = []
    for p in postings:
        path = p.get("externalPath", "")
        if not path or not p.get("title"):
            continue
        days = workday_age_days(p.get("postedOn", ""))
        where = p.get("locationsText", "")
        unknown = bool(re.fullmatch(r"\d+ locations?", where.strip(), re.I) or not where)
        if unknown:
            where = f"{label} (one of {where.strip() or 'several locations'})"      # what we asked Workday's filter for
        out.append({"key": f"wd:{company['name']}:{path}", "company": company["name"], "title": p["title"].strip(), "location": where,
                    "remote": bool(re.search(r"\bremote\b|virtual|work from home", where, re.I)),
                    **({"location_verified": False} if unknown and label != "New York" else {}),
                    "url": f"https://{host}/{site}{path}", "published": (now - timedelta(days=days)).isoformat() if days is not None else "",
                    "text": "", "employment": "", "pay": None, "wd_path": path})
    return out


async def fetch_workday(company: dict, get_json, now=None):
    """Two steps: find the New York filter ids, then page through the New York roles. Returns (jobs, error)."""
    base = f"https://{company['host']}/wday/cxs/{company['tenant']}/{company['site']}"
    status, first = await get_json(base + "/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    if status != 200 or not isinstance(first, dict):
        return [], f"HTTP {status}"
    facets = first.get("facets", [])
    ids = _facet_ids(facets)
    extra = _facet_ids(facets, match=_is_extra_place)
    if not ids and not extra:
        return [], "no New York, San Francisco or remote filter found"

    async def pages(applied, limit_pages):
        got = []
        for page in range(limit_pages):
            status, data = await get_json(base + "/jobs", {"appliedFacets": applied, "limit": WORKDAY_PAGE, "offset": page * WORKDAY_PAGE,
                                                           "searchText": company.get("search", "")})
            if status != 200 or not isinstance(data, dict):
                break
            batch = data.get("jobPostings", [])
            got += batch
            if len(batch) < WORKDAY_PAGE or (page + 1) * WORKDAY_PAGE >= data.get("total", 0):
                break
        return got

    found, seen = [], set()
    groups = ([(ids, WORKDAY_PAGES, "New York")] if ids else []) + \
             [({facet: values}, WORKDAY_EXTRA_PAGES, "San Francisco Bay Area or remote") for facet, values in extra.items()]
    for applied, limit_pages, label in groups:              # one query per filter group: Workday ANDs different groups
        for job in workday_jobs(company, await pages(applied, limit_pages), now, label):
            if job["key"] not in seen:
                seen.add(job["key"])
                found.append(job)
    return found, None


async def fetch_amazon(company: dict, get_json):
    jobs_, seen = [], set()
    for place in company.get("locations", AMAZON_LOCATIONS):
        for q in company.get("queries", AMAZON_QUERIES):
            params = urlencode({"base_query": q, "normalized_location[]": place, "result_limit": 50, "sort": "recent"})
            status, payload = await get_json(f"{AMAZON}?{params}")
            if status != 200:
                return jobs_, f"HTTP {status}"
            for j in amazon_jobs(payload, company["name"]):
                if j["key"] not in seen:
                    seen.add(j["key"])
                    jobs_.append(j)
    return jobs_, None


class _SearchLinkParser(HTMLParser):
    """Collect text and labels from anchors that point to actual job detail pages."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.current = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and self.current is None:
            href = attrs.get("href", "")
            path = urlsplit(href).path.lower()
            if re.search(r"(?:^|/)jobs/results/", path) or re.search(r"/en-us/details/[^/]+", path):
                self.current = {"href": href, "text": [], "heading": [], "in_heading": False,
                                "label": attrs.get("aria-label", ""), "title": attrs.get("title", "")}
        elif self.current is not None and tag in ("h1", "h2", "h3"):
            self.current["in_heading"] = True

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"].append(data)
            if self.current["in_heading"]:
                self.current["heading"].append(data)

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag in ("h1", "h2", "h3"):
            self.current["in_heading"] = False
        if tag == "a":
            self.links.append(self.current)
            self.current = None


def _canonical_job_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def _google_title(url: str, label: str) -> str:
    accessible_title = re.sub(r"(?i)^learn more about\s+", "", (label or "").strip()).strip(" .")
    if accessible_title:
        return accessible_title
    path = urlsplit(url).path
    match = re.search(r"/jobs/results/([^/]+)", path)
    if match:
        slug = unquote(match.group(1))
        slug = re.sub(r"^\d+-?", "", slug)
        if slug:
            return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", slug)).strip().title()
    return ""


def _real_apple_title(link: dict) -> str:
    generic = re.compile(r"^(see full role description|where we(?:'|’)re hiring|learn more|apply now|view details|read more)\b", re.I)
    choices = [" ".join(link["heading"]), " ".join(link["text"]), link["label"], link["title"]]
    for raw in choices:
        title = re.sub(r"\s+", " ", raw).strip()
        if title and not generic.search(title):
            return title
    return ""


def parse_google_html(page: str, company: str = "Google", place: str = "New York, NY") -> list:
    """Parse first-page Google results; normalize relative links and deduplicate tracking URLs."""
    parser = _SearchLinkParser()
    parser.feed(page if isinstance(page, str) else "")
    out, seen = [], set()
    base = "https://www.google.com/about/careers/applications/"
    for link in parser.links:
        url = _canonical_job_url(urljoin(base, link["href"]))
        parts = urlsplit(url)
        if parts.hostname != "www.google.com" or "/jobs/results/" not in parts.path:
            continue
        if url in seen:
            continue
        seen.add(url)
        title = _google_title(url, link["label"])
        if not title:
            continue
        out.append({"key": f"google:{company}:{url}", "company": company, "title": title,
                    "location": place, "location_verified": False,
                    "remote": False, "url": url, "published": "",
                    "text": "", "employment": "", "pay": None, "details_checked": False})
        if len(out) >= SEARCH_PAGE_LIMIT:
            break
    return out


def parse_apple_html(page: str, company: str = "Apple", cfg: dict = None, place: str = "New York City, NY") -> list:
    """Keep titled full-time Apple roles and discard generic links and title-level seasonal work."""
    parser = _SearchLinkParser()
    parser.feed(page if isinstance(page, str) else "")
    out, seen = [], set()
    base = "https://jobs.apple.com"
    for link in parser.links:
        url = _canonical_job_url(urljoin(base, link["href"]))
        parts = urlsplit(url)
        if parts.hostname != "jobs.apple.com" or not re.search(r"/en-us/details/[^/]+", parts.path):
            continue
        if url in seen:
            continue
        title = _real_apple_title(link)
        if not title or not title_matches(title, cfg or load_config())[0]:
            continue
        seen.add(url)
        out.append({"key": f"apple:{company}:{url}", "company": company, "title": title,
                    "location": place, "location_verified": False,
                    "remote": False, "url": url, "published": "",
                    "text": "", "employment": "", "pay": None, "details_checked": False})
        if len(out) >= SEARCH_PAGE_LIMIT:
            break
    return out


async def fetch_google(company: dict, get_html):
    """Read the first server-rendered results page for each query; never request page 2."""
    found, errors, seen = [], [], set()
    for place in company.get("locations", GOOGLE_LOCATIONS):
        for query in company.get("queries", GOOGLE_QUERIES):
            params = urlencode({"q": query, "location": place})
            status, page = await get_html(f"{GOOGLE_SEARCH}?{params}")
            if status != 200:
                errors.append(f"HTTP {status}")
                continue
            page_jobs = parse_google_html(page, company.get("name", "Google"), place.rsplit(", USA", 1)[0])
            if not page_jobs:
                errors.append("no job links found on first results page")
            for job in page_jobs:
                if job["url"] not in seen:
                    seen.add(job["url"])
                    found.append(job)
    return found, ("; ".join(errors) if errors else None)


async def fetch_apple(company: dict, get_html, cfg: dict):
    """Read Apple search pages for New York and the Bay Area and retain relevant titled roles only."""
    found, errors, seen = [], [], set()
    for slug, place in company.get("locations", APPLE_LOCATIONS).items():
        for query in company.get("queries", APPLE_QUERIES):
            params = urlencode({"location": slug, "search": query})
            status, page = await get_html(f"{APPLE_SEARCH}?{params}")
            if status != 200:
                errors.append(f"HTTP {status}")
                continue
            page_jobs = parse_apple_html(page, company.get("name", "Apple"), cfg, place)
            if not page_jobs:
                errors.append("no matching titled roles found on results page")
            for job in page_jobs:
                if job["url"] not in seen:
                    seen.add(job["url"])
                    found.append(job)
    return found, ("; ".join(errors) if errors else None)


# ---------- jobs read by a real browser on the Mac (Meta, Microsoft, Netflix) ----------
def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def parse_avature_html(page: str, company: str, host: str) -> list:
    """Jobs from an Avature search results page (Bloomberg, Two Sigma, Deloitte). Only links on the careers host itself are kept.
    Bloomberg shows the location in `list-item-location`; Two Sigma shows location, team and level as plain spans;
    Deloitte shows 'firm | business | location' spans in the subtitle (the location is the last one)."""
    out = []
    for block in re.findall(r'<article class="[^"]*\barticle--result\b[^"]*".*?</article>', page if isinstance(page, str) else "", re.S):
        link = (re.search(r'<a class="link" href="([^"]+/JobDetail/[^"]+)"[^>]*>(.*?)</a>', block, re.S)
                or re.search(r'<a href="([^"]+/JobDetail/[^"]+)"[^>]*class="link"[^>]*>(.*?)</a>', block, re.S))
        if not link:
            continue
        url = html_lib.unescape(link.group(1))
        if urlsplit(url).hostname != host:
            continue
        title = _clean(link.group(2))
        loc = re.search(r'class="list-item-location"[^>]*>(.*?)</span>', block, re.S)
        spans = [_clean(x) for x in re.findall(r'class="paragraph_inner-span"[^>]*>(.*?)</span>', block, re.S)]
        spans = [x for x in spans if x]
        sub = re.search(r'class="article__header__text__subtitle"[^>]*>(.*?)</div>', block, re.S)
        sub_spans = [x for x in (_clean(s) for s in re.findall(r"<span[^>]*>(.*?)</span>", sub.group(1), re.S)) if x] if sub else []
        if loc:
            location, extra = _clean(loc.group(1)), []
        elif spans:
            location, extra = spans[0], spans[1:]
        elif sub_spans:
            location, extra = sub_spans[-1], []
        else:
            location, extra = "", []
        job_id = re.search(r"/(\d+)/?$", urlsplit(url).path)
        if not title:
            continue
        out.append({"key": f"avature:{company}:{job_id.group(1) if job_id else url}", "company": company, "title": title,
                    "location": location, "location_verified": bool(location), "remote": False, "url": url, "published": "",
                    "text": "", "employment": " ".join(extra), "pay": None, "details_checked": False})
    return out


async def fetch_avature(company: dict, get_html):
    """Read the search results page by page (politely, with a pause), until a page adds nothing new."""
    search, name = company["search"], company.get("name", "")
    host = urlsplit(search).hostname
    found, seen, offset, step = [], set(), 0, 0
    for page_number in range(AVATURE_PAGE_LIMIT):
        if page_number:
            await asyncio.sleep(PAGE_DELAY)
        status, page = await get_html(search if offset == 0 else f"{search}{'&' if '?' in search else '?'}jobOffset={offset}")
        if status != 200:
            return found, f"HTTP {status}" + (f" on page {page_number + 1}" if page_number else "")
        jobs_on_page = parse_avature_html(page, name, host)
        new = [j for j in jobs_on_page if j["key"] not in seen]
        if not new:
            break
        step = step or len(jobs_on_page)
        for j in new:
            seen.add(j["key"])
            found.append(j)
        offset += step
    else:
        return found, f"stopped after {AVATURE_PAGE_LIMIT} pages (the list is longer)"
    return found, None


def avature_places(text: str):
    """Cities from an Avature posting page whose list said 'Multiple Locations'. Deloitte prints
    'Same job available in 43 locations <city, state, country> ...' before 'Back to search results'."""
    m = re.search(r"(?i)(?:same job available in \d+ locations?|multiple locations?)\s*:?\s*(.{3,6000}?)\s*back to search results", text or "")
    if not m:
        return None
    places = list(dict.fromkeys(p.strip() for p in re.findall(r"[A-Z][A-Za-z .'-]+?, [A-Z][A-Za-z .'-]+?, (?:United States|USA|US)\b", m.group(1))))
    rank = lambda p: 0 if "new york" in p.lower() else 1 if any(city in p.lower() for city in BAY_AREA) else 2   # shown first when cut short
    return "; ".join(sorted(places, key=rank)) or None


async def resolve_avature_places(found: list, company: dict, cfg: dict, get_html, now=None):
    """Read the posting page of roles listed only as 'Multiple Locations', but only when the title would pass if it were in New York.
    Paced like the search pages and capped, so one big careers site cannot slow the scan for long."""
    read = 0
    for job in found:
        if job["location"].strip().lower() not in ("multiple locations", "multiple location") or read >= AVATURE_DETAIL_LIMIT:
            continue
        if score_job(dict(job, location="New York, NY", tier=company.get("tier", 0)), cfg, now) is None:
            continue                                              # the title or level fails anyway: no need to open it
        read += 1
        await asyncio.sleep(PAGE_DELAY)
        status, page = await get_html(job["url"])
        body = page_text(page) if status == 200 and isinstance(page, str) else ""
        places = avature_places(body)
        if places:
            job["location"], job["location_verified"] = places, True
        if len(body) > 500:
            job["text"], job["details_checked"] = body, True
            job["pay"] = job.get("pay") or pay_from_text(body)
    return found


def parse_deshaw_html(page: str, company: str = "D. E. Shaw") -> list:
    """D. E. Shaw's careers page embeds its public job list as JSON (`regularJobs`). Internal listings are not read."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page if isinstance(page, str) else "", re.S)
    if not m:
        return []
    try:
        records = json.loads(m.group(1))["props"]["pageProps"]["regularJobs"]
    except (ValueError, KeyError, TypeError):
        return []
    out = []
    for rec in records if isinstance(records, list) else []:
        data = rec.get("data") or {}
        title = _clean(rec.get("displayName") or data.get("displayName") or "")
        slug = data.get("jobUrl") or ""
        if not title or not re.fullmatch(r"[A-Za-z0-9-]+", slug) or data.get("activeOnJobsListing") is False:
            continue
        offices = [o.get("name", "") for o in rec.get("office") or [] if isinstance(o, dict)]
        desc = data.get("jobDescription") or {}
        text = _clean(" ".join(str(desc.get(k) or "") for k in ("websiteDescription", "responsibilities", "peopleWeAreLookingForStr")))
        out.append({"key": f"deshaw:{rec.get('id', slug)}", "company": company, "title": title,
                    "location": "; ".join(o for o in offices if o), "location_verified": bool(offices),
                    "remote": any(o.lower().startswith("off-site") for o in offices), "url": f"{DESHAW_URL}/{slug}",
                    "published": "", "text": text, "employment": "", "pay": pay_from_text(text), "details_checked": True})
    return out


async def fetch_deshaw(company: dict, get_html):
    status, page = await get_html(DESHAW_URL)
    if status != 200:
        return [], f"HTTP {status}"
    found = parse_deshaw_html(page, company.get("name", "D. E. Shaw"))
    return found, (None if found else "no job list found on the careers page")


def oracle_jobs(company: dict, items: list) -> list:
    """Oracle Recruiting (Oracle Cloud HCM) requisitions in the common job shape."""
    host, site, name = company["host"], company["site"], company.get("name", "")
    out = []
    for raw in items if isinstance(items, list) else []:
        rid, title = str(raw.get("Id") or ""), _clean(raw.get("Title") or "")
        if not rid.isdigit() or not title:
            continue
        places = [raw.get("PrimaryLocation") or ""] + [s.get("Name", "") for s in raw.get("secondaryLocations") or [] if isinstance(s, dict)]
        places = list(dict.fromkeys(p for p in places if p))
        text = _clean(" ".join(str(raw.get(k) or "") for k in ("ShortDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))
        employment = " ".join(str(raw.get(k) or "") for k in ("JobSchedule", "WorkerType", "ContractType", "JobType"))
        out.append({"key": f"orc:{name}:{rid}", "company": name, "title": title, "location": "; ".join(places),
                    "location_verified": bool(places), "remote": "remote" in str(raw.get("WorkplaceType") or "").lower(),
                    "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                    "published": raw.get("PostedDate") or "", "text": text, "employment": employment,
                    "pay": pay_from_text(text), "details_checked": bool(text)})
    return out


async def fetch_oracle(company: dict, get_json):
    """Keyword searches on the public Oracle Recruiting API, up to three pages each; New York is filtered later."""
    base = f"https://{company['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    found, seen, errors = [], set(), []
    for query in company.get("queries", ORACLE_QUERIES):
        for page in range(ORACLE_PAGES):
            finder = (f"findReqs;siteNumber={company['site']},limit={ORACLE_PAGE},offset={page * ORACLE_PAGE},"
                      f'keyword="{query}"')
            status, data = await get_json(f"{base}?onlyData=true&expand=requisitionList.secondaryLocations&finder={quote(finder, safe='=;,')}")
            if status != 200 or not isinstance(data, dict):
                errors.append(f"HTTP {status}")
                break
            block = (data.get("items") or [{}])[0]
            batch = block.get("requisitionList") or []
            for job in oracle_jobs(company, batch):
                if job["key"] not in seen:
                    seen.add(job["key"])
                    found.append(job)
            if len(batch) < ORACLE_PAGE or (page + 1) * ORACLE_PAGE >= (block.get("TotalJobsCount") or 0):
                break
    return found, ("; ".join(dict.fromkeys(errors)) if errors else None)


def tiktok_jobs(posts: list, company: str = "TikTok") -> list:
    out = []
    for raw in posts if isinstance(posts, list) else []:
        jid, title = str(raw.get("id") or ""), _clean(raw.get("title") or "")
        if not jid.isdigit() or not title:
            continue
        city = (raw.get("city_info") or {}).get("en_name") or ""
        kind = raw.get("recruit_type") or {}
        text = _clean(f"{raw.get('description') or ''} {raw.get('requirement') or ''}")
        out.append({"key": f"tiktok:{jid}", "company": company, "title": title, "location": city, "location_verified": bool(city),
                    "remote": False, "url": f"https://lifeattiktok.com/search/{jid}", "published": "", "text": text,
                    "employment": kind.get("en_name", "") if isinstance(kind, dict) else str(kind),
                    "pay": pay_from_text(text), "details_checked": bool(text)})
    return out


async def fetch_tiktok(company: dict, get_json):
    """TikTok's public job search (the API its careers site calls), New York and San Jose, up to three pages per query."""
    found, seen, errors = [], set(), []
    for query in company.get("queries", TIKTOK_QUERIES):
        for page in range(3):
            body = {"keyword": query, "limit": 100, "offset": page * 100, "job_category_id_list": [],
                    "location_code_list": company.get("location_codes", TIKTOK_PLACES), "subject_id_list": [],
                    "recruitment_id_list": [], "portal_type": 4, "job_function_id_list": [], "portal_entrance": 1}
            status, data = await get_json(TIKTOK_SEARCH, body, headers={"website-path": "tiktok"})
            block = (data or {}).get("data") if isinstance(data, dict) else None
            if status != 200 or not isinstance(block, dict):
                errors.append(f"HTTP {status}")
                break
            posts = block.get("job_post_list") or []
            for job in tiktok_jobs(posts, company.get("name", "TikTok")):
                if job["key"] not in seen:
                    seen.add(job["key"])
                    found.append(job)
            if len(posts) < 100 or (page + 1) * 100 >= (block.get("count") or 0):
                break
    return found, ("; ".join(dict.fromkeys(errors)) if errors else None)


def atlassian_jobs(listings: list, company: str = "Atlassian") -> list:
    out = []
    for raw in listings if isinstance(listings, list) else []:
        jid, title = str(raw.get("id") or ""), _clean(raw.get("title") or "")
        if not jid.isdigit() or not title:
            continue
        places = [re.sub(r"\s+", " ", str(x)).strip() for x in raw.get("locations") or []]
        text = _clean(" ".join(str(raw.get(k) or "") for k in ("overview", "responsibilities", "qualifications", "payRanges")))
        out.append({"key": f"atlassian:{jid}", "company": company, "title": title, "location": "; ".join(places),
                    "location_verified": bool(places), "remote": any(x.lower().startswith("remote") for x in places),
                    "url": f"https://www.atlassian.com/company/careers/details/{jid}", "published": "", "text": text,
                    "employment": "", "pay": pay_from_text(text), "details_checked": bool(text)})
    return out


async def fetch_atlassian(company: dict, get_json):
    status, data = await get_json(ATLASSIAN_LISTINGS)
    if status != 200 or not isinstance(data, list):
        return [], f"HTTP {status}"
    return atlassian_jobs(data, company.get("name", "Atlassian")), None


def jibe_jobs(company: dict, items: list) -> list:
    """Jobs from a Jibe careers site's public /api/jobs list (AMD, GitHub, DocuSign)."""
    host, name = company["host"], company.get("name", "")
    out = []
    for raw in items if isinstance(items, list) else []:
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            continue
        slug, title = str(data.get("slug") or data.get("req_id") or ""), _clean(data.get("title") or "")
        if not re.fullmatch(r"[A-Za-z0-9-]+", slug) or not title:
            continue
        place = data.get("full_location") or ", ".join(x for x in (data.get("city"), data.get("state"), data.get("country")) if x)
        text = _clean(" ".join(str(data.get(k) or "") for k in ("description", "qualifications", "responsibilities")))
        out.append({"key": f"jibe:{name}:{slug}", "company": name, "title": title, "location": place or "",
                    "location_verified": bool(place), "remote": str(data.get("location_type") or "").upper() == "ANY",
                    "url": f"https://{host}/jobs/{slug}", "published": data.get("posted_date") or "", "text": text,
                    "employment": str(data.get("employment_type") or "").replace("_", " "), "pay": pay_from_text(text),
                    "details_checked": bool(text)})
    return out


async def fetch_jibe(company: dict, get_json):
    found, seen, errors = [], set(), []
    for query in company.get("queries", JIBE_QUERIES):
        for page in range(1, 4):
            status, data = await get_json(f"https://{company['host']}/api/jobs?{urlencode({'keywords': query, 'page': page, 'limit': 100})}")
            if status != 200 or not isinstance(data, dict):
                errors.append(f"HTTP {status}")
                break
            batch = data.get("jobs") or []
            for job in jibe_jobs(company, batch):
                if job["key"] not in seen:
                    seen.add(job["key"])
                    found.append(job)
            if len(batch) < 100 or page * 100 >= (data.get("totalCount") or 0):
                break
    return found, ("; ".join(dict.fromkeys(errors)) if errors else None)


def gem_jobs(posts, company: str) -> list:
    """Gem returns Greenhouse-shaped posts that already include the plain-text description."""
    out = []
    for raw in posts if isinstance(posts, list) else []:
        if not isinstance(raw, dict) or not raw.get("id") or not str(raw.get("absolute_url", "")).startswith("https://jobs.gem.com/"):
            continue
        job = normalize("greenhouse", dict(raw, location=raw.get("location") or {"name": ""}), company)
        text = _clean(raw.get("content_plain") or "")
        job.update(key=f"gem:{company}:{raw['id']}", text=text, details_checked=bool(text),
                   employment=str(raw.get("employment_type") or "").replace("_", " "), pay=pay_from_text(text))
        job.pop("gh_id", None)
        out.append(job)
    return [j for j in out if j["title"]]


async def fetch_gem(company: dict, get_json):
    status, data = await get_json(GEM.format(slug=company["slug"]))
    if status != 200 or not isinstance(data, list):
        return [], f"HTTP {status}"
    return gem_jobs(data, company.get("name", company["slug"])), None


def rippling_jobs(posts, company: str, slug: str) -> list:
    """Jobs from a Rippling ATS board's public list (one row per role and location; no description yet).
    Rows of the same role are merged into one job with all its places, so a role is never shown twice."""
    merged = {}
    for job in _rippling_rows(posts, company, slug):
        if job["rippling_id"] in merged:
            first = merged[job["rippling_id"]]
            if job["location"] and job["location"] not in first["location"].split("; "):
                first["location"] = "; ".join(x for x in (first["location"], job["location"]) if x)
            first["remote"] = first["remote"] or job["remote"]
            first["location_verified"] = first["location_verified"] or job["location_verified"]
        else:
            merged[job["rippling_id"]] = job
    return list(merged.values())


def _rippling_rows(posts, company: str, slug: str) -> list:
    out = []
    for raw in posts if isinstance(posts, list) else []:
        if not isinstance(raw, dict):
            continue
        uid, title = str(raw.get("uuid") or ""), _clean(raw.get("name") or "")
        url = str(raw.get("url") or "")
        if not re.fullmatch(r"[0-9a-f-]{36}", uid) or not title or not url.startswith(f"https://ats.rippling.com/{slug}/jobs/"):
            continue
        place = _clean(((raw.get("workLocation") or {}).get("label")) or "")
        out.append({"key": f"rippling:{company}:{uid}", "company": company, "title": title, "location": place,
                    "location_verified": bool(place), "remote": place.lower().startswith("remote"), "url": url, "published": "",
                    "text": "", "employment": "", "pay": None, "details_checked": False, "rippling_id": uid})
    return out


async def fetch_rippling(company: dict, get_json):
    status, data = await get_json(RIPPLING.format(slug=company["slug"]))
    if status != 200 or not isinstance(data, list):
        return [], f"HTTP {status}"
    return rippling_jobs(data, company.get("name", company["slug"]), company["slug"]), None


def rippling_detail(job: dict, detail) -> bool:
    """Fill text, pay and employment from a Rippling job page's JSON. Returns False if it is not full-time salaried work."""
    if not isinstance(detail, dict):
        return True
    desc = detail.get("description")                      # {'company': blurb, 'role': the posting}; the blurb is skipped
    parts = [desc.get("role")] if isinstance(desc, dict) and desc.get("role") else (desc.values() if isinstance(desc, dict) else [desc])
    job["text"] = _clean(" ".join(str(p or "") for p in parts))
    job["details_checked"] = bool(job["text"])
    kind = detail.get("employmentType") or {}
    job["employment"] = str(kind.get("id") or kind.get("label") or "") if isinstance(kind, dict) else str(kind)
    for r in detail.get("payRangeDetails") or []:
        lo, hi = (r.get("rangeStart"), r.get("rangeEnd")) if isinstance(r, dict) else (None, None)
        if isinstance(r, dict) and str(r.get("currency") or "USD").upper() == "USD" and lo and hi and str(r.get("frequency") or "YEAR").upper().startswith("YEAR"):
            job["pay"] = (int(lo), int(hi))
            break
    label = str(kind.get("label") or "").upper() if isinstance(kind, dict) else ""
    return not any(k in label for k in ("INTERN", "CONTRACT", "PART", "TEMP"))


def browser_jobs(company: dict, cfg: dict, now=None, path=None):
    """Reads browser_jobs.json (made by browser_reader.py). Returns (jobs, error_or_None)."""
    path = Path(path or company.get("file") or BROWSER_FILE)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return [], "no browser data yet (run browser_reader.py on the Mac)"
    source = (data.get("sources") or {}).get(company.get("source", company["name"])) or {}
    now = now or datetime.now(timezone.utc)
    try:
        fetched = datetime.fromisoformat(data.get("fetched_at", "").replace("Z", "+00:00"))
        age = (now - fetched).days
    except ValueError:
        fetched, age = None, None
    out = []
    for raw in source.get("jobs", []):
        if not isinstance(raw, dict) or not raw.get("title") or not raw.get("url"):
            continue
        out.append({"key": f"br:{company['name']}:{raw['url']}:{raw['title']}", "company": company["name"], "title": raw["title"].strip(),
                    "location": raw.get("location", ""), "remote": "remote" in (raw.get("location", "") + raw.get("posted", "")).lower(),
                    "url": raw["url"], "published": "", "text": "", "employment": "", "pay": None, "details_checked": False,
                    "location_verified": True if raw.get("location") else False})
    errors = []
    if source.get("error"):
        errors.append(f"browser reader: {source['error']}")
    if age is not None and age >= BROWSER_STALE_DAYS:
        errors.append(f"browser data is {age} days old")
    return out, ("; ".join(errors) or None)


# ---------- Hacker News "Ask HN: Who is hiring?" (a monthly thread where startups post roles) ----------
def hn_job(comment: dict, company_name: str):
    html_text = comment.get("text") or ""
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<p>|<br\s*/?>", "\n", html_text, flags=re.I)))
    lines = [l.strip() for l in plain.splitlines() if l.strip()]
    if not lines or "|" not in lines[0]:
        return None
    head = lines[0]
    parts = [p.strip(" *_") for p in head.split("|") if p.strip()]
    if len(parts) < 3:
        return None
    company = parts[0][:60]
    title = next((p for p in parts[1:5] if any(w in p.lower() for w in HN_TITLE_WORDS)), "")
    if not title:
        return None
    place = [p for p in parts[1:] if p != title and not re.fullmatch(r"(?i)(remote|onsite|on-site|hybrid|full[- ]?time|part[- ]?time)", p)
             and not re.match(r"(?i)(no\s+)?visa\b", p)]
    low = head.lower()
    note = ""
    if re.search(r"\bno\s+visa", low):
        note = "We cannot provide visa sponsorship. "
    elif re.search(r"\bvisa\b", low):
        note = "Visa sponsorship is available (stated by the poster). "
    link = re.search(r'href="([^"]+)"', html_text)
    url = html_lib.unescape(link.group(1)) if link else f"https://news.ycombinator.com/item?id={comment.get('id')}"
    return {"key": f"hn:{comment.get('id')}", "company": company, "title": title[:90], "location": "; ".join(place)[:80] or ("Remote" if "remote" in low else ""),
            "remote": "remote" in low, "url": url, "published": comment.get("created_at", ""), "text": note + " ".join(lines)[:5000],
            "employment": "intern" if "intern" in low else ("parttime" if re.search(r"part[- ]?time", low) else ""), "pay": None}


async def fetch_hn(company: dict, get_json, now=None):
    status, data = await get_json(HN_SEARCH)
    if status != 200 or not isinstance(data, dict):
        return [], f"HTTP {status}"
    now = now or datetime.now(timezone.utc)
    threads = [h for h in data.get("hits", []) if str(h.get("title", "")).startswith("Ask HN: Who is hiring?")]
    if not threads:
        return [], "no hiring thread found"
    status, thread = await get_json(HN_ITEM.format(id=threads[0]["objectID"]))
    if status != 200 or not isinstance(thread, dict):
        return [], f"HTTP {status}"
    out = [j for j in (hn_job(c, company["name"]) for c in thread.get("children", []) if isinstance(c, dict)) if j]
    return out, None


# ---------- matching ----------
def has(text: str, words) -> bool:
    """True when any word or phrase appears as a whole word ('intern' does not match 'internal')."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", text) for w in words)


def location_ok(location: str, remote: bool, cfg: dict) -> bool:
    text = location.lower()
    if has(text, cfg.get("home_locations", [])):
        return True                                           # New York always counts, even next to other cities
    if has(text, cfg.get("locations", [])):
        # other allowed places (the Bay Area) count only in a segment that names no excluded country,
        # so "Costa Rica, San Jose" is out but "San Francisco; London" is in
        return any(has(part, cfg.get("locations", [])) and not has(part, cfg.get("exclude_locations", []))
                   for part in re.split(r"[;|/]|\band\b", text))
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
    levels = re.search(r"(?:^|\s)(\d)\s*/\s*(\d)$", head)       # "Engineer 4/5": senior when even the lower level is 4 or more
    if levels and min(int(levels.group(1)), int(levels.group(2))) >= 4:
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


def rating_label(ratings) -> str:
    """' · pay 5 · prestige 5 · WLB 3 · exits 5' from the company's estimated ratings, or '' when there are none."""
    if not isinstance(ratings, dict):
        return ""
    parts = [f"{label} {ratings[key]}" for key, label in (("pay", "pay"), ("prestige", "prestige"), ("wlb", "WLB"), ("exits", "exits"))]
    return " · " + " · ".join(parts) if all(isinstance(ratings.get(k), int) for k in ("pay", "prestige", "wlb", "exits")) else ""


def tier_from_ratings(ratings: dict) -> int:
    """Tier 1-4 from the sum of the four 1-5 scores (equal weight): 17+ is 1, 15-16 is 2, 12-14 is 3, else 4."""
    total = sum(ratings[k] for k in ("pay", "prestige", "wlb", "exits"))
    return 1 if total >= 17 else 2 if total >= 15 else 3 if total >= 12 else 4


def visa_flags(text: str, ratings=None) -> list:
    flags = []
    if CITIZEN_ONLY.search(text):
        flags.append("⚠️ citizenship or clearance required")
    if NO_SPONSOR.search(text):
        flags.append("⚠️ says no sponsorship")
    if SPONSORS.search(text) and not flags:
        flags.append("✅ mentions sponsorship")
    if flags:
        return flags
    known = (ratings or {}).get("sponsor") if isinstance(ratings, dict) else None
    if known is True:
        return ["visa: not stated (the company usually sponsors; estimate)"]
    if known is False:
        return ["visa: not stated (the company rarely sponsors; estimate)"]
    return ["visa: not stated"]


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
async def scan(cfg: dict, get_json, db_path, mark=True, include_seen=False, limit=None, now=None, collect=None, get_html=None):
    """get_json(url) -> (status, JSON); get_html(url) -> (status, text). Returns text, count and errors."""
    candidates, errors = [], []
    gate = asyncio.Semaphore(CONCURRENCY)                         # a few companies at a time; each is a different server

    async def read_company(company):
        provider, name = company["provider"], company.get("name", company.get("slug"))
        slug = company.get("slug", company.get("tenant", ""))
        async with gate:
            if provider == "amazon":
                found, err = await fetch_amazon(dict(company, name=name), get_json)
            elif provider == "workday":
                found, err = await fetch_workday(dict(company, name=name), get_json, now)
            elif provider == "gem":
                found, err = await fetch_gem(dict(company, name=name), get_json)
            elif provider == "rippling":
                found, err = await fetch_rippling(dict(company, name=name), get_json)
            elif provider == "jibe":
                found, err = await fetch_jibe(dict(company, name=name), get_json)
            elif provider == "tiktok":
                found, err = await fetch_tiktok(dict(company, name=name), get_json)
            elif provider == "atlassian":
                found, err = await fetch_atlassian(dict(company, name=name), get_json)
            elif provider == "oracle":
                found, err = await fetch_oracle(dict(company, name=name), get_json)
            elif provider in ("avature", "deshaw"):
                if get_html is None:
                    found, err = [], "HTML reader is not configured"
                elif provider == "avature":
                    found, err = await fetch_avature(dict(company, name=name), get_html)
                    found = await resolve_avature_places(found, company, cfg, get_html, now)
                else:
                    found, err = await fetch_deshaw(dict(company, name=name), get_html)
            elif provider in ("google", "apple"):
                if get_html is None:
                    found, err = [], "HTML reader is not configured"
                elif provider == "google":
                    found, err = await fetch_google(dict(company, name=name), get_html)
                else:
                    found, err = await fetch_apple(dict(company, name=name), get_html, cfg)
            elif provider == "browser":
                found, err = browser_jobs(company, cfg, now)
            elif provider == "hn":
                found, err = await fetch_hn(dict(company, name=name), get_json, now)
            else:
                url = {"greenhouse": GREENHOUSE, "lever": LEVER, "ashby": ASHBY}[provider].format(slug=slug)
                status, payload = await get_json(url)
                found, err = (jobs_from_feed(provider, payload, name), None) if status == 200 else ([], f"HTTP {status}")
        return company, provider, slug, name, found, err

    results = await asyncio.gather(*(read_company(c) for c in cfg.get("companies", [])))
    for company, provider, slug, name, found, err in results:
        if err:
            errors.append(f"{name}: {err}")
            if not found:
                continue
        for job in found:
            job["tier"] = company.get("tier", 0)
            job["ratings"] = company.get("ratings")
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
        if provider == "rippling" and not job["text"] and job.get("rippling_id"):
            if fetched >= DETAIL_LIMIT * 2:
                continue
            fetched += 1
            status, detail = await get_json(f"{RIPPLING.format(slug=slug)}/{job['rippling_id']}")
            if status == 200 and not rippling_detail(job, detail):
                skipped_keys.append(job["key"])                  # internship, contract or part-time
                continue
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
        if provider == "google" and job.get("details_checked") is False and get_html is not None and fetched < DETAIL_LIMIT * 2:
            fetched += 1                                          # Google's posting pages carry the qualifications and locations as text
            status, page = await get_html(job["url"])
            body = page_text(page) if status == 200 and isinstance(page, str) else ""
            if len(body) > 500:
                job["text"], job["details_checked"] = body, True
                if "new york" in body.lower() or any(city in body.lower() for city in BAY_AREA):
                    job["location_verified"] = True
        job["pay"] = job.get("pay") or pay_from_text(job["text"])
        if not pay_ok(job["pay"], cfg):
            skipped_keys.append(job["key"])                      # known pay below your minimum
            continue
        years = required_years(job["text"])
        if max_years and years is not None and years >= max_years:
            skipped_keys.append(job["key"])                      # asks for too much experience: never show it again
            continue
        job["age"] = age_days(job, now)
        if job.get("details_checked") is False:
            job["flags"] = ["posting details not read; location, pay, visa, experience unchecked"]
        else:
            job["flags"] = [pay_label(job["pay"])] + visa_flags(job["text"], job.get("ratings")) + ([f"asks for {years}+ years"] if years and years >= 3 else [])
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
        if j.get("location_verified") is False:
            where += " (search filter; listing location unverified)"
        tier = f"  [tier {j['tier']}{rating_label(j.get('ratings'))}]" if j.get("tier") else ""
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
