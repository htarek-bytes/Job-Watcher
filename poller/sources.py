"""Source fetchers.

Every fetcher returns a SourceResult and never raises. A sweep calls all of
them; one failing source degrades coverage, it does not end the run. Amazon in
particular is an unofficial, undocumented endpoint and is treated as expected
to break.
"""

import html
import json
import os
import re
import time
import urllib.parse

import net as _http

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
AMAZON = "amazon"
SIMPLIFY = "simplify"

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    return _WS.sub(" ", _TAGS.sub(" ", text)).strip()


class SourceResult:
    def __init__(self, source, slug):
        self.source = source
        self.slug = slug
        self.jobs = []
        self.ok = False
        self.error = None
        self.status = 0
        self.seconds = 0.0
        self.not_modified = False

    @property
    def count(self):
        return len(self.jobs)


def _job(source, slug, uid, company, title, url, locations, posted_at=None,
         description=""):
    return {
        "uid": "%s:%s:%s" % (source, slug, uid),
        "source": source,
        "slug": slug,
        "company": company,
        "title": title,
        "url": url,
        "locations": [loc for loc in (locations or []) if loc],
        "posted_at": posted_at,
        "description": description,
    }


def _epoch(value):
    """Best effort timestamp -> epoch seconds. Returns None rather than lying."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        # Milliseconds if it is implausibly large for seconds.
        return int(value / 1000) if value > 10_000_000_000 else int(value)
    text = str(value).strip()
    if text.isdigit():
        return _epoch(int(text))
    text = text.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d"):
        try:
            import datetime
            dt = datetime.datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------

def fetch_greenhouse(slug, etag=None):
    res = SourceResult(GREENHOUSE, slug)
    url = ("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true"
           % urllib.parse.quote(slug))
    resp = _http.get(url, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    try:
        payload = resp.json() or {}
        for item in payload.get("jobs", []):
            loc = (item.get("location") or {}).get("name")
            offices = [o.get("name") for o in item.get("offices", []) or []]
            res.jobs.append(_job(
                GREENHOUSE, slug, item.get("id"),
                company=slug,
                title=item.get("title", ""),
                url=item.get("absolute_url", ""),
                locations=[loc] + offices,
                posted_at=_epoch(item.get("first_published") or item.get("updated_at")),
                description=strip_html(item.get("content", "")),
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------

def fetch_lever(slug, etag=None):
    res = SourceResult(LEVER, slug)
    url = "https://api.lever.co/v0/postings/%s?mode=json" % urllib.parse.quote(slug)
    resp = _http.get(url, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    try:
        payload = resp.json() or []
        for item in payload:
            cats = item.get("categories") or {}
            locs = [cats.get("location")] + list(item.get("additionalPlain", []) or [])
            body = item.get("descriptionPlain") or strip_html(item.get("description", ""))
            lists = " ".join(
                strip_html(section.get("content", ""))
                for section in (item.get("lists") or [])
            )
            res.jobs.append(_job(
                LEVER, slug, item.get("id"),
                company=slug,
                title=item.get("text", ""),
                url=item.get("hostedUrl") or item.get("applyUrl", ""),
                locations=locs,
                posted_at=_epoch(item.get("createdAt")),
                description=(body + " " + lists).strip(),
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------

def fetch_ashby(slug, etag=None):
    res = SourceResult(ASHBY, slug)
    url = ("https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true"
           % urllib.parse.quote(slug))
    resp = _http.get(url, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    try:
        payload = resp.json() or {}
        for item in payload.get("jobs", []):
            res.jobs.append(_job(
                ASHBY, slug, item.get("id"),
                company=payload.get("name") or slug,
                title=item.get("title", ""),
                url=item.get("jobUrl") or item.get("applyUrl", ""),
                locations=[item.get("location")] + [
                    a.get("location")
                    for a in (item.get("secondaryLocations") or [])
                ],
                posted_at=_epoch(item.get("publishedAt") or item.get("updatedAt")),
                description=strip_html(
                    item.get("descriptionHtml") or item.get("descriptionPlain", "")
                ),
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


# --------------------------------------------------------------------------
# Amazon. Unofficial, undocumented, and the first thing expected to break.
# Isolated behind its own try/except so a failure cannot take down a sweep.
# --------------------------------------------------------------------------

def fetch_amazon(query, etag=None):
    res = SourceResult(AMAZON, query)
    try:
        params = urllib.parse.urlencode({
            "base_query": query,
            "result_limit": 100,
            "sort": "recent",
            "country": "USA",
        })
        resp = _http.get("https://www.amazon.jobs/en/search.json?" + params)
        res.status, res.seconds = resp.status, resp.seconds
        if not resp.ok:
            res.error = resp.error
            return res
        payload = resp.json() or {}
        for item in payload.get("jobs", []):
            path = item.get("job_path") or ""
            res.jobs.append(_job(
                AMAZON, "amazon", item.get("id_icims") or item.get("id"),
                company="Amazon",
                title=item.get("title", ""),
                url=("https://www.amazon.jobs" + path) if path else "",
                locations=[item.get("normalized_location") or item.get("location")],
                posted_at=_epoch(item.get("posted_date") or item.get("updated_time")),
                description=strip_html(
                    (item.get("description") or "") + " " +
                    (item.get("basic_qualifications") or "")
                ),
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "isolated failure: %s: %s" % (type(exc).__name__, exc)
    return res


# --------------------------------------------------------------------------
# SimplifyJobs/New-Grad-Positions
#
# Path confirmed by reading the repository, not guessed:
#   .github/scripts/listings.json on branch `dev`, ~13 MB, ~19k records.
# Fetched with a conditional GET so the payload only crosses the wire when it
# actually changed.
#
# Note: the feed carries its own `sponsorship` field. It is close to useless --
# on inspection 3229 of 3240 active rows said "Other" -- so it is recorded but
# never trusted as a work authorization signal.
# --------------------------------------------------------------------------

def fetch_simplify(cfg_source, etag=None):
    repo = cfg_source.get("repo", "SimplifyJobs/New-Grad-Positions")
    ref = cfg_source.get("ref", "dev")
    path = cfg_source.get("path", ".github/scripts/listings.json")
    res = SourceResult(SIMPLIFY, repo)

    url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, ref, path)
    # Deliberately unauthenticated. raw.githubusercontent serves public files
    # anonymously, and sending an Authorization header it does not accept turns
    # a working request into a 404 -- which reads as "the path moved" and sends
    # you hunting for a problem that is not there.
    resp = _http.get(url, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    res.etag = resp.etag
    try:
        for item in resp.json() or []:
            if not item.get("active") or not item.get("is_visible", True):
                continue
            res.jobs.append(_job(
                SIMPLIFY, "listings", item.get("id"),
                company=item.get("company_name", ""),
                title=item.get("title", ""),
                url=item.get("url", ""),
                locations=item.get("locations") or [],
                posted_at=_epoch(item.get("date_posted")),
                description="",
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


def iter_configured(cfg):
    """Yield (source, key) pairs for everything enabled in config."""
    src = cfg.get("sources", {})
    for name, fetch_key in ((GREENHOUSE, "slugs"), (LEVER, "slugs"), (ASHBY, "slugs")):
        block = src.get(name, {})
        if block.get("enabled", True):
            for slug in block.get(fetch_key, []):
                yield name, slug
    if src.get(AMAZON, {}).get("enabled", True):
        for query in src.get(AMAZON, {}).get("queries", []):
            yield AMAZON, query
    if src.get(SIMPLIFY, {}).get("enabled", True):
        yield SIMPLIFY, src.get(SIMPLIFY, {}).get("repo", "SimplifyJobs/New-Grad-Positions")


def fetch(source, key, cfg, etag=None):
    if source == GREENHOUSE:
        return fetch_greenhouse(key, etag)
    if source == LEVER:
        return fetch_lever(key, etag)
    if source == ASHBY:
        return fetch_ashby(key, etag)
    if source == AMAZON:
        return fetch_amazon(key, etag)
    if source == SIMPLIFY:
        return fetch_simplify(cfg.get("sources", {}).get(SIMPLIFY, {}), etag)
    raise ValueError("unknown source %r" % source)
