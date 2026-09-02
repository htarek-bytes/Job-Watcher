"""Work authorization signal.

Four outcomes, most severe first:

  blocked  structurally impossible for a Canadian citizen -- a security
           clearance, an explicit US citizenship requirement, or an employer
           whose work is ITAR controlled. Not a sponsorship question.
  closed   the employer states it does not sponsor.
  open     the employer states it does sponsor.
  unknown  nothing found. The default, and the honest one.

Every call carries the phrase that produced it. This is a keyword guess on
someone else's prose, and it is never presented as a decision -- the evidence
exists so a wrong call can be spotted in a second rather than trusted.

Measured on the live feed: 11% of matching roles (58 of 527) sit at
US-person-gated employers and were previously surfaced as `unknown`.
"""

import re

BLOCKED = "blocked"
CLOSED = "closed"
OPEN = "open"
UNKNOWN = "unknown"

SEVERITY = {BLOCKED: 0, CLOSED: 1, OPEN: 2, UNKNOWN: 3}


def _phrases(*items):
    return [(p, re.compile(r"\b" + p.replace(" ", r"\s+") + r"\b", re.I)) for p in items]


# A clearance cannot be sponsored, granted, or worked around. These end it.
CLEARANCE = _phrases(
    r"ts/sci", r"top secret", r"secret clearance", r"active secret",
    r"security clearance", r"polygraph", r"poly required", r"full scope poly",
    r"q clearance", r"l clearance", r"public trust", r"dod clearance",
    r"clearance is required", r"clearance required", r"must be clearable",
)

CITIZENSHIP = _phrases(
    r"u\.?s\.? citizenship is required", r"must be a u\.?s\.? citizen",
    r"must be a united states citizen", r"u\.?s\.? citizenship required",
    r"requires u\.?s\.? citizenship", r"sole u\.?s\.? citizen",
    r"u\.?s\.? person as defined", r"itar", r"export control(?:led|s)? regulations",
    r"restricted to u\.?s\.? persons", r"green card holder",
)

# Checked before OPEN: an employer explaining that it will not sponsor often
# uses the word "sponsorship" in the same breath as a refusal.
NO_SPONSOR = _phrases(
    r"do(?:es)? not (?:offer|provide|sponsor)[^.]{0,40}sponsorship",
    r"not (?:able|eligible) to sponsor", r"unable to sponsor",
    r"cannot sponsor", r"can not sponsor", r"will not sponsor",
    r"no visa sponsorship", r"without (?:the need for )?(?:visa )?sponsorship",
    r"not require sponsorship", r"not provide visa",
    r"sponsorship is not available", r"we do not sponsor",
    r"unable to provide (?:visa )?sponsorship",
    r"not consider candidates requiring sponsorship",
)

SPONSOR = _phrases(
    r"visa sponsorship (?:is )?available", r"will sponsor", r"we sponsor",
    r"offer(?:s)? (?:visa )?sponsorship", r"provide (?:visa )?sponsorship",
    r"immigration support", r"tn visa", r"tn status", r"h-?1b",
    r"sponsorship for", r"open to sponsoring", r"visa support",
)

# Employers whose work is US-person gated in practice, regardless of the words
# in any one posting. Matched on company name.
US_PERSON_EMPLOYERS = re.compile(
    r"\b(spacex|anduril|raytheon|rtx|lockheed martin|lockheed|northrop|"
    r"general dynamics|johns hopkins applied physics|l3harris|booz allen|"
    r"leidos|mitre|sandia national|los alamos|draper|aerospace corporation|"
    r"ball aerospace|bae systems|peraton|caci|saic|parsons corporation|"
    r"mantech|peraton labs|peraton inc|palantir federal|anduril industries)\b",
    re.I,
)


def _context(text, match, width=90):
    """The phrase in its sentence, so a call can be checked at a glance."""
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


def _first(text, phrases):
    for label, pattern in phrases:
        found = pattern.search(text)
        if found:
            return label, _context(text, found)
    return None


def classify(title="", description="", company=""):
    """Return (status, evidence). Evidence is empty only for `unknown`."""
    haystack = "%s\n%s" % (title or "", description or "")

    if company and US_PERSON_EMPLOYERS.search(company):
        return BLOCKED, "employer is US-person gated in practice: %s" % company.strip()

    for phrases, label in ((CLEARANCE, "clearance"), (CITIZENSHIP, "citizenship")):
        hit = _first(haystack, phrases)
        if hit:
            return BLOCKED, "%s: %s" % (label, hit[1])

    hit = _first(haystack, NO_SPONSOR)
    if hit:
        return CLOSED, hit[1]

    hit = _first(haystack, SPONSOR)
    if hit:
        return OPEN, hit[1]

    return UNKNOWN, ""


def is_takeable(status):
    """Blocked roles are not a judgement call -- they cannot be applied to."""
    return status != BLOCKED
