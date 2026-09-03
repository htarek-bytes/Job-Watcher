"""Region tagging: US, CA, REMOTE, or OTHER.

Deliberately conservative. A posting we cannot place is tagged UNKNOWN and
kept (config: locations.allow_unknown), because dropping a real US req to keep
the feed tidy is the expensive mistake here.
"""

import re

US = "US"
CA = "CA"
REMOTE = "REMOTE"
OTHER = "OTHER"
UNKNOWN = "UNKNOWN"

_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}

_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

_US_CITIES = {
    "san francisco", "sf", "new york", "nyc", "seattle", "austin", "boston",
    "chicago", "los angeles", "la", "san jose", "palo alto", "mountain view",
    "sunnyvale", "menlo park", "redmond", "bellevue", "denver", "atlanta",
    "san diego", "washington", "arlington", "pittsburgh", "philadelphia",
    "cambridge", "santa clara", "cupertino", "boulder", "portland", "miami",
    "dallas", "houston", "phoenix", "detroit", "raleigh", "durham", "nashville",
    "salt lake city", "minneapolis", "brooklyn", "irvine", "san mateo",
    "culver city", "kirkland", "herndon", "reston", "mclean", "bentonville",
}

_CA_CITIES = {
    "toronto", "vancouver", "montreal", "montréal", "ottawa", "waterloo",
    "kitchener", "calgary", "edmonton", "winnipeg", "halifax", "quebec",
    "québec", "mississauga", "burnaby", "victoria", "hamilton", "london on",
}

_CA_PROVINCES = {
    "ontario", "quebec", "québec", "british columbia", "alberta", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland",
    "prince edward island",
}

_CA_CODES = {"on", "qc", "bc", "ab", "mb", "sk", "ns", "nb", "nl", "pe"}

_CANADA = re.compile(r"\bcanada\b|\bcanadian\b", re.I)
_USA = re.compile(r"\bunited states\b|\bu\.?s\.?a?\b|\bamerica\b", re.I)
_REMOTE = re.compile(r"\bremote\b|\banywhere\b|\bdistributed\b|\bwork from home\b", re.I)

# Countries that show up constantly in the SimplifyJobs feed and are not
# reachable on TN or as a Canadian fallback.
_ELSEWHERE = re.compile(
    r"\b(united kingdom|uk|london|england|scotland|ireland|dublin|germany|berlin|"
    r"munich|france|paris|netherlands|amsterdam|spain|madrid|barcelona|portugal|"
    r"lisbon|poland|warsaw|krakow|switzerland|zurich|zürich|sweden|stockholm|"
    r"norway|denmark|copenhagen|finland|helsinki|italy|milan|rome|india|bangalore|"
    r"bengaluru|hyderabad|pune|mumbai|delhi|gurgaon|noida|chennai|china|beijing|"
    r"shanghai|shenzhen|japan|tokyo|korea|seoul|singapore|australia|sydney|"
    r"melbourne|new zealand|israel|tel aviv|brazil|sao paulo|são paulo|mexico|"
    r"mexico city|argentina|colombia|bogota|chile|poland|romania|bucharest|"
    r"hungary|budapest|czech|prague|ukraine|kyiv|turkey|istanbul|dubai|uae|"
    r"abu dhabi|saudi|egypt|cairo|nigeria|lagos|kenya|nairobi|south africa|"
    r"taiwan|taipei|hong kong|vietnam|hanoi|thailand|bangkok|philippines|manila|"
    r"indonesia|jakarta|malaysia|kuala lumpur)\b",
    re.I,
)

_TOKEN_SPLIT = re.compile(r"[,/|;()\[\]]+|\s+-\s+|\bor\b|\band\b")


def _tokens(text):
    parts = [p.strip().lower() for p in _TOKEN_SPLIT.split(text) if p and p.strip()]
    return [p for p in parts if p]


def classify_one(text):
    if not text:
        return UNKNOWN
    low = text.lower()

    if _CANADA.search(low):
        return CA
    if _USA.search(low):
        return US

    toks = _tokens(low)
    for tok in toks:
        if tok in _CA_CITIES or tok in _CA_PROVINCES or tok in _CA_CODES:
            return CA
    for tok in toks:
        if tok in _US_CITIES or tok in _STATES or tok in _STATE_CODES:
            return US

    # Substring pass for "Toronto, Ontario" style strings the tokenizer missed.
    for name in _CA_CITIES | _CA_PROVINCES:
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return CA
    for name in _US_CITIES | _STATES:
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return US

    if _ELSEWHERE.search(low):
        return OTHER
    if _REMOTE.search(low):
        return REMOTE
    return UNKNOWN


def classify(values):
    """Classify a posting from one or more location strings.

    A posting listed in several places keeps the best region it offers: a role
    open in both Toronto and London is a Canadian role worth knowing about.
    """
    if isinstance(values, str):
        values = [values]
    values = [v for v in (values or []) if v]
    if not values:
        return UNKNOWN, ""

    seen = []
    for value in values:
        seen.append((classify_one(value), value))

    for wanted in (US, CA, REMOTE):
        for region, value in seen:
            if region == wanted:
                return region, value

    for region, value in seen:
        if region == OTHER:
            return OTHER, value
    return UNKNOWN, values[0]


def classify_all(values):
    """Every region a posting covers, not just the best one.

    `classify` returns one region and prefers US, which is right for the
    headline tag but wrong for filtering: a role open in Toronto and New York
    is tagged US, so filtering the dashboard to Canada hid it. Canadian roles
    at companies that also hire in the States are exactly the ones worth
    seeing, and there were 496 US rows to 24 Canadian ones to begin with.
    """
    if isinstance(values, str):
        values = [values]
    found = {classify_one(v) for v in (values or []) if v}
    found.discard(UNKNOWN)
    return sorted(found)


# Where a remote role will actually hire, which is a different question from
# where it is listed. "Remote" on a US company's board almost always means US
# only, so being remote is never on its own taken as being open to Canada.
GLOBAL = "GLOBAL"
CA_OK = "CA_OK"
# Geo-locked somewhere that is not Canada. Not "US only": the same rule catches
# EMEA-only and India-only postings, and naming it after one of them made the
# results read wrong.
LOCKED = "LOCKED"

# Only used on a description, and deliberately narrow. The bare word "remote"
# appears in half of all descriptions ("we support remote work", "hybrid with
# remote days"), so matching it would call almost everything remote. These are
# phrases a posting only uses when the role itself is remote.
_FULLY_REMOTE = re.compile(
    r"\b(fully remote|100% remote|100 percent remote|remote[- ]first|"
    r"remote position|remote role|work from anywhere|fully distributed|"
    r"this role is remote|entirely remote|permanently remote)\b", re.I)

_ANYWHERE = re.compile(
    r"\b(work from anywhere|from anywhere|anywhere in the world|any location|"
    r"worldwide|world wide|globally|global remote|remote global|"
    r"fully distributed|any country|location agnostic)\b", re.I)

# Checked before the global markers, because a posting can say both: "fully
# remote" in the headline and "must reside in the United States" in the body.
# The restriction is the operative half.
_GEO_LOCKED = re.compile(
    r"\bmust (?:be (?:located|based|authorized)|reside|live|work)\b[^.]{0,60}"
    r"\b(united states|u\.s\.|usa|us only)\b"
    r"|\bremote\b[^.]{0,25}\b(?:us|u\.s\.|usa|united states)[- ]?(?:only|based)\b"
    r"|\bus[- ]based (?:only|candidates)\b"
    r"|\b(?:eu|emea|uk|india|latam|apac)[- ]?(?:only|based only)\b"
    r"|\bauthorized to work in the (?:united states|us)\b",
    re.I,
)


def remote_scope(values, description=""):
    """Whether a remote role is open beyond one country, and beyond it to us.

    Returns GLOBAL, CA_OK, US_ONLY or UNKNOWN, and None when the role is not
    remote at all. A role is only treated as reachable from Canada on an
    explicit signal: "remote" by itself is the single most over-claimed word on
    a job board, and presenting a US-only role as Canada-open wastes an
    application, which is the expensive mistake here.
    """
    # The remote signal is not always in the location. A role listed as
    # "Berlin, Germany" whose description says it hires from any country is
    # exactly the kind this exists to find, and reading only the location field
    # threw it away.
    if not is_remote(values) and not _FULLY_REMOTE.search(description or ""):
        return None
    text = " ".join([v for v in (values or []) if v] + [description or ""])
    if _GEO_LOCKED.search(text):
        return LOCKED
    if _ANYWHERE.search(text):
        return GLOBAL
    if _CANADA.search(text) or any(
        classify_one(v) == CA for v in (values or []) if v
    ):
        return CA_OK
    return UNKNOWN


def is_remote(values):
    """Whether a posting says it is remote, regardless of its region.

    `classify` returns one region and prefers a country over REMOTE, so a role
    listed "Remote - US" is tagged US and a Remote filter built on the region
    finds almost nothing: the live feed had 18 postings whose location says
    remote and exactly 1 tagged REMOTE. Remote is a property of the role, not
    a place, so it is recorded separately.
    """
    if isinstance(values, str):
        values = [values]
    return any(_REMOTE.search(v) for v in (values or []) if v)


def allowed(region, cfg, scope=None):
    """Whether a posting's location is one worth alerting on.

    `scope` is remote_scope's answer. It is what lets a role listed in Berlin
    or Sydney through: a fully remote role that says it hires anywhere, or
    names Canada, is reachable on a Canadian passport, and dropping it because
    of the city in its location field loses exactly the roles this filter has
    no other way to find. Without that signal a foreign posting is still
    dropped, which is the old behaviour.
    """
    loc = cfg.get("locations", {})
    countries = set(loc.get("countries", [US, CA]))
    if scope in (GLOBAL, CA_OK) and loc.get("allow_global_remote", True):
        return True
    if region in (US, CA):
        return region in countries
    if region == REMOTE:
        return bool(loc.get("allow_remote", True))
    if region == UNKNOWN:
        return bool(loc.get("allow_unknown", True))
    return False
