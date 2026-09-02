"""Canadian job sources.

The feed was 496 US roles against 24 Canadian ones. That is not a filter
problem, it is a structural one. Every source in sources.py is an applicant
tracking system keyed by a company slug, and the slug registry is mined from
SimplifyJobs, which is a US new grad aggregator. So the pipeline had no way to
learn that a 150 person company in Waterloo exists at all.

The sources here are national aggregators instead of per company boards: one
endpoint covering thousands of employers. That is the only shape that closes a
coverage gap rather than adding one more company to it.

Two of them are also discovery feeds. Job Bank and Eluta link out to the
employer's own careers page, so their results feed discover.slug_from_url and
turn into Greenhouse, Lever and Workable boards the US aggregator never
mentions. Canadian coverage then compounds instead of staying flat.

None of these publish an API contract. Every endpoint below is a CANDIDATE
until `python poller/cli.py probe` has run it from a machine with real egress.
Each fetcher therefore tries several shapes and reports which one answered,
and every parser falls back to schema.org JSON-LD, which these sites emit for
search engines and which changes far less often than their markup.
"""

import json
import re
import urllib.parse

import net as _http
import sources

JOBBANK = "jobbank"
JOBILLICO = "jobillico"
TALENTEGG = "talentegg"
ELUTA = "eluta"
GETRO = "getro"

# Sources that answer a national query rather than a single company board.
SOURCES = (JOBBANK, JOBILLICO, TALENTEGG, ELUTA, GETRO)

# Sources whose results carry a link to the employer's own ATS, so their
# postings are worth running discovery over.
DISCOVERY_SOURCES = (JOBBANK, ELUTA, GETRO)

_BROWSER = {
    # These are HTML pages meant for a browser. Asking for application/json,
    # which net.get does by default, gets a 406 from some of them.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.8",
}


# --------------------------------------------------------------------------
# JSON-LD. Every one of these sites emits schema.org JobPosting for search
# engines, and that markup outlives the CSS classes around it, so it is tried
# before any site specific parsing.
# --------------------------------------------------------------------------

_LD_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def _json_ld(body):
    """Yield every schema.org JobPosting object on a page."""
    for block in _LD_BLOCK.findall(body or ""):
        try:
            payload = json.loads(block.strip())
        except Exception:  # noqa: BLE001 - one bad block must not lose the rest
            continue
        for node in _walk_ld(payload):
            if str(node.get("@type", "")).lower() == "jobposting":
                yield node


def _walk_ld(node):
    """Flatten @graph and ItemList wrappers, which these sites both use."""
    if isinstance(node, list):
        for item in node:
            yield from _walk_ld(item)
        return
    if not isinstance(node, dict):
        return
    yield node
    for key in ("@graph", "itemListElement", "item", "mainEntity"):
        if key in node:
            yield from _walk_ld(node[key])


def _ld_location(node):
    """schema.org jobLocation, which is nested three deep and often a list."""
    out = []
    loc = node.get("jobLocation")
    for entry in (loc if isinstance(loc, list) else [loc]):
        if not isinstance(entry, dict):
            if entry:
                out.append(str(entry))
            continue
        addr = entry.get("address")
        if isinstance(addr, dict):
            out.append(", ".join(
                str(addr.get(k)) for k in
                ("addressLocality", "addressRegion", "addressCountry")
                if addr.get(k)
            ))
        elif addr:
            out.append(str(addr))
        elif entry.get("name"):
            out.append(str(entry["name"]))
    if str(node.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        out.append("Remote")
    return [o for o in out if o]


def _ld_org(node):
    org = node.get("hiringOrganization")
    if isinstance(org, dict):
        return org.get("name") or ""
    return str(org or "")


def _from_json_ld(source, slug, body, base=""):
    """Build jobs from a page's JSON-LD. Returns [] when there is none."""
    out = []
    for node in _json_ld(body):
        url = node.get("url") or node.get("sameAs") or ""
        if url and base and url.startswith("/"):
            url = urllib.parse.urljoin(base, url)
        title = sources.strip_html(str(node.get("title") or ""))
        if not title:
            continue
        out.append(sources._job(
            source, slug,
            node.get("identifier") if isinstance(node.get("identifier"), (str, int))
            else (url or title),
            company=sources.strip_html(_ld_org(node)),
            title=title,
            url=url,
            locations=_ld_location(node),
            posted_at=sources._epoch(node.get("datePosted")),
            description=sources.strip_html(str(node.get("description") or ""))[:4000],
        ))
    return out


def _dedupe_by_url(jobs):
    seen, out = set(), []
    for job in jobs:
        key = job.get("url") or job.get("uid")
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


# --------------------------------------------------------------------------
# Job Bank Canada. The federal government's board, and the reason this module
# exists. Practically every Canadian employer that advertises publicly ends up
# here, including the small firms that are on no aggregator at all, and every
# LMIA backed posting is required to be here. It is also the one Canadian
# source with no commercial incentive to block a reader.
#
# Its results link to the employer's own application page, so these postings
# are run through discovery as well as matched directly.
# --------------------------------------------------------------------------

JOBBANK_BASE = "https://www.jobbank.gc.ca"

# `sort=M` is most recent first, which is the whole point at a one minute
# cadence. Deliberately nothing else: every extra query parameter is another
# guess that can turn a working request into an empty page, and the probe can
# only tell one guess from another if there is one of them.
JOBBANK_SEARCH = JOBBANK_BASE + "/jobsearch/jobsearch?searchstring=%s&sort=M"

_JB_ARTICLE = re.compile(r"<article\b.*?</article>", re.S | re.I)
_JB_ID = re.compile(r'id=["\']article-(\d+)', re.I)
_JB_HREF = re.compile(r'href=["\'](/jobsearch/jobposting/[^"\'?#]+)', re.I)
_JB_TITLE = re.compile(r'class=["\'][^"\']*noc-title[^"\']*["\'][^>]*>(.*?)<', re.S | re.I)
_JB_BUSINESS = re.compile(r'class=["\'][^"\']*business[^"\']*["\'][^>]*>(.*?)<', re.S | re.I)
_JB_LOCATION = re.compile(r'class=["\'][^"\']*location[^"\']*["\'][^>]*>(.*?)<', re.S | re.I)
_JB_DATE = re.compile(
    r'class=["\'][^"\']*date[^"\']*["\'][^>]*>(?:\s*<[^>]+>)*\s*([^<]+)', re.S | re.I)


def _clean(text):
    return sources.strip_html(text or "").strip(" \t\r\n-|")


def fetch_jobbank(query, etag=None):
    res = sources.SourceResult(JOBBANK, query)
    url = JOBBANK_SEARCH % urllib.parse.quote_plus(query)
    resp = _http.get(url, headers=_BROWSER, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    try:
        res.jobs = _parse_jobbank(query, resp.body)
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


def _parse_jobbank(query, body):
    """JSON-LD first, then the results list markup.

    Kept separate from the fetch so the probe command can run it over a saved
    page and so it is testable without egress, which this sandbox has none of.
    """
    jobs = _from_json_ld(JOBBANK, query, body, base=JOBBANK_BASE)
    if jobs:
        return _dedupe_by_url(jobs)

    for block in _JB_ARTICLE.findall(body or ""):
        href = _JB_HREF.search(block)
        title = _JB_TITLE.search(block)
        if not href or not title:
            continue
        ident = _JB_ID.search(block)
        date = _JB_DATE.search(block)
        jobs.append(sources._job(
            JOBBANK, query,
            ident.group(1) if ident else href.group(1).rsplit("/", 1)[-1],
            company=_clean(_JB_BUSINESS.search(block).group(1))
            if _JB_BUSINESS.search(block) else "",
            title=_clean(title.group(1)),
            url=urllib.parse.urljoin(JOBBANK_BASE, href.group(1)),
            locations=[_clean(_JB_LOCATION.search(block).group(1))]
            if _JB_LOCATION.search(block) else ["Canada"],
            posted_at=_jobbank_date(date.group(1) if date else None),
            description="",
        ))
    return _dedupe_by_url(jobs)


_JB_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12,
}

_JB_DATE_RE = re.compile(r"(\d{1,2})?\s*([A-Za-zéûô]+)\s+(\d{1,2})?,?\s*(\d{4})")


def _jobbank_date(text):
    """Job Bank prints "September 2, 2026", and "2 septembre 2026" in French."""
    if not text:
        return None
    found = _JB_DATE_RE.search(text.strip())
    if not found:
        return None
    month = _JB_MONTHS.get(found.group(2).lower())
    if not month:
        return None
    day = found.group(1) or found.group(3) or "1"
    return sources._epoch("%s-%02d-%02d" % (found.group(4), month, int(day)))


# --------------------------------------------------------------------------
# Jobillico. Quebec's largest board and effectively invisible outside it.
# A Montreal company with 200 people posts here and on nothing else.
# --------------------------------------------------------------------------

JOBILLICO_BASE = "https://www.jobillico.com"
JOBILLICO_SEARCH = JOBILLICO_BASE + "/en/job-search?skwd=%s&page=1"

_JI_LINK = re.compile(
    r'href=["\'](/en/job-offer/[^"\'?#]+)["\'][^>]*>(?:\s*<[^>]+>)*\s*([^<]{3,120})',
    re.I)


def fetch_jobillico(query, etag=None):
    res = sources.SourceResult(JOBILLICO, query)
    resp = _http.get(JOBILLICO_SEARCH % urllib.parse.quote_plus(query),
                     headers=_BROWSER, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    try:
        res.jobs = _parse_jobillico(query, resp.body)
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


def _parse_jobillico(query, body):
    jobs = _from_json_ld(JOBILLICO, query, body, base=JOBILLICO_BASE)
    if jobs:
        return _dedupe_by_url(jobs)
    for path, title in _JI_LINK.findall(body or ""):
        title = _clean(title)
        if not title:
            continue
        jobs.append(sources._job(
            JOBILLICO, query, path.rsplit("/", 1)[-1],
            company="", title=title,
            url=urllib.parse.urljoin(JOBILLICO_BASE, path),
            # Jobillico is Quebec only, so an unparsed location is still CA.
            locations=["Quebec, Canada"],
            posted_at=None, description="",
        ))
    return _dedupe_by_url(jobs)


# --------------------------------------------------------------------------
# TalentEgg. A Canadian board that is new grad and campus only, which makes it
# the highest signal to noise source in this file: almost everything on it
# already passes the matcher's fourth gate.
# --------------------------------------------------------------------------

TALENTEGG_BASE = "https://talentegg.ca"
TALENTEGG_SEARCH = TALENTEGG_BASE + "/search/jobs/?q=%s"

_TE_LINK = re.compile(
    r'href=["\']((?:https://talentegg\.ca)?/(?:job|internship)/[^"\'?#]+)["\']'
    r'[^>]*>(?:\s*<[^>]+>)*\s*([^<]{3,120})', re.I)


def fetch_talentegg(query, etag=None):
    res = sources.SourceResult(TALENTEGG, query)
    resp = _http.get(TALENTEGG_SEARCH % urllib.parse.quote_plus(query),
                     headers=_BROWSER, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    try:
        res.jobs = _parse_talentegg(query, resp.body)
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


def _parse_talentegg(query, body):
    jobs = _from_json_ld(TALENTEGG, query, body, base=TALENTEGG_BASE)
    if jobs:
        return _dedupe_by_url(jobs)
    for path, title in _TE_LINK.findall(body or ""):
        title = _clean(title)
        if not title:
            continue
        jobs.append(sources._job(
            TALENTEGG, query, path.rstrip("/").rsplit("/", 1)[-1],
            company="", title=title,
            url=urllib.parse.urljoin(TALENTEGG_BASE, path),
            locations=["Canada"], posted_at=None, description="",
        ))
    return _dedupe_by_url(jobs)


# --------------------------------------------------------------------------
# Eluta. It indexes Canadian employers' own careers pages directly rather than
# reselling postings, so its links point at Greenhouse, Lever and Workable
# boards. That makes it worth more as a discovery feed than as a job source:
# every hit is a chance to learn a Canadian ATS slug the US aggregator has
# never heard of, and once learned the board is polled directly from then on.
# --------------------------------------------------------------------------

ELUTA_BASE = "https://www.eluta.ca"
ELUTA_SEARCH = ELUTA_BASE + "/search?q=%s&l=Canada&sort=date"

_EL_LINK = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.I)
_EL_SELF = re.compile(r"eluta\.ca|google|facebook|twitter|linkedin", re.I)


def fetch_eluta(query, etag=None):
    res = sources.SourceResult(ELUTA, query)
    resp = _http.get(ELUTA_SEARCH % urllib.parse.quote_plus(query),
                     headers=_BROWSER, etag=etag)
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    if resp.not_modified:
        res.ok, res.not_modified = True, True
        return res
    try:
        res.jobs = _parse_eluta(query, resp.body)
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


def _parse_eluta(query, body):
    """Eluta's value is its outbound links, so keep only the ones that name an
    ATS discovery already understands. Anything else is a careers page this
    tool cannot poll and would only be a dead row in the feed."""
    import discover

    jobs = _from_json_ld(ELUTA, query, body, base=ELUTA_BASE)
    if jobs:
        return _dedupe_by_url(jobs)

    for url in _EL_LINK.findall(body or ""):
        if _EL_SELF.search(url) or not discover.slug_from_url(url):
            continue
        jobs.append(sources._job(
            ELUTA, query, url, company="", title="",
            url=url, locations=["Canada"], posted_at=None, description="",
        ))
    return _dedupe_by_url(jobs)


# --------------------------------------------------------------------------
# Getro. It powers the talent networks that venture funds and accelerators run
# for their portfolios, and the Canadian ones (Inovia, Real, Golden, BDC,
# Georgian, OMERS, Radical, Panache) are a directory of exactly the employer
# the user asked for: a hundred to three hundred people, real engineering, and
# on no aggregator anywhere.
#
# A network is addressed by numeric id, which is only visible in the board's
# own page source, so ids are configured rather than guessed. Its records link
# to the portfolio company's ATS, so this is a discovery feed too.
# --------------------------------------------------------------------------

GETRO_API = "https://api.getro.com/api/v2/collections/%s/search/jobs"


def fetch_getro(collection, etag=None):
    res = sources.SourceResult(GETRO, collection)
    body = json.dumps({"hitsPerPage": 100, "page": 0, "filters": {}}).encode("utf-8")
    resp = _http.post(GETRO_API % urllib.parse.quote(str(collection)), body,
                      headers={"Content-Type": "application/json"})
    res.status, res.seconds = resp.status, resp.seconds
    if not resp.ok:
        res.error = resp.error
        return res
    try:
        res.jobs = _parse_getro(collection, resp.json() or {})
        res.ok = True
    except Exception as exc:  # noqa: BLE001
        res.error = "parse: %s" % exc
    return res


def _parse_getro(collection, payload):
    """Getro publishes no contract, so every field is read through a list of
    candidate names rather than one assumed key."""
    items = (payload.get("results", {}).get("jobs")
             if isinstance(payload.get("results"), dict) else None)
    if items is None:
        items = payload.get("jobs") or payload.get("hits") or []

    jobs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        org = item.get("organization") or item.get("company") or {}
        if not isinstance(org, dict):
            org = {"name": str(org)}
        locs = item.get("locations") or item.get("location") or []
        if isinstance(locs, (str, dict)):
            locs = [locs]
        flat = []
        for loc in locs:
            if isinstance(loc, dict):
                flat.append(", ".join(
                    str(loc.get(k)) for k in ("city", "region", "country")
                    if loc.get(k)) or str(loc.get("name") or ""))
            elif loc:
                flat.append(str(loc))
        jobs.append(sources._job(
            GETRO, str(collection),
            item.get("id") or item.get("slug") or item.get("url"),
            company=org.get("name") or "",
            title=item.get("title") or item.get("name") or "",
            url=item.get("url") or item.get("apply_url") or "",
            locations=flat,
            posted_at=sources._epoch(
                item.get("created_at") or item.get("posted_at")
                or item.get("published_at")),
            description="",
        ))
    return _dedupe_by_url([j for j in jobs if j["title"]])


# --------------------------------------------------------------------------

_FETCHERS = {
    JOBBANK: fetch_jobbank,
    JOBILLICO: fetch_jobillico,
    TALENTEGG: fetch_talentegg,
    ELUTA: fetch_eluta,
    GETRO: fetch_getro,
}


def fetch(source, key, etag=None):
    return _FETCHERS[source](key, etag)


def iter_configured(cfg):
    """Yield (source, key) for every enabled Canadian source.

    A key is a search term, or a network id for Getro. These are national
    queries, not company boards, so they are always polled rather than
    rotated: there are a handful of them, and they are the only thing standing
    between the feed and being 95% American.
    """
    src = cfg.get("sources", {})
    for name in SOURCES:
        block = src.get(name, {})
        if not block.get("enabled", False):
            continue
        for key in block.get("queries", []) or block.get("collections", []):
            yield name, str(key)
