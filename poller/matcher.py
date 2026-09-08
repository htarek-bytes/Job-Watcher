"""Title matching for new grad roles.

Three gates, in order:

1. the title must name a role we care about (role_keywords)
2. the title must not hit an exclude keyword
3. the title must not carry a seniority level above the entry level

and then it must show a new grad signal: either an explicit phrase
("new grad", "university graduate", "entry level") or a level-I suffix.

Two traps this module exists to avoid, both hit by hand first:

* A bare " 2" exclude kills every posting containing 2027. Four digit years
  are stripped before any level reasoning happens, so "Software Engineer 2027"
  never looks like a level 2 posting.
* Substring matching on "ii" fires inside ordinary words (Hawaii, viii).
  Every keyword test here is anchored on word boundaries.
"""

import re
import unicodedata

# Roman and arabic level tokens, mapped to the number they mean. Anything above
# ENTRY_LEVEL is a more senior req wearing a new grad shaped title.
_LEVELS = {
    "i": 1, "1": 1,
    "ii": 2, "2": 2,
    "iii": 3, "3": 3,
    "iv": 4, "4": 4,
    "v": 5, "5": 5,
}

ENTRY_LEVEL = 1

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_PUNCT = re.compile(r"[^a-z0-9+#/ ]+")
_SPACE = re.compile(r"\s+")


def fold_accents(text):
    """Strip accents, so French titles match keywords written in plain ASCII.

    Necessary rather than cosmetic. Job Bank and Jobillico are bilingual and a
    French search returns French titles, so "Analyste de systèmes" and
    "Développeur" have to reduce to "analyste de systemes" and "developpeur"
    or they fail the role gate outright. Before this, every French query in
    the config was spending requests on results the matcher could not read.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def normalize(title):
    """Lowercase, fold accents, drop punctuation, collapse whitespace.

    Punctuation goes so that "University Graduate, Software Engineer" and
    "Software Engineer - New Grad" reduce to the same shape. Slashes survive
    for "engineer/developer", + and # survive for "c++" and "c#".
    """
    text = fold_accents(title.lower())
    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def strip_years(text):
    """Remove four digit years.

    This runs before level detection so that a trailing "2027" is never read as
    a level, and before exclude matching so that a careless " 2" style keyword
    cannot fire on it.
    """
    return _SPACE.sub(" ", _YEAR.sub(" ", text)).strip()


def contains_phrase(text, phrase):
    """Word boundary containment. `text` must already be normalized.

    Guards the "ii" trap: contains_phrase("hawaii", "ii") is False.
    """
    norm = normalize(phrase)
    if not norm:
        return False
    return re.search(r"\b" + re.escape(norm) + r"\b", text) is not None


def trailing_level(text):
    """The seniority level a title ends in, or None.

    Expects year-stripped, normalized text. Only a trailing token counts:
    "Software Engineer II" is level 2, but "V Team Software Engineer" is not
    level 5 because the token is not final.
    """
    tokens = text.split()
    if not tokens:
        return None
    return _LEVELS.get(tokens[-1])


NEW_GRAD = "new grad"
INTERNSHIP = "internship"
# A software role with no seniority marker at all: "Developer, Rust",
# "Full Stack Software Developer", "Firmware Engineer". Not new grad, not
# senior, and at a two hundred person company frequently open to a strong new
# graduate. An audit of 90 Canadian boards found 63 of these being dropped for
# "no new grad signal" while only 122 postings matched, so they are the single
# largest recoverable group on the Canadian side.
#
# They are a THIRD kind rather than more new grad results, for the same reason
# internships are: the confidence is different and a blended list hides that.
OPEN_LEVEL = "open level"
# A role whose own description asks for at most a few years. Stronger evidence
# than OPEN_LEVEL, because the posting states the requirement rather than
# leaving it unsaid, and it is the tier most new grads actually get hired into.
JUNIOR = "0 to 3 years"

# Tracks. A kind says how junior a role is; a track says what job it is.
#
# Technical pre-sales is a different career from writing software and wants a
# different role list, so mixing the two into one gate would mean either
# polluting the software feed or not carrying pre-sales at all. Instead the
# title picks a track, the track picks the keyword and exclusion lists, and
# everything after that -- seniority, internships, the four kinds -- is shared.
# The dashboard filters on it, so neither track buries the other.
SOFTWARE = "software"
PRESALES = "presales"
# Systems administration, systems analysis, infrastructure, networking, IT
# operations, cloud and platform operations, security operations, OT and
# SCADA, telecommunications, rail and transportation technology, energy and
# utilities technology, and public sector IT. One track rather than twelve,
# because they are one labour market: the same person moves between them, and
# a dashboard with twelve filters is a dashboard nobody uses.
INFRASTRUCTURE = "infrastructure"

# Role keywords too generic to decide a track with. Every title on every track
# contains at least one of these, so they say nothing about which job it is.
_GENERIC_ROLE = {"engineer", "developer", "programmer", "sde", "swe"}

# Words that settle the track on their own: a title carrying one of them is a
# software engineering role whatever else it also says, so "Software Engineer -
# Solutions Engineering" is not pre-sales and "Software Systems Analyst" is not
# infrastructure.
#
# This started out as a derived set -- every role keyword no other track had
# claimed -- and that was too clever by half. It could not decide "Software
# Systems Analyst", because the very keyword it needed to win with was the one
# handed over, and it made the answer depend on the shape of three lists at
# once. Three words that name the discipline outright do the same job and can
# be reasoned about by reading them.
_SOFTWARE_MARKER = ("software", "developer", "programmer")

# "2+ years of experience", "1-3 years experience", "at least 2 years of
# relevant experience". Only counted when the word experience is nearby: a
# description mentioning "5 years ago" or "over the last 3 years" is talking
# about something else entirely.
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|—|to|or)?\s*(\d{1,2})?\s*"
    r"(?:\+|plus)?\s*years?\b",
    re.I,
)
_EXPERIENCE_NEARBY = 70


def min_years(text):
    """The smallest years-of-experience requirement a description states.

    The minimum, not the maximum: a posting asking "2+ years, 5 preferred" is
    reachable at two. Returns None when it says nothing about years, which is
    most postings and must not be read as zero.
    """
    if not text:
        return None
    low = text.lower()
    found = []
    for match in _YEARS.finditer(low):
        window = low[max(0, match.start() - _EXPERIENCE_NEARBY):
                     match.end() + _EXPERIENCE_NEARBY]
        if "experience" not in window:
            continue
        years = int(match.group(1))
        # A range gives its low end, which is the bar to clear.
        if years <= 20:
            found.append(years)
    return min(found) if found else None


class Track:
    """One job family: its own titles, its own exclusions, its own level rules.

    A track is not a level and not a place. It answers "what job is this",
    where `kind` answers "how junior is it", and keeping the two apart is what
    lets the dashboard show a systems administrator and a new grad software
    engineer in the same feed without either burying the other.

    Everything a track does not override is shared: the seniority words, the
    level suffixes, the internship handling and the four kinds all behave the
    same on every track.
    """

    def __init__(self, name, block, base_excludes, default_years):
        self.name = name
        self.enabled = bool(block.get("enabled", False))
        self.role_keywords = [normalize(k) for k in block.get("role_keywords", [])]
        # Most of these families almost never say "new grad" in the title, so
        # requiring an early career signal would empty the track. The tier is
        # labelled, so it stays filterable rather than silently blended in.
        self.open_level = bool(block.get("open_level", True))
        self.max_years = int(block.get("max_years_experience", default_years))
        # Regions the OPEN LEVEL tier is restricted to. Empty means anywhere
        # the location rules already allow.
        #
        # It restricts that tier alone, and the difference was measured rather
        # than reasoned. Restricting the whole track to Canada would have
        # dropped 176 roles the board already carried, and 174 of them held a
        # real early career signal: a new grad title, an internship, or a
        # stated bar of nought to three years. Those earned their place and
        # losing them was not what anyone asked for.
        #
        # The tier that does need holding back is the permissive one. "Systems
        # Analyst" with no seniority marker is one of the most common job
        # titles in North America, and accepting it from every US board would
        # bury the Canadian roles this track exists to find under thousands of
        # American ones.
        self.open_level_regions = [
            str(r) for r in block.get("open_level_regions", [])]
        self.label = block.get("label", name)

        # Every shared exclusion still applies except the ones this track's own
        # titles contain. Worked out from the two lists rather than restated by
        # hand: "sales engineer" and "solutions engineer" are software-side
        # exclusions precisely because they are pre-sales roles, and a bare
        # "consultant" would reject "Solutions Consultant". Dropping only the
        # words a track's own role names carry cannot over-drop.
        self.excludes = [
            k for k in base_excludes
            if not any(contains_phrase(role, k) for role in self.role_keywords)
        ] + [normalize(k) for k in block.get("exclude_keywords", [])]

    def claims(self, text):
        return self.enabled and any(
            contains_phrase(text, k) for k in self.role_keywords)


# Tracks are declared in config as [match.<name>] sub-tables. Order matters
# only for a title that two of them could claim, which the software-core rule
# below already settles for the common cases.
TRACK_ORDER = ("presales", "infrastructure")


class Matcher:
    def __init__(self, cfg):
        m = cfg["match"]
        self.role_keywords = [normalize(k) for k in m["role_keywords"]]
        self.new_grad_phrases = [normalize(k) for k in m["new_grad_phrases"]]
        self.exclude_keywords = [normalize(k) for k in m["exclude_keywords"]]
        self.max_level = m.get("max_level", ENTRY_LEVEL)
        # The most years of experience a posting may ask for and still count.
        self.max_years = int(m.get("max_years_experience", 3))

        # Internships were an exclusion, full stop. They are now a second kind
        # of match, kept apart from new grad roles rather than mixed into them:
        # the two have different deadlines and different value, and a list that
        # silently blends them is worse than either list alone.
        self.internship_phrases = [
            normalize(k) for k in m.get("internship_phrases", [])
        ]
        self.include_internships = bool(m.get("include_internships", False))
        # When internships are wanted, the internship words in the exclusion
        # list would reject them before they could be classified, so they are
        # dropped from the list that runs. Everything else still applies: a
        # senior or staff internship is still not this.
        internship_set = set(self.internship_phrases)
        self.active_excludes = (
            [k for k in self.exclude_keywords if k not in internship_set]
            if self.include_internships else self.exclude_keywords
        )

        # Every non-software track, in declaration order. Each is off unless
        # config says otherwise, so adding one cannot change the software feed
        # by being merged.
        self.tracks = [
            Track(name, m.get(name) or {}, self.active_excludes, self.max_years)
            for name in TRACK_ORDER
        ]
        self.by_track = {t.name: t for t in self.tracks}

        # The software track, expressed the same way so evaluate_full has one
        # shape to work with rather than a special case.
        self.software = Track(SOFTWARE, {
            "enabled": True,
            "role_keywords": m["role_keywords"],
            # Unlabelled software roles stay Canadian-boards-only, decided by
            # the caller, for the reason recorded in config.toml: accepting
            # them everywhere buries the new grad results under US mid-level
            # ones. The other tracks are small enough not to have that problem.
            "open_level": False,
            "max_years_experience": self.max_years,
        }, self.active_excludes, self.max_years)

        # The words that settle the track on their own. See _SOFTWARE_MARKER
        # for why this is a short fixed list and not something derived from
        # the other tracks' keywords.
        self.software_core = list(_SOFTWARE_MARKER)
        # Kept for the tests and for anything reading the older shape.
        self.specific_role_keywords = self.software_core

    # ---- compatibility shims for the pre-sales-only shape -----------------
    @property
    def presales_enabled(self):
        return self.by_track["presales"].enabled

    @property
    def presales_keywords(self):
        return self.by_track["presales"].role_keywords

    @property
    def presales_excludes(self):
        return self.by_track["presales"].excludes

    @property
    def presales_open_level(self):
        return self.by_track["presales"].open_level

    def track_for(self, text):
        """The Track object a normalized, year-stripped title belongs to."""
        if any(contains_phrase(text, k) for k in self.software_core):
            return self.software
        for track in self.tracks:
            if track.claims(text):
                return track
        return self.software

    def _track(self, text):
        """The name of the track a normalized, year-stripped title belongs to.

        The tracks overlap by design. "Sales Engineer" is a software-side
        exclusion precisely because it is a pre-sales role, and "Systems
        Analyst" sits in the software list only because an earlier widening put
        it there when there was nowhere else for it to go.

        An unambiguously software title still wins, so "Software Engineer -
        Solutions Engineering" and "Software Systems Analyst" stay where they
        belong. See software_core for how that set is worked out.
        """
        return self.track_for(text).name

    def resolve_track(self, title):
        """The Track a raw title belongs to. Same decision evaluate_full makes."""
        return self.track_for(strip_years(normalize(title)))

    def track(self, title):
        """The track a raw title belongs to. Same decision `evaluate_full` makes."""
        return self._track(strip_years(normalize(title)))

    def early_career_query(self, text):
        """Whether a search term itself carries an early career signal."""
        norm = strip_years(normalize(text))
        return next((k for k in self.new_grad_phrases
                     if contains_phrase(norm, k)), None)

    def evaluate(self, title, signal=None):
        """Return (matched, reason). See evaluate_full for the kind as well."""
        matched, reason, _ = self.evaluate_full(title, signal)
        return matched, reason

    def evaluate_full(self, title, signal=None, allow_open_level=False,
                      years=None):
        """Return (matched, reason, kind). The reason is kept for the dashboard and
        for debugging a miss without re-running the poller.

        `signal` is an early career signal the SOURCE vouches for rather than
        one in the title. Job Bank is why it exists: it displays the NOC title,
        so a req the employer called "Junior Software Developer" is listed as
        plain "software developer" and fails the fourth gate every time. A
        measured run searched "junior software developer" and got back 25
        postings not one of which had a qualifying title.

        It only ever substitutes for the fourth gate. The exclusions and the
        seniority suffix are still read off the real title, so a senior req
        that surfaced because its description mentions junior developers is
        still thrown out.
        """
        text = strip_years(normalize(title))
        if not text:
            return False, "empty title", None

        track = self.track_for(text)
        if track.open_level:
            allow_open_level = True
        max_years = track.max_years

        role = next((k for k in track.role_keywords if contains_phrase(text, k)),
                    None)
        if role is None:
            return False, "no role keyword", None

        bad = next((k for k in track.excludes if contains_phrase(text, k)), None)
        if bad is not None:
            return False, "excluded by %r" % bad, None

        level = trailing_level(text)
        if level is not None and level > self.max_level:
            return False, "seniority level %d suffix" % level, None

        # Checked before the new grad gate, because an internship rarely says
        # "new grad" as well and would otherwise fall through to "no signal".
        intern = next(
            (k for k in self.internship_phrases if contains_phrase(text, k)), None
        )
        if intern is not None:
            if not self.include_internships:
                return False, "excluded by %r" % intern, None
            return True, "internship phrase %r" % intern, INTERNSHIP

        phrase = next(
            (k for k in self.new_grad_phrases if contains_phrase(text, k)), None
        )
        if phrase is not None:
            return True, "new grad phrase %r" % phrase, NEW_GRAD
        if level is not None:
            return True, "level %d suffix on %r" % (level, role), NEW_GRAD
        if signal:
            # Weaker evidence than a title, and labelled as such so the reason
            # shown in the dashboard does not read like the other three.
            return True, "matched the early career search %r" % signal, NEW_GRAD
        if years is not None and years <= max_years:
            # The posting's own requirement, so this is the strongest of the
            # weaker signals. The seniority exclusions have already run on the
            # title, which is what stops a senior req that happens to ask for
            # "1 year of leadership experience" getting in here.
            return True, "asks for %d year%s of experience" % (
                years, "" if years == 1 else "s"), JUNIOR
        if years is not None:
            # It stated a bar and the bar is too high. Said out loud rather than
            # falling through, because the tier below accepts a role for saying
            # nothing about seniority and this one has said plenty. Without this
            # a "Solutions Engineer" wanting eight years lands in "level not
            # stated", which is the one place it is least likely to be noticed.
            return False, "asks for %d years of experience" % years, None
        if allow_open_level:
            # Weakest of the four, and labelled so. The caller decides where
            # this is allowed; it is not a global loosening.
            return True, "%s role, no seniority stated" % track.label, OPEN_LEVEL

        return False, "no new grad signal", None

    def matches(self, title, signal=None):
        return self.evaluate(title, signal)[0]
