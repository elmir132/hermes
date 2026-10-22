"""Reads Meta, Microsoft and Netflix job pages and Cerebral Valley's New York hackathon list in a real Chrome and writes browser_jobs.json.

Runs on your Mac, not on the server (the server is too small for a browser). Those three sites load their jobs
with JavaScript, so a plain request sees nothing. This opens each public search page the way a visitor would,
once per keyword, waits for it to load, and copies the visible titles and locations. It does not log in, hide
that it is automated, solve captchas or retry past a refusal: if a site returns an error or a block page,
that source is skipped and the reason is written to the file.

    ~/.venvs/hermes-browser/bin/python browser_reader.py            # writes ~/.hermes-browser/browser_jobs.json
    ~/.venvs/hermes-browser/bin/python browser_reader.py --only Meta

The bot reads the file (provider "browser" in jobs_config.json). Their terms may not allow automated reading,
so keep it to one run a day, for your own job search.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

OUT = Path.home() / ".hermes-browser" / "browser_jobs.json"
QUERIES = ["software engineer", "machine learning engineer", "new grad software engineer"]
LOAD_WAIT_MS = 7000
PAUSE_BETWEEN_PAGES_S = 4
BLOCK_WORDS = re.compile(r"(access denied|captcha|unusual traffic|verify you are (a )?human|too many requests|temporarily blocked)", re.I)
LOCATION_LINE = re.compile(r"^[A-Z][A-Za-z .'\-]+,\s*[A-Z]{2}\b|^Remote\b|United States|USA", re.I)
SEPARATORS = {"⋅", "·", "•", "|"}


def _lines(text: str) -> list:
    return [l.strip() for l in (text or "").splitlines() if l.strip() and l.strip() not in SEPARATORS]


# ---------- turning what the page showed into job records (pure functions, tested) ----------
def parse_meta_cards(cards: list) -> list:
    """cards: [{"text": innerText, "href": href or ""}] from the job cards on metacareers.com."""
    jobs, seen = [], set()
    for card in cards:
        lines = _lines(card.get("text", ""))
        if len(lines) < 2:
            continue
        title = lines[0]
        places = [l for l in lines[1:] if LOCATION_LINE.search(l)][:6]
        if not places:
            continue
        href = card.get("href", "") or ""
        if re.search(r"/(profile/job_details|jobs)/\d+", href):
            url = "https://www.metacareers.com" + href.split("?")[0] if href.startswith("/") else href.split("?")[0]
            identity = url                                               # two vacancies can share a title
        else:
            url = "https://www.metacareers.com/jobs?q=" + quote(title)         # the card has no direct link: search for its exact title
            identity = title
        if identity in seen:
            continue
        seen.add(identity)
        jobs.append({"title": title, "location": "; ".join(places), "url": url, "posted": ""})
    return jobs


def parse_eightfold_anchors(anchors: list, base: str) -> list:
    """anchors: [{"text": innerText, "href": href}] from an Eightfold careers page (Microsoft)."""
    jobs, seen = [], set()
    for a in anchors:
        href = (a.get("href") or "").split("?")[0]
        if not re.search(r"/careers/job/\d+", href) or href in seen:
            continue
        lines = _lines(a.get("text", ""))
        if len(lines) < 2:
            continue
        seen.add(href)
        posted = next((l for l in lines if l.lower().startswith("posted")), "")
        place = next((l for l in lines[1:] if l != posted and not l.lower().startswith(("remote", "hybrid", "onsite"))), "")
        jobs.append({"title": lines[0], "location": place, "url": base.rstrip("/") + href, "posted": posted})
    return jobs


def parse_netflix_cards(cards: list, search_url: str) -> list:
    jobs, seen = [], set()
    for card in cards:
        lines = _lines(card.get("text", ""))
        if len(lines) < 2 or lines[0] in seen:
            continue
        seen.add(lines[0])
        jobs.append({"title": lines[0], "location": lines[1], "url": search_url.replace("__Q__", quote(lines[0])), "posted": ""})
    return jobs


def parse_cerebral_cards(cards: list) -> list:
    """cards: [{"href": outbound event link, "card": text of the card}] from cerebralvalley.ai/events."""
    events, seen = [], set()
    for c in cards:
        href = (c.get("href") or "").split("?")[0]
        lines = _lines(c.get("card", ""))
        when = next((l for l in lines if " · " in l and re.search(r"\d{1,2}:\d{2}\s?[AP]M", l)), "")
        # layout: month, day, title, then the date line
        title = ""
        for i, l in enumerate(lines):
            if re.fullmatch(r"\d{1,2}", l) and i + 1 < len(lines):
                title = lines[i + 1]
                break
        if not href.startswith("http") or not title or not when or href in seen:
            continue
        seen.add(href)
        events.append({"name": title, "when": when, "url": href})
    return events


# ---------- the browser part ----------
SOURCES = {
    "Meta": {"url": "https://www.metacareers.com/jobs?q={q}&offices[0]=New%20York%2C%20NY"},
    "Microsoft": {"url": "https://apply.careers.microsoft.com/careers?query={q}&location=New%20York%2C%20NY%2C%20United%20States&sort_by=timestamp",
                  "base": "https://apply.careers.microsoft.com"},
    "Netflix": {"url": "https://explore.jobs.netflix.net/careers?query={q}&location=New%20York",
                "search": "https://explore.jobs.netflix.net/careers?query=__Q__&location=New%20York"},
}

JS_META = """() => [...document.querySelectorAll('a[role="link"]')].filter(a => (a.innerText||'').split('\\n').length >= 2)
    .slice(0, 40).map(a => ({text: a.innerText, href: a.getAttribute('href') || ''}))"""
JS_EIGHTFOLD = """() => [...document.querySelectorAll('a[href*="/careers/job/"]')].slice(0, 40)
    .map(a => ({text: a.innerText, href: a.getAttribute('href') || ''}))"""
JS_CEREBRAL = """() => [...document.querySelectorAll('a[href^="http"]')].filter(a => !/cerebralvalley\\.ai/.test(a.href))
    .map(a => ({href: a.href, card: a.parentElement && a.parentElement.parentElement ? a.parentElement.parentElement.innerText : ''}))"""
EVENT_SOURCES = {"Cerebral Valley": {"url": "https://cerebralvalley.ai/events?type=HACKATHON&locations=NYC"}}
JS_NETFLIX = """() => [...document.querySelectorAll('div.position-card')].slice(0, 40).map(e => ({text: e.innerText}))"""


def read_source(page, name: str, query: str):
    """Loads one search page and returns (jobs, error)."""
    cfg = SOURCES[name]
    resp = page.goto(cfg["url"].format(q=quote(query)), wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(LOAD_WAIT_MS)
    if resp is not None and resp.status >= 400:
        return [], f"HTTP {resp.status}"
    if BLOCK_WORDS.search(page.inner_text("body")[:3000]):
        return [], "the site showed a block or captcha page; stopped"
    if name == "Meta":
        return parse_meta_cards(page.evaluate(JS_META)), None
    if name == "Microsoft":
        return parse_eightfold_anchors(page.evaluate(JS_EIGHTFOLD), cfg["base"]), None
    return parse_netflix_cards(page.evaluate(JS_NETFLIX), cfg["search"]), None


def read_events(page, name: str):
    """Loads an events list page once and returns (events, error)."""
    resp = page.goto(EVENT_SOURCES[name]["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(LOAD_WAIT_MS + 3000)
    if resp is not None and resp.status >= 400:
        return [], f"HTTP {resp.status}"
    if BLOCK_WORDS.search(page.inner_text("body")[:3000]):
        return [], "the site showed a block or captcha page; stopped"
    return parse_cerebral_cards(page.evaluate(JS_CEREBRAL)), None


def write_snapshot(path, data):
    """Atomically publish a complete cache; readers never see a partial JSON file."""
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=1, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def run(only=None, out=OUT, queries=QUERIES):
    from playwright.sync_api import sync_playwright
    result = {"fetched_at": datetime.now(timezone.utc).isoformat(), "sources": {}, "events": {}}
    if only:                                                       # a partial run updates its part and keeps the rest of the file
        try:
            previous = json.loads(Path(out).read_text())
            result = {"fetched_at": previous.get("fetched_at", result["fetched_at"]), "sources": previous.get("sources", {}), "events": previous.get("events", {})}
        except (OSError, ValueError):
            pass
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1300, "height": 900})
        for name in SOURCES:
            if only and name.lower() != only.lower():
                continue
            jobs, error, seen = [], None, set()
            for query in queries:
                page = context.new_page()
                try:
                    found, error = read_source(page, name, query)
                except Exception as e:                        # a page that never loads is reported, not retried
                    found, error = [], f"{type(e).__name__}: {str(e)[:80]}"
                finally:
                    page.close()
                if error:
                    break                                      # a refusal or error ends this source for today
                for job in found:
                    if job["url"] + job["title"] not in seen:
                        seen.add(job["url"] + job["title"])
                        jobs.append(job)
                time.sleep(PAUSE_BETWEEN_PAGES_S)
            result["sources"][name] = {"jobs": jobs, "error": error, "fetched_at": datetime.now(timezone.utc).isoformat()}
        for name in EVENT_SOURCES:
            if only and name.lower() != only.lower().replace("-", " "):
                continue
            page = context.new_page()
            try:
                found, error = read_events(page, name)
            except Exception as e:
                found, error = [], f"{type(e).__name__}: {str(e)[:80]}"
            finally:
                page.close()
            result["events"][name] = {"events": found, "error": error}
        browser.close()
    write_snapshot(out, result)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read Meta, Microsoft and Netflix job pages in Chrome.")
    ap.add_argument("--only", help="one source: Meta, Microsoft, Netflix or Cerebral Valley")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)
    result = run(args.only, args.out)
    for name, src in result["sources"].items():
        print(f"{name}: {len(src['jobs'])} listings" + (f" (error: {src['error']})" if src["error"] else ""))
    for name, src in result["events"].items():
        print(f"{name}: {len(src['events'])} events" + (f" (error: {src['error']})" if src["error"] else ""))
    print("written to", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
