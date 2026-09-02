"""Resume support, built only from data the sweep already has.

Greenhouse, Lever and Ashby return full job descriptions, which the poller
previously scanned for work authorization and then discarded. The same text
answers a more useful question: across the roles you actually match, what do
employers keep asking for, and what does your CV not say?

Three outputs:

  demand    aggregate skill counts across matched roles -> what to add
  gap       per role, requested skills your profile lacks -> what to tailor
  lint      bullets checked against a measured pattern -> how to write it

What this deliberately does not do: write bullets for you. A generated bullet
is either generic or a claim you cannot defend in an interview, and both are
worse than the blank line you already have.

Note: SimplifyJobs carries no description text, so roles arriving only from
there contribute nothing here.
"""

import re

# (canonical name, pattern). Short and ambiguous names are the whole difficulty:
# a bare "go" or "c" or "r" appears in ordinary prose constantly. Those are
# matched case-sensitively and only in contexts a job description actually
# uses, which trades a little recall for not reporting nonsense.
SKILLS = [
    ("Java", r"\bjava\b(?!\s*script)"),
    ("JavaScript", r"\bjavascript\b|\bjs\b"),
    ("TypeScript", r"\btypescript\b|\bts\b(?!/)"),
    ("Python", r"\bpython\b"),
    ("C++", r"c\+\+"),
    ("C#", r"c#|\.net\b"),
    ("C", r"(?<![a-zA-Z+#])C(?=\s*(?:/|,|\s+or\b|\s+and\b)\s*C\+\+)|\bC\s*/\s*C\+\+"),
    # Case-sensitive overall, so the "golang" spelling states both cases itself.
    ("Go", r"\b[Gg]olang\b|(?<![a-zA-Z])Go(?=\s*[,/]|\s+programming|\s+language)"),
    ("Rust", r"\brust\b"),
    ("Kotlin", r"\bkotlin\b"),
    ("Swift", r"\bswift\b(?!\s*ly)"),
    ("Ruby", r"\bruby\b"),
    ("Scala", r"\bscala\b"),
    ("PHP", r"\bphp\b"),
    ("SQL", r"\bsql\b"),
    ("React", r"\breact(?:\.js)?\b"),
    ("Next.js", r"\bnext\.js\b"),
    ("Node.js", r"\bnode(?:\.js)?\b"),
    ("Angular", r"\bangular\b"),
    ("Vue", r"\bvue(?:\.js)?\b"),
    ("Spring", r"\bspring\s*(?:boot|framework)?\b"),
    ("Django", r"\bdjango\b"),
    ("Flask", r"\bflask\b"),
    ("REST", r"\brest(?:ful)?\s*apis?\b|\brest\b"),
    ("GraphQL", r"\bgraphql\b"),
    ("gRPC", r"\bgrpc\b"),
    ("PostgreSQL", r"\bpostgres(?:ql)?\b"),
    ("MySQL", r"\bmysql\b"),
    ("MongoDB", r"\bmongo(?:db)?\b"),
    ("Redis", r"\bredis\b"),
    ("Kafka", r"\bkafka\b"),
    ("Elasticsearch", r"\belastic\s*search\b|\belasticsearch\b"),
    ("Docker", r"\bdocker\b|\bcontainer(?:s|ized|ization)?\b"),
    ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
    ("Terraform", r"\bterraform\b"),
    ("AWS", r"\baws\b|\bamazon web services\b"),
    ("GCP", r"\bgcp\b|\bgoogle cloud\b"),
    ("Azure", r"\bazure\b"),
    ("CI/CD", r"\bci\s*/\s*cd\b|\bcontinuous integration\b|\bjenkins\b|\bgithub actions\b"),
    ("Linux", r"\blinux\b|\bunix\b"),
    ("Git", r"\bgit\b(?!hub\s+actions)"),
    ("Microservices", r"\bmicro\s*services?\b"),
    ("Distributed systems", r"\bdistributed\s+(?:systems?|computing)\b"),
    ("Concurrency", r"\bconcurren(?:cy|t)\b|\bmulti\s*threading\b|\bparallel(?:ism)?\b"),
    ("Data structures & algorithms", r"\bdata structures\b|\balgorithms?\b"),
    ("Operating systems", r"\boperating systems?\b"),
    ("Networking", r"\bnetworking\b|\btcp/ip\b"),
    ("Testing", r"\bunit test(?:s|ing)?\b|\btest(?:ing)? automation\b|\bjunit\b|\bpytest\b"),
    ("Observability", r"\bobservability\b|\bprometheus\b|\bgrafana\b|\bmonitoring\b"),
    ("Machine learning", r"\bmachine learning\b|\bdeep learning\b|\bpytorch\b|\btensorflow\b"),
    ("Security", r"\b(?:application|software|cyber)\s+security\b|\bcryptograph\w+\b"),
    ("Spark", r"\bspark\b"),
    ("Airflow", r"\bairflow\b"),
]

# Case-sensitive by design; see the note above.
_CASE_SENSITIVE = {"C", "Go"}
COMPILED = [
    (name, re.compile(pattern, 0 if name in _CASE_SENSITIVE else re.I))
    for name, pattern in SKILLS
]


def extract(text):
    """Canonical skill names mentioned in a job description."""
    if not text:
        return set()
    return {name for name, pattern in COMPILED if pattern.search(text)}


def profile_skills(profile):
    """Everything the CV already claims, lowercased for comparison."""
    have = set()
    for key in ("technical", "also_known"):
        have |= {s.strip().lower() for s in (profile.get(key) or []) if s.strip()}
    return have


def gap(requested, have):
    """Requested skills the profile does not claim, in a stable order."""
    return sorted(s for s in requested if s.lower() not in have)


def demand(counter, have, total_roles):
    """Rank skills by how many real openings ask for them."""
    rows = []
    for skill, count in counter.items():
        rows.append({
            "skill": skill,
            "roles": count,
            "pct": round(100.0 * count / total_roles, 1) if total_roles else 0.0,
            "on_cv": skill.lower() in have,
        })
    rows.sort(key=lambda r: (-r["roles"], r["skill"]))
    return rows


# --------------------------------------------------------------------------
# Bullet linter
#
# The thresholds are measured, not invented. A reference CV that earned
# interviews at Amazon, Microsoft and IBM ran 9 bullets: median 28 words, max
# 30, 100% containing a number, 100% ending on the outcome.
# --------------------------------------------------------------------------

MIN_WORDS = 20
MAX_WORDS = 32
TAIL_WORDS = 6

WEAK_OPENERS = re.compile(
    r"^(responsible for|helped|worked on|assisted|participated|involved in|"
    r"tasked with|duties included|contributed to)\b", re.I,
)

# Past tense, and the verb carries ownership rather than participation.
STRONG_OPENERS = {
    "built", "shipped", "led", "designed", "cut", "reduced", "delivered",
    "launched", "migrated", "automated", "implemented", "rewrote", "wrote",
    "debugged", "diagnosed", "hardened", "scaled", "optimised", "optimized",
    "eliminated", "replaced", "merged", "gated", "exported", "instrumented",
    "modelled", "modeled", "containerised", "containerized", "ran", "drove",
    "owned", "resolved", "root-caused", "closed", "removed", "added",
}

FIRST_PERSON = re.compile(r"\b(i|my|me|we|our)\b", re.I)


def lint_bullet(text):
    """Return a list of findings. Empty means the bullet matches the pattern."""
    words = text.split()
    findings = []

    if len(words) < MIN_WORDS:
        findings.append("too short (%d words, aim %d-%d)" % (len(words), MIN_WORDS, MAX_WORDS))
    elif len(words) > MAX_WORDS:
        findings.append("too long (%d words, aim %d-%d)" % (len(words), MIN_WORDS, MAX_WORDS))

    if not re.search(r"\d", text):
        findings.append("no number anywhere: a scanner takes nothing from this")
    else:
        tail = " ".join(words[-TAIL_WORDS:])
        if not re.search(r"\d", tail):
            findings.append("does not end on the outcome; move the number to the end")

    first = re.sub(r"[^a-z-]", "", words[0].lower()) if words else ""
    if WEAK_OPENERS.match(text):
        findings.append("weak opener %r: state what you did, not what you were assigned" % words[0])
    elif first and first not in STRONG_OPENERS:
        findings.append("opener %r is not a strong past-tense verb" % words[0])

    if FIRST_PERSON.search(text):
        findings.append("first person: drop I/we")

    return findings


def parse_bullets(text):
    """Pull bullets out of pasted CV text.

    Handles both bullet characters and lines wrapped by a PDF extractor.
    """
    flat = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    chunks = re.split(r"[••●]|(?:^|\s)-\s", flat)
    out = []
    for chunk in chunks:
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if len(chunk.split()) < 5:
            continue
        # Section headers and skill dumps are not bullets.
        if re.match(r"^(technical|general|soft)\s+skills?\b", chunk, re.I):
            continue
        if re.match(r"^coursework\b", chunk, re.I):
            continue
        # Achievement entries ("... | 2025") are one-line credentials, not
        # experience bullets, and the verb/length rules do not apply to them.
        if re.search(r"\|\s*\d{4}\s*$", chunk):
            continue
        out.append(chunk)
    return out
