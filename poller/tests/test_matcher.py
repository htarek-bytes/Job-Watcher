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
SPEC_CASES = [
    ("New Grad Software Engineer 2027", True),
    ("University Graduate, Software Engineer", True),
    ("Software Engineer I", True),
    ("Entry Level Data Engineer", True),
    ("Software Engineer II", False),
    ("Senior Software Engineer", False),
    ("Software Engineering Intern, Summer 2027", False),
    ("Engineering Manager", False),
]


class SpecTable(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_spec_table(self):
        for title, expected in SPEC_CASES:
            with self.subTest(title=title):
                got, reason = self.m.evaluate(title)
                self.assertEqual(got, expected, "%r -> %s (%s)" % (title, got, reason))


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
