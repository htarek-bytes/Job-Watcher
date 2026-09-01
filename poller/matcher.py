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


def normalize(title):
    """Lowercase, drop punctuation, collapse whitespace.

    Punctuation goes so that "University Graduate, Software Engineer" and
    "Software Engineer - New Grad" reduce to the same shape. Slashes survive
    for "engineer/developer", + and # survive for "c++" and "c#".
    """
    text = title.lower()
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


class Matcher:
    def __init__(self, cfg):
        m = cfg["match"]
        self.role_keywords = [normalize(k) for k in m["role_keywords"]]
        self.new_grad_phrases = [normalize(k) for k in m["new_grad_phrases"]]
        self.exclude_keywords = [normalize(k) for k in m["exclude_keywords"]]
        self.max_level = m.get("max_level", ENTRY_LEVEL)

    def evaluate(self, title):
        """Return (matched, reason). The reason is kept for the dashboard and
        for debugging a miss without re-running the poller."""
        text = strip_years(normalize(title))
        if not text:
            return False, "empty title"

        role = next((k for k in self.role_keywords if contains_phrase(text, k)), None)
        if role is None:
            return False, "no role keyword"

        bad = next((k for k in self.exclude_keywords if contains_phrase(text, k)), None)
        if bad is not None:
            return False, "excluded by %r" % bad

        level = trailing_level(text)
        if level is not None and level > self.max_level:
            return False, "seniority level %d suffix" % level

        phrase = next(
            (k for k in self.new_grad_phrases if contains_phrase(text, k)), None
        )
        if phrase is not None:
            return True, "new grad phrase %r" % phrase
        if level is not None:
            return True, "level %d suffix on %r" % (level, role)

        return False, "no new grad signal"

    def matches(self, title):
        return self.evaluate(title)[0]
