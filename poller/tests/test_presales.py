"""The technical pre-sales track.

Every title this track accepts is a title the software track excludes, and
that overlap is the whole design: "sales engineer" and "solutions engineer"
are software-side exclusions precisely because they are pre-sales roles. So
the tests here are mostly about the seam. A change that widened the software
list to reach these instead would have put a Sales Engineer in a feed of
software engineering roles, which is the thing the exclusions were added to
stop in the first place.

The other half is the noise. "Sales Engineer" is an old industrial title as
well as a software one, and a board search returns pump and HVAC reqs
alongside the SaaS ones.
"""

import os
import sys
import tomllib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import matcher as m
from matcher import Matcher, min_years

with open(os.path.join(os.path.dirname(HERE), "config.toml"), "rb") as fh:
    CFG = tomllib.load(fh)


def _off(cfg):
    """The same config with the track turned off, which is the old behaviour."""
    match = dict(cfg["match"])
    match["presales"] = dict(match["presales"], enabled=False)
    return dict(cfg, match=match)


class TrackSelection(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_a_presales_title_picks_the_presales_track(self):
        for title in ("Sales Engineer", "Associate Solutions Engineer",
                      "Pre-Sales Engineer", "Solutions Consultant",
                      "Technical Solutions Consultant", "Customer Engineer",
                      "Technical Account Executive",
                      "Junior Technical Sales Engineer"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.PRESALES)

    def test_a_software_title_stays_on_the_software_track(self):
        for title in ("Software Engineer, New Grad", "Data Engineer",
                      "Backend Engineer", "Site Reliability Engineer",
                      "Firmware Engineer", "Programmer Analyst",
                      # Removed from the pre-sales list on measurement: the
                      # feed already carries seven of these as new grad
                      # software roles, which is what they are.
                      "Forward Deployed Engineer, New Grad",
                      # The two-word rule again: "sales engineer" is not
                      # adjacent here, so this stays a software role.
                      "Software Engineer, Sales Platform"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.SOFTWARE)

    def test_a_specific_software_keyword_wins(self):
        # From the audit of the first 138 roles the track produced. These name
        # a software engineering role and happen to also name the team they
        # sit next to; reading them as pre-sales took real engineering jobs
        # out of the software filter.
        for title in ("Software Engineer - Solutions Engineering",
                      "DevOps Solutions Engineer",
                      "Data Engineer, Solutions"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.SOFTWARE)

    def test_a_generic_keyword_does_not_win(self):
        # Bare "engineer" is in nearly every title on both sides. If it counted
        # as specific there would be no pre-sales track at all.
        for title in ("Sales Engineer", "Solutions Engineer",
                      "Associate Solutions Engineer"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.PRESALES)

    def test_the_track_agrees_with_what_the_match_used(self):
        # `track` is computed separately from evaluate_full, so the two reading
        # a title differently is a real failure mode rather than a theoretical
        # one. A pre-sales title must not be matching on a software keyword.
        title = "Solutions Engineer"
        self.assertEqual(self.m.track(title), m.PRESALES)
        matched, reason, _kind = self.m.evaluate_full(title)
        self.assertTrue(matched, reason)
        self.assertIn("technical sales", reason)


class OffByDefault(unittest.TestCase):
    """With the track disabled nothing changes: these are exclusions again."""

    def setUp(self):
        self.m = Matcher(_off(CFG))

    def test_the_titles_go_back_to_being_excluded(self):
        for title in ("Sales Engineer", "Associate Solutions Engineer",
                      "Customer Engineer", "Solutions Consultant"):
            with self.subTest(title=title):
                matched, reason, _ = self.m.evaluate_full(
                    title, None, allow_open_level=True, years=1)
                self.assertFalse(matched, "%s (%s)" % (title, reason))

    def test_software_roles_are_untouched_by_the_switch(self):
        for title in ("New Grad Software Engineer", "Firmware Engineer"):
            with self.subTest(title=title):
                self.assertEqual(
                    self.m.evaluate_full(title, None, True)[0],
                    Matcher(CFG).evaluate_full(title, None, True)[0])


class Accepted(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_the_early_career_wordings_match_as_new_grad(self):
        for title in ("Associate Solutions Engineer",
                      "Junior Sales Engineer",
                      "Entry Level Solutions Consultant",
                      "Early Career Sales Engineer",
                      "Associate Technical Solutions Specialist",
                      "Sales Engineer I"):
            with self.subTest(title=title):
                matched, reason, kind = self.m.evaluate_full(title)
                self.assertTrue(matched, "%s (%s)" % (title, reason))
                self.assertEqual(kind, m.NEW_GRAD)

    def test_a_stated_bar_of_three_years_or_fewer_is_its_own_tier(self):
        for desc in ("2+ years of experience in a customer facing role",
                     "1-3 years of experience", "0 to 2 years experience"):
            with self.subTest(desc=desc):
                matched, reason, kind = self.m.evaluate_full(
                    "Solutions Engineer", years=min_years(desc))
                self.assertTrue(matched, reason)
                self.assertEqual(kind, m.JUNIOR)

    def test_an_unlabelled_role_is_carried_as_level_not_stated(self):
        # The tier that makes the track non-empty. A pre-sales req rarely says
        # new grad in its title, so without this there would be almost nothing
        # here. It is labelled, so it stays filterable.
        for title in ("Solutions Engineer", "Sales Engineer",
                      "Technical Solutions Specialist"):
            with self.subTest(title=title):
                matched, reason, kind = self.m.evaluate_full(title)
                self.assertTrue(matched, "%s (%s)" % (title, reason))
                self.assertEqual(kind, m.OPEN_LEVEL)

    def test_it_does_not_need_a_canadian_board_to_do_that(self):
        # The software side's open level tier is Canadian boards only, decided
        # by the caller. This track's is not, and that is a config switch
        # rather than a caller decision, so it holds with allow_open_level off.
        self.assertTrue(self.m.evaluate_full(
            "Solutions Engineer", None, allow_open_level=False)[0])

    def test_an_internship_is_still_an_internship(self):
        _, _, kind = self.m.evaluate_full("Sales Engineering Intern")
        self.assertEqual(kind, m.INTERNSHIP)

    def test_a_consultant_title_survives_the_bare_consultant_exclusion(self):
        # "consultant" is a software-side exclusion and is dropped for this
        # track because the track's own role names contain it. Without that
        # rule every Solutions Consultant would be rejected by it.
        self.assertNotIn("consultant", self.m.presales_excludes)
        self.assertIn("consultant", self.m.active_excludes)
        self.assertTrue(self.m.evaluate_full("Solutions Consultant")[0])

    def test_the_dropped_exclusions_are_only_the_track_s_own_titles(self):
        # The seniority words must survive the drop, or the track would accept
        # every senior req.
        for word in ("senior", "staff", "principal", "lead", "manager",
                     "director", "architect", "ii", "iii"):
            with self.subTest(word=word):
                self.assertIn(word, self.m.presales_excludes)


class Rejected(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_seniority_still_applies(self):
        for title in ("Senior Sales Engineer", "Staff Solutions Engineer",
                      "Principal Sales Engineer", "Sales Engineer II",
                      "Lead Solutions Consultant",
                      "Manager, Solutions Engineering",
                      "Director of Sales Engineering",
                      "Sr. Solutions Engineer",
                      "Solutions Architect"):
            with self.subTest(title=title):
                matched, reason, _ = self.m.evaluate_full(
                    title, None, allow_open_level=True, years=1)
                self.assertFalse(matched, "%s (%s)" % (title, reason))

    def test_a_bare_consultant_title_is_not_a_role_at_all(self):
        # Dropping the "consultant" exclusion for this track does not make
        # every consultancy a match: it takes a role keyword first, and there
        # is no bare "consultant" among them. Management consulting stays out.
        for title in ("Associate Consultant", "Consultant, Technology",
                      "Junior Consultant"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, allow_open_level=True, years=1)[0], title)

    def test_a_generic_sales_role_is_not_this(self):
        # The exclusion the whole track hangs on. A quota-carrying seat where
        # the CS degree is decoration is not a technical pre-sales job, and
        # accepting these would have swamped the track with them.
        for title in ("Account Executive", "Enterprise Account Executive",
                      "Business Development Representative",
                      "Sales Development Representative",
                      "Account Manager", "Inside Sales Representative",
                      "Sales Associate", "Retail Sales Advisor",
                      "Territory Sales Manager"):
            with self.subTest(title=title):
                matched, reason, _ = self.m.evaluate_full(
                    title, None, allow_open_level=True, years=1)
                self.assertFalse(matched, "%s (%s)" % (title, reason))

    def test_an_industrial_sales_engineer_is_not_this(self):
        # "Sales Engineer" predates software by a century and the boards are
        # full of the older meaning.
        for title in ("HVAC Sales Engineer", "Sales Engineer - Pumps",
                      "Mechanical Sales Engineer",
                      "Industrial Sales Engineer",
                      "Sales Engineer, Compressors",
                      "Field Sales Engineer",
                      "Automotive Sales Consultant"):
            with self.subTest(title=title):
                matched, reason, _ = self.m.evaluate_full(
                    title, None, allow_open_level=True, years=1)
                self.assertFalse(matched, "%s (%s)" % (title, reason))

    def test_the_audited_noise_is_excluded(self):
        # Real titles from the first 138 the track produced. Two categories:
        # a salesperson with a technical product, and the person who runs a
        # company's own internal IT or recruiting tooling. Neither talks to a
        # customer about architecture.
        for title in ("Technical Sales Representative",
                      "Technical Sales Executive", "Technical Sales Analyst",
                      "IT Solutions Engineer (Networking)",
                      "Recruiting Solutions Engineer",
                      "AI Solutions Engineer, Talent Acquisition",
                      "Hardware Solutions Engineer",
                      "Electrical Sales Engineer",
                      "Founding Sales Engineer"):
            with self.subTest(title=title):
                matched, reason, _ = self.m.evaluate_full(
                    title, None, allow_open_level=True, years=1)
                self.assertFalse(matched, "%s (%s)" % (title, reason))

    def test_a_vertical_is_not_treated_as_an_industry(self):
        # The other half of that: at a software company these words name the
        # customer, not the job, so excluding them would drop real roles.
        for title in ("Solutions Engineer, Insurance",
                      "Sales Engineer - Healthcare",
                      "Associate Solutions Engineer, Automotive"):
            with self.subTest(title=title):
                matched, reason, _ = self.m.evaluate_full(title)
                self.assertTrue(matched, "%s (%s)" % (title, reason))


class StatedYearsAboveTheBar(unittest.TestCase):
    """A posting that states a bar above the ceiling is rejected, not demoted.

    It used to fall through to the tier below, which accepts a role for saying
    nothing about seniority. A Solutions Engineer wanting eight years would
    land in "level not stated", which is the one place it is least likely to
    be noticed. This applies to both tracks.
    """

    def setUp(self):
        self.m = Matcher(CFG)

    def test_a_presales_role_asking_too_much_is_dropped(self):
        years = min_years("8+ years of pre-sales experience")
        self.assertEqual(years, 8)
        matched, reason, kind = self.m.evaluate_full(
            "Solutions Engineer", None, allow_open_level=True, years=years)
        self.assertFalse(matched, reason)
        self.assertIsNone(kind)
        self.assertIn("8 years", reason)

    def test_a_software_role_asking_too_much_is_dropped_too(self):
        years = min_years("6 years of experience required")
        self.assertFalse(self.m.evaluate_full(
            "Developer, Rust", None, allow_open_level=True, years=years)[0])

    def test_saying_nothing_still_reaches_the_open_level_tier(self):
        # The rejection is for a stated bar. Silence is not a bar.
        _, _, kind = self.m.evaluate_full(
            "Developer, Rust", None, allow_open_level=True, years=None)
        self.assertEqual(kind, m.OPEN_LEVEL)


if __name__ == "__main__":
    unittest.main()
