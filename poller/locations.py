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


def allowed(region, cfg):
    loc = cfg.get("locations", {})
    countries = set(loc.get("countries", [US, CA]))
    if region in (US, CA):
        return region in countries
    if region == REMOTE:
        return bool(loc.get("allow_remote", True))
    if region == UNKNOWN:
        return bool(loc.get("allow_unknown", True))
    return False
