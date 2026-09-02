"""Dedupe and ranking.

Two jobs here. Collapse the same role arriving from several places, and put a
number on which roles are worth the next twenty minutes.

Measured on the live feed: 79 duplicate rows across 49 groups, one staffing
firm posting an identical role twenty times.
"""

import hashlib
import re
import time

import workauth

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")

# Direct ATS beats an aggregator: the link is the real application page, and
# the posting arrived hours earlier.
SOURCE_RANK = {
    "greenhouse": 0, "lever": 0, "ashby": 0, "workday": 1,
    "smartrecruiters": 1, "amazon": 1, "simplify": 2,
}

# Staffing firms and body shops repost the same req endlessly. They are not
# excluded, only pushed down -- occasionally one is a real employer.
BODYSHOP = re.compile(
    r"\b(staffing|recruit(?:ing|ers)?|talent solutions|consultanc(?:y|ies)|"
    r"technologies llc|infotech|systems inc|solutions inc|it services|"
    r"outsourc\w+|manpower|randstad|robert half|insight global|teksystems|"
    r"apex systems|collabera|cognizant|infosys|wipro|hcl|accenture|deloitte|"
    r"capgemini|dellfor)\b",
    re.I,
)


def normalize_title(title):
    """Strip punctuation, years, and level suffixes so variants collapse."""
    text = _PUNCT.sub(" ", (title or "").lower())
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    return _WS.sub(" ", text).strip()


def dedupe_key(job):
    company = _PUNCT.sub(" ", (job.get("company") or "").lower())
    return (_WS.sub(" ", company).strip(), normalize_title(job.get("title")))


def group_key(job):
    """A stable identity for a role, independent of which source supplied it.

    `uid` carries the source, so the same role switches uid the moment a direct
    ATS starts covering what the aggregator used to supply. Anything the user
    attaches to a role, like having applied to it, has to hang off this instead
    or it silently detaches when coverage improves.
    """
    company, title = dedupe_key(job)
    digest = hashlib.sha1(("%s\x00%s" % (company, title)).encode("utf-8"))
    return digest.hexdigest()[:12]


def dedupe(jobs, lag_samples=None):
    """Collapse duplicates, keeping the best-sourced copy.

    The survivor records what it absorbed, so the dashboard can say a role was
    seen in three places rather than silently hiding two of them.

    Where a role arrives from both a direct ATS and the aggregator, the gap
    between their two timestamps is the aggregator's indexing lag, measured
    rather than assumed. Those samples go into `lag_samples` and are used to
    correct the age of roles that only ever arrive from the aggregator, whose
    date is when the aggregator noticed the posting and not when the company
    published it.
    """
    groups = {}
    for job in jobs:
        groups.setdefault(dedupe_key(job), []).append(job)

    collapsed = []
    for members in groups.values():
        members.sort(
            key=lambda j: (
                SOURCE_RANK.get(j.get("source"), 9),
                -(j.get("posted_at") or 0),
            )
        )
        best = dict(members[0])
        best["posted_from"] = best.get("source")
        best["group_key"] = group_key(best)

        if len(members) > 1:
            best["duplicate_count"] = len(members)
            best["duplicate_sources"] = sorted({m.get("source") for m in members})

            # Keep the earliest known posting time across the copies: the
            # aggregator often carries a date the ATS omits, and vice versa.
            dated = [m for m in members if m.get("posted_at")]
            if dated:
                earliest = min(dated, key=lambda m: m["posted_at"])
                best["posted_at"] = earliest["posted_at"]
                best["posted_from"] = earliest.get("source")

            if lag_samples is not None:
                direct = [m for m in dated if SOURCE_RANK.get(m.get("source"), 9) == 0]
                aggregated = [m for m in dated if m.get("source") == "simplify"]
                if direct and aggregated:
                    gap = aggregated[0]["posted_at"] - min(m["posted_at"] for m in direct)
                    # Only forward gaps are lag. A negative one means the ATS
                    # re-dated the req, which is not what is being measured.
                    if 0 < gap < 90 * 86400:
                        lag_samples.append(gap)

        collapsed.append(best)
    return collapsed


def score(job, now=None):
    """0-100ish. Higher is worth your time sooner. Returns (score, reasons)."""
    now = now or time.time()
    reasons = []
    total = 50.0

    status = job.get("work_auth", workauth.UNKNOWN)
    if status == workauth.BLOCKED:
        return 0, ["cannot be applied to: %s" % (job.get("work_auth_evidence") or "gated")]
    if status == workauth.OPEN:
        total += 25
        reasons.append("+25 states it sponsors")
    elif status == workauth.CLOSED:
        total -= 35
        reasons.append("-35 states it does not sponsor")

    posted = job.get("posted_at")
    if posted:
        hours = max(0.0, (now - posted) / 3600.0)
        if hours <= 6:
            total += 20
            reasons.append("+20 posted in the last 6h")
        elif hours <= 24:
            total += 14
            reasons.append("+14 posted today")
        elif hours <= 72:
            total += 7
            reasons.append("+7 posted in the last 3 days")
        elif hours > 720:
            total -= 10
            reasons.append("-10 posted over a month ago")

    if SOURCE_RANK.get(job.get("source"), 9) == 0:
        total += 8
        reasons.append("+8 direct from the ATS")

    if BODYSHOP.search(job.get("company") or ""):
        total -= 25
        reasons.append("-25 looks like a staffing firm")

    if (job.get("duplicate_count") or 0) > 3:
        total -= 8
        reasons.append("-8 reposted %d times" % job["duplicate_count"])

    if job.get("region") == "CA":
        total += 3
        reasons.append("+3 Canadian, no status needed")

    return int(max(0, min(100, round(total)))), reasons


def apply_ranking(jobs, now=None):
    now = now or time.time()
    for job in jobs:
        value, reasons = score(job, now)
        job["score"] = value
        job["score_reasons"] = reasons
    jobs.sort(key=lambda j: (-j.get("score", 0), -(j.get("posted_at") or 0)))
    return jobs
