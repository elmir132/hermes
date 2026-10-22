"""Bounded readers for openly published career pages; transport enforces robots."""
import asyncio
import html as html_lib
import json
import re
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin, urlencode, quote, unquote
from xml.etree import ElementTree

from jobs import _clean, pay_from_text, title_matches, score_job, seen_keys, location_ok

MORGAN_RESULTSET = "https://www.morganstanley.com/web/career_services/webapp/service/careerservice/resultset.json"
WORKABLE_JOBS = "https://apply.workable.com/api/v3/accounts/{slug}/jobs"
DETAIL_CAP = 2

def job_postings(page: str) -> list[dict]:
    """Read Schema.org JobPosting objects from a public career detail page."""
    postings = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if not isinstance(types, (list, tuple)):
                types = []
            if any(str(t).rstrip("/").rsplit("/", 1)[-1] == "JobPosting" for t in types):
                postings.append(value)
            else:
                for item in value.values():
                    visit(item)

    scripts = re.findall(
        r"<script\b(?=[^>]*\btype\s*=\s*['\"]application/ld(?:\+|&#x2[bB];|&#43;)json['\"])[^>]*>(.*?)</script\s*>",
        page or "", re.I | re.S)
    for script in scripts:
        try:
            visit(json.loads(re.sub(r"^\s*<!--|-->\s*$", "", script.strip())))
        except (json.JSONDecodeError, TypeError):
            continue
    return postings


def schema_address(value) -> str:
    if isinstance(value, list):
        return "; ".join(dict.fromkeys(filter(None, (schema_address(item) for item in value))))
    if not isinstance(value, dict):
        return ""
    address = value.get("address") if isinstance(value.get("address"), dict) else value
    country = address.get("addressCountry", "")
    if isinstance(country, dict):
        country = country.get("name") or country.get("@id") or ""
    fields = (address.get("addressLocality"), address.get("addressRegion"), country)
    return ", ".join(dict.fromkeys(str(part).strip() for part in fields if part))


def schema_regions(value) -> str:
    """Return the publisher's explicit remote eligibility regions, if any."""
    if isinstance(value, list):
        return "; ".join(dict.fromkeys(filter(None, (schema_regions(item) for item in value))))
    if isinstance(value, dict):
        for key in ("name", "addressCountry", "addressRegion"):
            region = schema_regions(value.get(key))
            if region:
                return region
        return schema_address(value)
    return str(value or "").strip()


def parse_schema_job(page: str, page_url: str, company: str) -> dict | None:
    postings = job_postings(page)
    if not postings:
        return None
    raw = postings[0]
    title = (raw.get("title") or "").strip()
    if not title:
        return None

    allowed_host = urlsplit(page_url).hostname
    canonical = raw.get("url") or page_url
    canonical_parts = urlsplit(canonical)
    if canonical_parts.hostname != allowed_host or canonical_parts.scheme != "https":
        canonical = page_url

    location = schema_address(raw.get("jobLocation"))
    job_location_type = str(raw.get("jobLocationType") or "").lower()
    remote = "telecommute" in job_location_type or str(raw.get("workplaceType") or "").lower() == "remote"
    remote_regions = schema_regions(raw.get("applicantLocationRequirements"))
    if remote and not location:
        location = f"Remote, {remote_regions}" if remote_regions else "Remote (eligibility unspecified)"

    identifier = raw.get("identifier") or {}
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name") or ""
    identifier = str(identifier).strip() or urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    description = _clean(html_lib.unescape(raw.get("description") or ""))
    employment = raw.get("employmentType") or ""
    if isinstance(employment, list):
        employment = ", ".join(str(item) for item in employment)
    return {
        "key": f"schema:{company}:{identifier}", "company": company, "title": title,
        "location": location, "remote": remote, "url": canonical,
        "published": raw.get("datePosted") or "", "text": description,
        "employment": str(employment), "pay": pay_from_text(description),
        "details_checked": True,
        "location_verified": bool(schema_address(raw.get("jobLocation")) or (remote and remote_regions)),
    }


def _boozallen_locations(posting: dict) -> list[str]:
    raw = posting.get("jobLocation") or []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raw = [raw]
    labels = []
    for place in raw:
        if isinstance(place, str):
            label = _clean(place)
        elif isinstance(place, dict):
            address = place.get("address") or {}
            if isinstance(address, list):
                addresses = address
            else:
                addresses = [address]
            for item in addresses:
                if not isinstance(item, dict):
                    continue
                country = item.get("addressCountry") or ""
                if isinstance(country, dict):
                    country = country.get("name") or country.get("alternateName") or country.get("identifier") or ""
                label = ", ".join(str(part).strip() for part in
                                   (item.get("addressLocality"), item.get("addressRegion"), country) if part)
                if label and label not in labels:
                    labels.append(label)
            continue
        else:
            label = ""
        if label and label not in labels:
            labels.append(label)
    return labels


def parse_boozallen_job(page: str, page_url: str, company: str = "Booz Allen", published: str = ""):
    """Parse one public Booz Allen posting. Preserve the explicit Remote Work field and US location."""
    fields = {}
    pair_pattern = re.compile(
        r"<div\b(?=[^>]*class=[\"'][^\"']*field__label[^\"']*[\"'])[^>]*>(.*?)</div>\s*"
        r"<div\b(?=[^>]*class=[\"'][^\"']*field__value[^\"']*[\"'])[^>]*>(.*?)</div>",
        re.I | re.S,
    )
    for label, value in pair_pattern.findall(page or ""):
        fields[_clean(label).rstrip(":").casefold()] = _clean(value)

    postings = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            kind = [kind] if isinstance(kind, str) else kind
            if isinstance(kind, (list, tuple)) and any(str(t).rstrip("/").rsplit("/", 1)[-1].casefold() == "jobposting" for t in kind):
                postings.append(value)
            else:
                for item in value.values():
                    visit(item)

    for script in re.findall(r"<script\b[^>]*>(.*?)</script\s*>", page or "", re.I | re.S):
        if "jobposting" not in script.casefold():
            continue
        try:
            visit(json.loads(script.strip().rstrip(";")))
        except (json.JSONDecodeError, TypeError):
            continue
    if not postings:
        return None, "job page has no readable JobPosting data"
    posting = postings[0]
    title = _clean(posting.get("title") or "")
    if not title:
        return None, "job page has no title in JobPosting data"
    remote_value = fields.get("remote work")
    remote = (remote_value or "").casefold() == "yes"

    locations = _boozallen_locations(posting)
    field_location = fields.get("location", "")
    if field_location and not any(field_location.casefold() in value.casefold() for value in locations):
        locations.append(field_location)
    location_text = "; ".join(locations) or field_location
    if not re.search(r"\b(?:united states(?: of america)?|u\.?s\.?a?\.?|usa)\b", location_text, re.I):
        return None, None

    description = _clean(posting.get("description") or "")[:20000]
    identifier = posting.get("identifier") or {}
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name") or ""
    if isinstance(identifier, (list, dict)):
        identifier = ""
    path_id = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
    key_id = str(identifier or path_id)
    employment = posting.get("employmentType") or ""
    if isinstance(employment, list):
        employment = ", ".join(str(value) for value in employment)
    base_location = location_text or "United States"
    return ({
        "key": f"ba:{key_id}", "company": company, "title": title,
        "location": f"Remote, United States (posted location: {base_location})" if remote else base_location, "remote": remote,
        "location_verified": True, "url": page_url,
        "published": posting.get("datePosted") or published, "text": description,
        "employment": str(employment), "pay": pay_from_text(description),
        "details_checked": bool(description),
    }, None)


class _IntuitCards(HTMLParser):
    """Read job cards from Intuit's server-rendered TalentBrew search results."""

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self.stack = []
        self.current = None
        self.card_depth = None
        self.location_depth = None
        self.location_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        depth = len(self.stack)
        if tag == "li" and attrs.get("data-intuit-jobid"):
            if self.current:
                self._finish_card()
            self.current = {"id": attrs["data-intuit-jobid"], "title": "", "href": "", "location": ""}
            self.card_depth = depth
        if self.current:
            classes = set((attrs.get("class") or "").split())
            if tag == "a" and ("sr-item" in classes or attrs.get("data-title")):
                self.current["href"] = attrs.get("href") or ""
                self.current["title"] = attrs.get("data-title") or self.current["title"]
            if tag in ("span", "div") and "job-location" in classes:
                self.location_depth = depth
                self.location_parts = []
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_data(self, data):
        if self.current and self.location_depth is not None:
            self.location_parts.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i] == tag), None)
        if index is None:
            return
        if self.current and self.location_depth == index:
            self.current["location"] = _clean(" ".join(self.location_parts))
            self.location_depth = None
            self.location_parts = []
        if self.current and tag == "li" and self.card_depth == index:
            self._finish_card()
        del self.stack[index:]

    def _finish_card(self):
        if self.location_depth is not None and self.current:
            self.current["location"] = _clean(" ".join(self.location_parts))
        self.cards.append(self.current)
        self.current = None
        self.card_depth = None
        self.location_depth = None
        self.location_parts = []


def intuit_jobs(page: str, company: str = "Intuit") -> list:
    parser = _IntuitCards()
    parser.feed(page or "")
    parser.close()
    if parser.current:
        parser._finish_card()
    out = []
    for raw in parser.cards:
        title, job_id = _clean(raw.get("title")), str(raw.get("id") or "").strip()
        url = urljoin("https://jobs.intuit.com", html_lib.unescape(raw.get("href") or ""))
        parts = urlsplit(url)
        if (not job_id.isdigit() or not title or parts.scheme != "https" or parts.hostname != "jobs.intuit.com"
                or not parts.path.startswith("/job/")):
            continue
        place = _clean(raw.get("location"))
        out.append({"key": f"intuit:{job_id}", "company": company, "title": title, "location": place,
                    "location_verified": bool(place), "remote": "remote" in place.casefold(), "url": url,
                    "published": "", "text": "", "employment": "", "pay": None, "details_checked": False})
    return out


def morgan_jobs(rows: list, company: str = "Morgan Stanley") -> list:
    """Normalize the student-and-graduate results used by Morgan Stanley's public careers search."""
    out = []
    for raw in rows if isinstance(rows, list) else []:
        job_id = str(raw.get("jobNumber") or "").strip()
        title = _clean(raw.get("jobTitle") or "")
        url = html_lib.unescape(str(raw.get("url") or ""))
        parts = urlsplit(url)
        if (not job_id.isdigit() or not title or parts.scheme != "https" or parts.hostname != "morganstanley.tal.net"
                or "/opp/" not in parts.path):
            continue
        place = _clean(raw.get("location") or "")
        text = _clean(raw.get("jobDescription") or "")
        employment = _clean(raw.get("employmentType") or "")
        remote = "remote" in place.casefold()
        out.append({"key": f"morganstanley:{job_id}", "company": company, "title": title, "location": place,
                    "location_verified": bool(place), "remote": remote, "url": url,
                    "published": raw.get("postedDate") or "", "text": text, "employment": employment,
                    "pay": pay_from_text(text), "details_checked": len(text) > 300})
    return out


def sitemap_candidates(page, company, cfg):
    """Use only same-host job URLs. A sitemap title is a hint, never a listing."""
    root = ElementTree.fromstring(page)
    source = urlsplit(company['sitemap_url'])
    rows = []
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] != 'url':
            continue
        values = {n.tag.rsplit('}', 1)[-1]: (n.text or '').strip() for n in node}
        url = values.get('loc', '')
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.netloc != source.netloc or parts.query or parts.fragment
                or not parts.path.startswith(company['job_path'])):
            continue
        segments = parts.path.rstrip('/').split('/')
        segment = company.get('title_segment', -1)
        if len(segments) < abs(segment):
            continue
        title = re.sub(r'[-_]+', ' ', unquote(segments[segment]))
        if company.get('parser') == 'boozallen':
            title = re.sub(r'[-_]+', ' ', unquote(parts.path.split('/')[-2]))
        if title_matches(title, cfg)[0]:
            rows.append((url, title, values.get('lastmod', '')))
    rows.sort(key=lambda r: (-bool(re.search(r'junior|new.grad|early.career|engineer i\b', r[1], re.I)),
                             -any(place in re.sub('[-_]', ' ', unquote(r[0])).lower() for place in cfg.get('locations', [])),
                             r[2], r[0]))
    return list(dict.fromkeys(r[0] for r in rows))


async def fetch_sitemap(company, cfg, public, seen_path=None):
    status, text = await public.text(company['sitemap_url'])
    if status != 200:
        return [], f'sitemap HTTP {status}'
    try:
        urls = sitemap_candidates(text, company, cfg)
    except (ElementTree.ParseError, TypeError):
        return [], 'invalid sitemap XML'
    # Seen URLs avoid spending the small cap on delivered/rejected records.
    old = seen_keys(seen_path, ['page:' + u for u in urls]) if seen_path is not None else set()
    urls = [u for u in urls if 'page:' + u not in old]
    found = []
    for url in urls[:DETAIL_CAP]:
        status, page = await public.text(url)
        if status in (404, 410):
            continue          # the sitemap still lists a posting that was taken down; not a source failure
        if status != 200:
            return found, f'posting HTTP {status}'
        if company.get('parser') == 'boozallen':
            job, issue = parse_boozallen_job(page, url, company['name'])
            if issue:
                return found, issue
            if job is None:
                continue
        else:
            job = parse_schema_job(page, url, company['name'])
        if job is None or not job['text']:
            return found, 'posting omitted JobPosting description'
        job['key'] = 'page:' + url
        found.append(job)
    return found, None


async def fetch_morgan(company, public):
    url = MORGAN_RESULTSET + '?' + urlencode({'opportunity':'sg', 'lang':'en'})
    status, data = await public.json(url)
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get('resultSet'), list):
        return [], f'invalid graduate resultset (HTTP {status})'
    return morgan_jobs(data['resultSet'], company['name']), None


async def fetch_intuit(company, cfg, public, seen_path=None):
    found = {}
    # One page per query, then two complete descriptions in total.
    for query in company.get('queries', ('software engineer 1', 'machine learning engineer'))[:2]:
        url = 'https://jobs.intuit.com/search-jobs?' + urlencode({'k':query, 'p':1})
        status, page = await public.text(url)
        if status != 200:
            return [], public_error(public, url, f'search HTTP {status}')
        rows = intuit_jobs(page, company['name'])
        if not rows:
            return [], 'search omitted job cards'
        found.update((j['key'], j) for j in rows)
    candidates = [j for j in found.values() if title_matches(j['title'], cfg)[0] and
                  (location_ok(j['location'], j['remote'], cfg) or j['location'].casefold() == 'multiple locations')]
    old = seen_keys(seen_path, [j['key'] for j in candidates]) if seen_path is not None else set()
    candidates = [j for j in candidates if j['key'] not in old]
    candidates.sort(key=lambda j: (-title_matches(j['title'], cfg)[1], j['title']))
    complete = []
    for job in candidates[:DETAIL_CAP]:
        status, page = await public.text(job['url'])
        if status != 200:
            return complete, public_error(public, job['url'], f'posting HTTP {status}')
        detail = parse_schema_job(page, job['url'], company['name'])
        if detail is None or not detail['text']:
            return complete, 'posting omitted JobPosting description'
        detail['key'] = job['key']
        complete.append(detail)
    return complete, None


def workable_jobs(payload, company):
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        return []
    found = []
    for raw in payload['results']:
        code = str(raw.get('shortcode') or '') if isinstance(raw, dict) else ''
        if not re.fullmatch(r'[A-Z0-9]+', code) or raw.get('state') != 'published' or raw.get('isInternal'):
            continue
        locations = raw.get('locations') or [raw.get('location')]
        if isinstance(locations, dict):
            locations = [locations]
        places = [', '.join(str(p.get(k) or '') for k in ('city','region','country') if p.get(k))
                  for p in locations if isinstance(p, dict)]
        found.append({'key': f"wb:{company['name']}:{code}", 'company': company['name'],
                      'title':_clean(raw.get('title') or ''), 'location':'; '.join(places),
                      'remote':raw.get('remote') is True or raw.get('workplace') == 'remote',
                      'url':f"https://apply.workable.com/{quote(company['slug'], safe='')}/j/{code}/",
                      'published':raw.get('published') or '', 'text':'', 'employment':raw.get('type') or '',
                      'pay':None, 'workable_shortcode':code})
    return found


async def fetch_workable(company, cfg, public, seen_path=None):
    url = WORKABLE_JOBS.format(slug=quote(company['slug'], safe=''))
    status, data = await public.json(url, {})
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get('results'), list):
        return [], f'Workable list HTTP {status}'
    if data.get('total') != len(data['results']):
        return [], 'Workable list is partial; pagination required'
    rows = workable_jobs(data, company)
    candidates = [j for j in rows if title_matches(j['title'], cfg)[0] and location_ok(j['location'], j['remote'], cfg)]
    old = seen_keys(seen_path, [j['key'] for j in candidates]) if seen_path is not None else set()
    complete = []
    for job in [j for j in candidates if j['key'] not in old][:DETAIL_CAP]:
        status, detail = await public.json(f"https://apply.workable.com/api/v2/accounts/{quote(company['slug'], safe='')}/jobs/{job['workable_shortcode']}")
        if status != 200 or not isinstance(detail, dict) or not detail.get('description'):
            return complete, f'Workable detail HTTP {status}'
        job['text'] = _clean(' '.join(str(detail.get(k) or '') for k in ('description','requirements','benefits')))
        job['pay'] = pay_from_text(job['text'])
        job['details_checked'] = True
        complete.append(job)
    return complete, None


async def read(company, cfg, public, seen_path=None):
    """A hard wall-clock budget bounds each custom source, including its details."""
    async def run():
        if company['provider'] == 'morganstanley':
            return await fetch_morgan(company, public)
        if company['provider'] == 'intuit':
            return await fetch_intuit(company, cfg, public, seen_path)
        if company['provider'] == 'workable':
            return await fetch_workable(company, cfg, public, seen_path)
        if company["provider"] == "recruitee":
            return await fetch_recruitee(company, public)
        if company["provider"] == "guardant":
            return await fetch_guardant(company, public)
        if company["provider"] == "teamtailor_rss":
            return await fetch_teamtailor(company, public)
        return await fetch_sitemap(company, cfg, public, seen_path)
    budget = 60 if company['provider'] == 'intuit' else 20
    try:
        return await asyncio.wait_for(run(), timeout=budget)
    except TimeoutError:
        return [], f'public source exceeded {budget}-second budget'


def teamtailor_jobs(page, company):
    """Parse the publisher's public Teamtailor RSS, including per-job locations."""
    root = ElementTree.fromstring(page)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("not an RSS job feed")
    out = []
    namespace = {"tt": "https://teamtailor.com/locations"}
    for item in root.findall("./channel/item")[:250]:
        title, url = item.findtext("title", ""), item.findtext("link", "")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != urlsplit(company["feed_url"]).netloc or not parts.path.startswith("/jobs/"):
            continue
        description = item.find("description")
        text = _clean(html_lib.unescape(ElementTree.tostring(description, encoding="unicode"))) if description is not None else ""
        if not title or not text:
            continue
        locations = []
        for place in item.findall("tt:locations/tt:location", namespace):
            location = ", ".join(filter(None, (place.findtext("tt:city", "", namespace), place.findtext("tt:country", "", namespace))))
            if location:
                locations.append(location)
        published = item.findtext("pubDate", "")
        try:
            published = parsedate_to_datetime(published).isoformat()
        except (ValueError, TypeError, OverflowError):
            published = ""
        remote = item.findtext("remoteStatus", "").casefold() in ("fully", "fully_remote", "remote")
        out.append({"key": "page:" + url, "company": company["name"], "title": title,
                    "url": url, "location": "; ".join(dict.fromkeys(locations)), "remote": remote,
                    "published": published, "text": text, "employment": "", "pay": pay_from_text(text),
                    "details_checked": True, "location_verified": bool(locations)})
    return out


async def fetch_teamtailor(company, public):
    status, page = await public.text(company["feed_url"])
    if status != 200:
        return [], f"RSS HTTP {status}"
    try:
        return teamtailor_jobs(page, company), None
    except (ElementTree.ParseError, ValueError, TypeError):
        return [], "invalid RSS job feed"


async def fetch_guarded_rippling(company, cfg, public):
    """One board request and at most two candidate details, inside a 20s caller budget."""
    from jobs import fetch_rippling, rippling_detail, RIPPLING
    found, error = await fetch_rippling(company, public.json)
    if error:
        return [], error
    candidates = [j for j in found if score_job(dict(j, tier=company.get("tier", 0)), cfg)]
    candidates.sort(key=lambda j: (-bool(re.search(r"junior|early.career|new.grad", j["title"], re.I)), j["key"]))
    out = []
    for job in candidates[:DETAIL_CAP]:
        status, data = await public.json(RIPPLING.format(slug=company["slug"]) + "/" + job["rippling_id"])
        if status != 200 or not isinstance(data, dict):
            return out, f"posting HTTP {status}"
        if rippling_detail(job, data) and job["text"]:
            job["details_checked"] = True
            out.append(job)
    return found, None


async def fetch_guarded_workday(company, cfg, public, now=None):
    """At most four listing calls and two details, with a 20s caller deadline."""
    from jobs import fetch_workday, _html_text, pay_from_text, ListingCap
    calls = 0
    failure = None
    limited = False
    async def bounded_json(url, body=None):
        nonlocal calls, failure, limited
        calls += 1
        if calls > 4:
            limited = True
            return 200, {"jobPostings": [], "total": 0}
        status, data = await public.json(url, body)
        if status != 200:
            failure = f"listing HTTP {status}"
        return status, data
    found, error = await fetch_workday(dict(company, page_cap=1), bounded_json, now)
    if failure or (error and not isinstance(error, ListingCap)):
        return found, failure or error
    if limited:
        error = ListingCap('; '.join(filter(None, [error, 'listing request cap reached: 4 calls'])))
    candidates = [j for j in found if score_job(dict(j, tier=company.get("tier", 0)), cfg, now)]
    candidates.sort(key=lambda j: (-bool(re.search(r"junior|early.career|new.grad", j["title"], re.I)), j["key"]))
    base = f"https://{company['host']}/wday/cxs/{company['tenant']}/{company['site']}"
    for job in candidates[:DETAIL_CAP]:
        status, data = await public.json(base + job["wd_path"])
        info = data.get("jobPostingInfo", {}) if isinstance(data, dict) else {}
        if status != 200 or not info.get("jobDescription"):
            return found, f"posting unavailable (HTTP {status})"
        job["text"] = _html_text(info["jobDescription"])
        job["employment"] = info.get("timeType", "")
        job["details_checked"] = True
        job["pay"] = pay_from_text(job["text"])
        if any(term in job["employment"].casefold() for term in ("part", "intern", "contract", "temp")):
            job["text"] = ""  # Guarded scan skips this incomplete, non-full-time record.
    return found, error


def guardant_jobs(payload, company):
    """Normalize the complete job descriptions published by the site's own widget."""
    data = payload.get('data') if isinstance(payload, dict) else None
    rows = data.get('data') if isinstance(data, dict) else None
    if not payload.get('success') or not isinstance(rows, list):
        raise ValueError('invalid public listing payload')
    out = []
    for raw in rows[:250]:
        if not isinstance(raw, dict):
            continue
        url = raw.get('url') or ''
        parts = urlsplit(url)
        # Match the publisher's widget: private-posting URLs are not advertised.
        if (parts.scheme != 'https' or parts.username or parts.password or re.search('private_posting', url, re.I)
                or not raw.get('id') or not raw.get('title')):
            continue
        places = [raw.get('primaryLocation') or {}] + (raw.get('additionalLocations') or [])
        location = '; '.join(dict.fromkeys(p['descriptor'] for p in places if isinstance(p, dict) and p.get('descriptor')))
        text = _clean(html_lib.unescape(raw.get('jobDescription') or ''))
        if not text:
            continue
        out.append({'key': f"guardant:{raw['id']}", 'company': company['name'], 'title': raw['title'],
                    'url': url, 'location': location, 'remote': bool(re.search(r'\bremote\b', location, re.I)),
                    'text': text, 'published': raw.get('startDate') or '',
                    'employment': (raw.get('timeType') or {}).get('descriptor', ''), 'pay': pay_from_text(text),
                    'details_checked': True, 'location_verified': bool(location)})
    return out


async def fetch_guardant(company, public):
    page_url = 'https://www.guardanthealth.com/careers/jobs/'
    status, page = await public.text(page_url)
    match = re.search(r'var workdayApi\s*=\s*(\{.*?\});', page or '') if status == 200 else None
    if not match:
        return [], f'public widget configuration unavailable (HTTP {status})'
    try:
        config = json.loads(match[1])
        # This is a PUBLIC listing/search nonce openly embedded in the careers
        # page, not a private API key or login credential. Refresh it each scan.
        # The read-only WordPress listing action accepts GET as well as the
        # widget's POST. No application or other action is called.
        endpoint = 'https://www.guardanthealth.com/wp-admin/admin-ajax.php'
        if config.get('ajax_url') != endpoint or not re.fullmatch(r'[a-zA-Z0-9]+', config.get('nonce', '')):
            return [], 'unexpected public widget configuration'
        url = endpoint + '?' + urlencode({'action': 'get_workday_data', 'nonce': config['nonce']})
        status, data = await public.json(url)
        if status != 200:
            return [], f'public listing HTTP {status}'
        return guardant_jobs(data, company), None
    except (ValueError, TypeError, AttributeError):
        return [], 'invalid public listing payload'


def recruitee_jobs(payload, company):
    """Recruitee's public careers offers, including full requirements and locations."""
    if not isinstance(payload, dict) or not isinstance(payload.get('offers'), list):
        raise ValueError('invalid public offers')
    host = company['slug'] + '.recruitee.com'
    out = []
    for raw in payload['offers'][:250]:
        if not isinstance(raw, dict):
            continue
        url = raw.get('careers_url') or ''
        if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != host:
            continue
        title = raw.get('title') or ''
        text = _clean(html_lib.unescape((raw.get('description') or '') + ' ' + (raw.get('requirements') or '')))
        places = raw.get('locations') or []
        locations = [', '.join(str(p[k]).strip() for k in ('city', 'state', 'country') if p.get(k)) for p in places if isinstance(p, dict)]
        location = '; '.join(dict.fromkeys(filter(None, locations))) or raw.get('location', '')
        if title and text and raw.get('id'):
            out.append({'key': f"recruitee:{company['name']}:{raw['id']}", 'company': company['name'],
                        'title': title, 'url': url, 'location': location, 'remote': bool(raw.get('remote')),
                        'text': text, 'published': raw.get('published_at') or '',
                        'employment': str(raw.get('employment_type_code') or '').replace('_', ' '),
                        'pay': pay_from_text(text), 'details_checked': True, 'location_verified': bool(locations)})
    return out


async def fetch_recruitee(company, public):
    if not re.fullmatch(r'[a-z0-9-]+', company.get('slug', '')):
        return [], 'invalid public board name'
    status, data = await public.json(f"https://{company['slug']}.recruitee.com/api/offers/")
    if status != 200:
        return [], f'public offers HTTP {status}'
    try:
        return recruitee_jobs(data, company), None
    except (ValueError, TypeError):
        return [], 'invalid public offers'


def public_error(public, url, message):
    """Keep HTTP status and, for HTTP 0, the transport/policy failure category."""
    reason = getattr(public, 'failures', {}).get(url)
    return f"{message} ({reason})" if reason else message


async def fetch_paced_workday(company, cfg, public, now=None, seen_path=None):
    """Paced lists with configured page caps, then at most three details.

    The caller supplies a dedicated, robots-aware client and a 60s deadline.
    No retry after a 429, including responses carrying Retry-After: stop this
    scan and let the next daily scan try again with the smaller request load.
    """
    from jobs import fetch_workday, _html_text, pay_from_text, ListingCap
    last_url = None
    async def listing(url, body=None):
        nonlocal last_url
        last_url = url
        return await public.json(url, body)
    found, error = await fetch_workday(company, listing, now)
    if error and not isinstance(error, ListingCap):
        return found, public_error(public, last_url, error)
    candidates = [j for j in found if score_job(dict(j, tier=company.get('tier', 0)), cfg, now)]
    old = seen_keys(seen_path, [j['key'] for j in candidates]) if seen_path is not None else set()
    candidates = [j for j in candidates if j['key'] not in old]
    candidates.sort(key=lambda j: (-score_job(dict(j, tier=company.get('tier', 0)), cfg, now)[0], j['title']))
    base = f"https://{company['host']}/wday/cxs/{company['tenant']}/{company['site']}"
    for job in candidates[:3]:
        url = base + job['wd_path']
        status, data = await public.json(url)
        info = data.get('jobPostingInfo', {}) if isinstance(data, dict) else {}
        if status != 200 or not info.get('jobDescription'):
            return found, public_error(public, url, f'posting unavailable (HTTP {status})')
        job['text'] = _html_text(info['jobDescription'])
        job['employment'] = info.get('timeType', '')
        job['details_checked'] = True
        job['pay'] = pay_from_text(job['text'])
    return found, error
