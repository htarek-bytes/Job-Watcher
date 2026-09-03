import os
import sys
import tomllib
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matcher import Matcher, contains_phrase, normalize, strip_years, trailing_level

CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.toml"
)

with open(CONFIG, "rb") as fh:
    CFG = tomllib.load(fh)


# The table from the spec. These are the cases that must pass.
#
# One row has deliberately changed since the spec was written. The spec wanted
# internships rejected; they are now wanted, matched as a separate kind rather
# than blended into the new grad list. The old rule is still tested, below,
# under include_internships = false, because it is still the behaviour that
# flag is supposed to restore.
SPEC_CASES = [
    ("New Grad Software Engineer 2027", True),
    ("University Graduate, Software Engineer", True),
    ("Software Engineer I", True),
    ("Entry Level Data Engineer", True),
    ("Software Engineer II", False),
    ("Senior Software Engineer", False),
    ("Engineering Manager", False),
]

INTERNSHIP_CASES = [
    ("Software Engineering Intern, Summer 2027", True),
    ("Software Developer Co-op", True),
    ("Senior Software Engineering Intern", False),
    ("Engineering Internship Program Manager", False),
]


def _cfg(**overrides):
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg["match"] = dict(cfg["match"], **overrides)
    return cfg


class SpecTable(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_spec_table(self):
        for title, expected in SPEC_CASES:
            with self.subTest(title=title):
                got, reason = self.m.evaluate(title)
                self.assertEqual(got, expected, "%r -> %s (%s)" % (title, got, reason))


class Internships(unittest.TestCase):
    def setUp(self):
        self.on = Matcher(_cfg(include_internships=True))
        self.off = Matcher(_cfg(include_internships=False))

    def test_internships_match_when_wanted(self):
        for title, expected in INTERNSHIP_CASES:
            with self.subTest(title=title):
                got, reason = self.on.evaluate(title)
                self.assertEqual(got, expected, "%r -> %s (%s)" % (title, got, reason))

    def test_they_are_tagged_as_internships_not_new_grad(self):
        # Blending them into one list is the thing to avoid: the two have
        # different deadlines and different value.
        _, _, kind = self.on.evaluate_full("Software Engineering Intern")
        self.assertEqual(kind, "internship")

    def test_a_new_grad_role_is_still_tagged_new_grad(self):
        _, _, kind = self.on.evaluate_full("New Grad Software Engineer")
        self.assertEqual(kind, "new grad")

    def test_seniority_still_wins_over_an_internship(self):
        self.assertFalse(self.on.matches("Senior Software Engineering Intern"))
        self.assertFalse(self.on.matches("Software Engineering Intern III"))

    def test_the_flag_restores_the_original_spec_rule(self):
        # The spec's row, still enforced by the switch that turns this off.
        self.assertFalse(self.off.matches("Software Engineering Intern, Summer 2027"))
        self.assertFalse(self.off.matches("Software Developer Co-op"))

    def test_turning_it_off_leaves_new_grad_matching_alone(self):
        for title, expected in SPEC_CASES:
            with self.subTest(title=title):
                self.assertEqual(self.off.matches(title), expected, title)

    def test_a_product_word_is_not_an_internship(self):
        # The same trap as a bare "ii" matching inside "hawaii", one level up:
        # these are permanent roles whose titles name a product or a team, and
        # filing them under internships hides them from a new grad filter.
        for title in ("Software Engineer, Student Experience",
                      "Software Engineer, Student Success Platform",
                      "Software Developer, Internal Tools"):
            with self.subTest(title=title):
                _, _, kind = self.on.evaluate_full(title)
                self.assertNotEqual(kind, "internship", title)

    def test_a_real_student_internship_is_still_caught(self):
        _, _, kind = self.on.evaluate_full("Student Intern, Software Engineering")
        self.assertEqual(kind, "internship")

    def test_intern_still_never_fires_inside_internal(self):
        # The original trap. "Internal Tools" is not an internship.
        _, _, kind = self.on.evaluate_full("Software Engineer, Internal Tools")
        self.assertEqual(kind, "new grad" if kind else kind)
        self.assertNotEqual(kind, "internship")


class YearTrap(unittest.TestCase):
    """A bare " 2" exclude used to kill every posting containing 2027."""

    def setUp(self):
        self.m = Matcher(CFG)

    def test_years_stripped_before_level_check(self):
        self.assertEqual(strip_years(normalize("Software Engineer 2027")), "software engineer")

    def test_trailing_year_is_not_a_level(self):
        self.assertIsNone(trailing_level(strip_years(normalize("New Grad SWE 2027"))))

    def test_every_grad_year_still_matches(self):
        for year in ("2025", "2026", "2027", "2028"):
            with self.subTest(year=year):
                self.assertTrue(self.m.matches("New Grad Software Engineer %s" % year))

    def test_year_does_not_rescue_a_senior_role(self):
        self.assertFalse(self.m.matches("Senior Software Engineer 2027"))


class RomanNumeralTrap(unittest.TestCase):
    """Substring matching on "ii" fires inside ordinary words."""

    def setUp(self):
        self.m = Matcher(CFG)

    def test_ii_needs_word_boundaries(self):
        self.assertFalse(contains_phrase("hawaii", "ii"))
        self.assertTrue(contains_phrase("software engineer ii", "ii"))

    def test_hawaii_location_in_title_still_matches(self):
        self.assertTrue(self.m.matches("New Grad Software Engineer, Hawaii"))

    def test_real_level_two_rejected(self):
        self.assertFalse(self.m.matches("Software Engineer II"))
        self.assertFalse(self.m.matches("Software Engineer 2"))
        self.assertFalse(self.m.matches("Data Engineer III"))


class LevelSuffix(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_level_one_is_entry(self):
        self.assertTrue(self.m.matches("Software Engineer I"))
        self.assertTrue(self.m.matches("Software Engineer 1"))

    def test_only_trailing_token_counts(self):
        self.assertIsNone(trailing_level("v team software engineer"))
        self.assertEqual(trailing_level("software engineer v"), 5)

    def test_level_alone_is_not_enough_without_a_role(self):
        self.assertFalse(self.m.matches("Recruiter I"))


class ExcludeRules(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_intern_does_not_fire_inside_internal(self):
        self.assertFalse(contains_phrase("internal tools engineer", "intern"))

    def test_internal_tools_new_grad_still_matches(self):
        self.assertTrue(self.m.matches("New Grad Engineer, Internal Tools"))

    def test_seniority_words(self):
        for title in (
            "Staff Software Engineer",
            "Principal Software Engineer",
            "Lead Software Engineer",
            "Software Engineering Manager",
            "Distinguished Engineer",
        ):
            with self.subTest(title=title):
                self.assertFalse(self.m.matches(title))

    def test_non_engineering_roles_rejected(self):
        for title in (
            "New Grad Product Manager",
            "Entry Level Sales Associate",
            "University Graduate, Recruiting Coordinator",
        ):
            with self.subTest(title=title):
                self.assertFalse(self.m.matches(title))


class ReasonStrings(unittest.TestCase):
    """Every call has to be explainable, per the spec's never-present-a-guess rule."""

    def setUp(self):
        self.m = Matcher(CFG)

    def test_reason_present_for_each_outcome(self):
        for title, _ in SPEC_CASES:
            with self.subTest(title=title):
                _, reason = self.m.evaluate(title)
                self.assertTrue(reason)

    def test_empty_title(self):
        self.assertEqual(self.m.evaluate("")[1], "empty title")


if __name__ == "__main__":
    unittest.main(verbosity=2)
