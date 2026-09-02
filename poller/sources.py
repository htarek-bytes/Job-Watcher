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


def iter_configured(cfg, registry=None):
    """Yield (source, key) for everything enabled.

    `registry` is the discovered-slug store. Discovered slugs are unioned with
    the hand-written ones, which is how the 70 guesses became ~360 real boards.
    """
    src = cfg.get("sources", {})
    import discover as _discover

    # Every ATS whose board key can be recovered from a posting URL. The
    # configured entries and the discovered ones are unioned; discovery is
    # where almost all of them come from.
    for name in _discover.SOURCES:
        block = src.get(name, {})
        if not block.get("enabled", True):
            continue
        key = "boards" if name == WORKDAY else "slugs"
        configured = block.get(key, [])
        if name in (WORKDAY, SMARTRECRUITERS):
            boards = set(configured)
        else:
            boards = {b.lower() for b in configured}
        if registry:
            boards |= set(_discover.all_slugs(registry, name))
        for board in sorted(boards):
            yield name, board

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
    if source == WORKDAY:
        return fetch_workday(key, etag)
    if source == SMARTRECRUITERS:
        return fetch_smartrecruiters(key, etag)
    if source == BAMBOOHR:
        return fetch_bamboohr(key, etag)
    if source == RIPPLING:
        return fetch_rippling(key, etag)
    if source == SIMPLIFY:
        return fetch_simplify(cfg.get("sources", {}).get(SIMPLIFY, {}), etag)
    raise ValueError("unknown source %r" % source)


# --------------------------------------------------------------------------
# Workday. The single largest platform in the feed (27.8% of active postings)
# and previously unsupported, which is where most of the missing coverage was.
#
# Every Workday careers site exposes the same JSON endpoint behind the UI:
#   POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
# It wants a JSON body and answers with a page of postings. There is no public
# documentation for it, so it is treated like Amazon: expected to break.
# --------------------------------------------------------------------------

WORKDAY = "workday"
SMARTRECRUITERS = "smartrecruiters"


def fetch_workday(spec, etag=None):
    """`spec` is "tenant/site" or "tenant.dc/site" from config."""
    res = SourceResult(WORKDAY, spec)
    try:
        host, _, site = spec.partition("/")
        tenant = host.split(".")[0]
        dc = host.split(".")[1] if "." in host else "wd1"
        if not tenant or not site:
            res.error = "bad workday spec %r, want tenant[.dc]/site" % spec
            return res

        url = "https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s/jobs" % (
            tenant, dc, tenant, site,
        )
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0,
                           "searchText": ""}).encode("utf-8")
        resp = _http.post(url, body, headers={"Content-Type": "application/json"})
        res.status, res.seconds = resp.status, resp.seconds
        if not resp.ok:
            res.error = resp.error
            return res

        payload = resp.json() or {}
        for item in payload.get("jobPostings", []):
            path = item.get("externalPath") or ""
            res.jobs.append(_job(
                WORKDAY, spec, path.rsplit("/", 1)[-1] or item.get("bulletFields", [""])[0],
                company=tenant,
                title=item.get("title", ""),
                url="https://%s.%s.myworkdayjobs.com/%s%s" % (tenant, dc, site, path),
                locations=[item.get("locationsText")],
                # postedOn is relative prose ("Posted 3 Days Ago"), not a date.
                posted_at=_workday_posted(item.get("postedOn")),
                description="",
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "isolated failure: %s: %s" % (type(exc).__name__, exc)
    return res


_WD_AGE = re.compile(r"(\d+)\+?\s+(day|hour|minute|month)", re.I)


def _workday_posted(text):
    """Workday reports "Posted 3 Days Ago", never a timestamp.

    Converting prose to an epoch is lossy, so the result is deliberately coarse
    and "Posted Today" resolves to now rather than pretending to a minute.
    """
    if not text:
        return None
    if re.search(r"today|just posted", text, re.I):
        return int(time.time())
    found = _WD_AGE.search(text)
    if not found:
        return None
    amount = int(found.group(1))
    unit = found.group(2).lower()
    seconds = {"minute": 60, "hour": 3600, "day": 86400, "month": 2592000}[unit]
    return int(time.time() - amount * seconds)


# --------------------------------------------------------------------------
# SmartRecruiters. 6.9% of the feed, and the cleanest public API of the lot.
# --------------------------------------------------------------------------

def fetch_smartrecruiters(slug, etag=None):
    res = SourceResult(SMARTRECRUITERS, slug)
    url = ("https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100"
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
        for item in payload.get("content", []):
            loc = item.get("location") or {}
            city = ", ".join(
                str(part) for part in (loc.get("city"), loc.get("region"), loc.get("country"))
                if part
            )
            res.jobs.append(_job(
                SMARTRECRUITERS, slug, item.get("id"),
                company=(item.get("company") or {}).get("name") or slug,
                title=item.get("name", ""),
                url=item.get("applyUrl") or item.get("ref", ""),
                locations=[city, "Remote" if loc.get("remote") else ""],
                posted_at=_epoch(item.get("releasedDate")),
                description="",
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


# --------------------------------------------------------------------------
# BambooHR and Rippling. Small platforms, but both expose a plain public JSON
# endpoint and both are discoverable from posting URLs, so they cost little.
# Neither has been exercised against a live board yet.
# --------------------------------------------------------------------------

BAMBOOHR = "bamboohr"
RIPPLING = "rippling"


def fetch_bamboohr(slug, etag=None):
    res = SourceResult(BAMBOOHR, slug)
    url = "https://%s.bamboohr.com/careers/list" % urllib.parse.quote(slug)
    resp = _http.get(url, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    try:
        for item in (resp.json() or {}).get("result", []):
            loc = item.get("location") or {}
            where = ", ".join(
                str(p) for p in (loc.get("city"), loc.get("state"), loc.get("country"))
                if p
            )
            res.jobs.append(_job(
                BAMBOOHR, slug, item.get("id"),
                company=slug,
                title=item.get("jobOpeningName", ""),
                url="https://%s.bamboohr.com/careers/%s" % (slug, item.get("id")),
                locations=[where, "Remote" if item.get("isRemote") else ""],
                posted_at=_epoch(item.get("datePosted")),
                description="",
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


def fetch_rippling(slug, etag=None):
    res = SourceResult(RIPPLING, slug)
    url = ("https://api.rippling.com/platform/api/ats/v1/board/%s/jobs"
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
        payload = resp.json() or []
        items = payload if isinstance(payload, list) else payload.get("items", [])
        for item in items:
            res.jobs.append(_job(
                RIPPLING, slug, item.get("uuid") or item.get("id"),
                company=slug,
                title=item.get("name") or item.get("title", ""),
                url=item.get("url") or ("https://ats.rippling.com/%s/jobs/%s"
                                        % (slug, item.get("uuid"))),
                locations=[(item.get("workLocation") or {}).get("label")],
                posted_at=_epoch(item.get("createdAt") or item.get("publishedAt")),
                description="",
            ))
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res
