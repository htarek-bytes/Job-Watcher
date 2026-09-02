"""Derive ATS slugs from the SimplifyJobs feed instead of guessing them.

Hand-written slug lists rot silently and are mostly wrong: of 70 guessed
slugs, 5 appeared in the live feed, while the feed's own posting URLs yielded
359 slugs that are correct by construction. This module reads those URLs.

The point is not only coverage. Hitting Greenhouse directly sees a req the
moment it is live, whereas the same role reaches SimplifyJobs hours later, so
every slug discovered here also shortens time to alert for that company.
"""

import re

GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
WORKDAY = "workday"
SMARTRECRUITERS = "smartrecruiters"
BAMBOOHR = "bamboohr"
RIPPLING = "rippling"

# Every ATS whose board key can be recovered from a posting URL. Measured
# against the live feed: workday 371 boards over 927 postings, smartrecruiters
# 110 over 231, bamboohr 21 over 39, rippling 18 over 21.
SOURCES = (GREENHOUSE, LEVER, ASHBY, WORKDAY, SMARTRECRUITERS, BAMBOOHR, RIPPLING)

# Ordered: the first pattern that matches a URL wins.
PATTERNS = [
    # `for=` is not always the first query parameter, and many embed URLs carry
    # only a token with no recoverable slug at all.
    (GREENHOUSE, re.compile(r"boards\.greenhouse\.io/embed/job_app\?[^\s]*[?&]for=([a-z0-9_-]+)", re.I)),
    (GREENHOUSE, re.compile(r"(?:job-)?boards\.greenhouse\.io/([a-z0-9_-]+)", re.I)),
    (LEVER, re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    (ASHBY, re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    # Workday keys are three parts: tenant, data centre, site. All three are in
    # the URL, and the fetcher wants them as "tenant.dc/site". An optional
    # locale segment sits between the host and the site name.
    (WORKDAY, re.compile(
        r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
        r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
    (SMARTRECRUITERS, re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    (BAMBOOHR, re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com", re.I)),
    (RIPPLING, re.compile(r"ats\.rippling\.com/([a-z0-9-]+)", re.I)),
]

# Path segments that are never a company slug.
NOISE = {
    "embed", "job_app", "jobs", "job", "careers", "search", "www", "api",
    "applications", "application", "apply",
}


def slug_from_url(url):
    """Return (source, slug) for an ATS posting URL, or None."""
    if not url:
        return None
    for source, pattern in PATTERNS:
        found = pattern.search(url)
        if not found:
            continue
        if source == WORKDAY:
            tenant, dc, site = found.group(1), found.group(2), found.group(3)
            if not tenant or not site or site.lower() in NOISE:
                continue
            return source, "%s.%s/%s" % (tenant.lower(), dc.lower(), site)

        slug = found.group(1).strip("-._")
        # SmartRecruiters company identifiers are case sensitive in its API.
        if source != SMARTRECRUITERS:
            slug = slug.lower()
        if not slug or slug in NOISE or len(slug) < 2:
            # Fall through: an earlier pattern matching a noise segment must
            # not stop a later, more specific pattern from matching.
            continue
        return source, slug
    return None


def discover(records):
    """Map ATS -> {board key: count} from raw SimplifyJobs listing records."""
    out = {name: {} for name in SOURCES}
    for record in records:
        found = slug_from_url(record.get("url"))
        if not found:
            continue
        source, slug = found
        out[source][slug] = out[source].get(slug, 0) + 1
    return out


def merge(known, found, now):
    """Fold a discovery pass into the stored slug registry.

    Slugs are never dropped automatically. A company with no new grad req today
    still has a working board, and deleting it would mean rediscovering it only
    once it posts -- exactly the moment being early matters. Pruning is a
    decision for `verify`, which can tell a dead board from a quiet one.
    """
    registry = dict(known or {})
    for source, slugs in found.items():
        block = dict(registry.get(source, {}))
        for slug, count in slugs.items():
            entry = dict(block.get(slug, {}))
            entry["last_seen"] = now
            entry["postings"] = count
            entry.setdefault("first_seen", now)
            entry.setdefault("origin", "discovered")
            block[slug] = entry
        registry[source] = block
    return registry


def seed_from_config(registry, cfg, now):
    """Record the hand-written config slugs alongside discovered ones.

    They are kept because a guessed slug can still be a real board that simply
    has nothing indexed right now -- `openai` and `anthropic` on Ashby are the
    obvious cases. They are marked so verify can report on them separately.
    """
    registry = dict(registry or {})
    for source in SOURCES:
        block = dict(registry.get(source, {}))
        key = "boards" if source == WORKDAY else "slugs"
        for slug in cfg.get("sources", {}).get(source, {}).get(key, []):
            slug = slug if source in (WORKDAY, SMARTRECRUITERS) else slug.lower()
            entry = dict(block.get(slug, {}))
            entry.setdefault("first_seen", now)
            entry.setdefault("origin", "config")
            block[slug] = entry
        registry[source] = block
    return registry


def all_slugs(registry, source):
    return sorted((registry or {}).get(source, {}).keys())
