"""The open-level tier, and where remote roles are reachable from Canada.

Both come out of measurements, not preference:

* An audit of 90 Canadian boards found 1976 postings, 122 matches, and 63
  drops for "no new grad signal". Those 63 are titles like "Developer, Rust"
  and "Full Stack Software Developer" with no seniority marker at all. They
  are the largest recoverable group on the Canadian side.
* The live feed had 18 postings whose location says remote and 1 tagged
  REMOTE, because the region tag prefers a country. And every posting outside
  the US and Canada was dropped on its city, including fully remote ones that
  hire anywhere.
"""

import os
import sys
import tomllib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import locations
from matcher import Matcher, min_years as matcher_min

with open(os.path.join(os.path.dirname(HERE), "config.toml"), "rb") as fh:
    CFG = tomllib.load(fh)


# Real titles from the audit's "no new grad signal" bucket.
AUDITED_DROPS = [
    "Developer, Rust",
    "Developer, Authorization",
    "Intermediate Software Engineer",
    "Full Stack Software Developer",
    "Firmware Engineer",
    "Software Engineer - Data Insights",
    "IOS Developer",
    "Data Engineer",
    "Analytics Engineer",
]


class OpenLevel(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_the_audited_titles_are_recovered(self):
        for title in AUDITED_DROPS:
            with self.subTest(title=title):
                matched, reason, kind = self.m.evaluate_full(
                    title, None, allow_open_level=True)
                self.assertTrue(matched, "%s (%s)" % (title, reason))
                self.assertEqual(kind, "open level")

    def test_they_stay_dropped_when_it_is_not_allowed(self):
        # This is the default, and it is what keeps the change contained to
        # Canadian boards rather than loosening all ~990 of them.
        for title in AUDITED_DROPS:
            with self.subTest(title=title):
                self.assertFalse(self.m.matches(title))

    def test_it_does_not_recover_senior_roles(self):
        for title in ("Senior Software Engineer", "Staff Developer, Growth",
                      "Principal Software Engineer", "Software Engineer II",
                      "Tech Lead Veeva Vault Developer",
                      "Manager, Software Engineering",
                      "Sr. Full-Stack Developer"):
            with self.subTest(title=title):
                self.assertFalse(
                    self.m.matches(title) or
                    self.m.evaluate_full(title, None, True)[0], title)

    def test_it_does_not_recover_non_engineering_roles(self):
        # The audit's largest bucket, 773 of them, and every one correctly
        # dropped. Widening the level must not widen the role gate.
        for title in ("Director, Product Marketing", "Senior Accountant, Tax",
                      "Manager, Security Incident Response",
                      "Senior Customer Success Manager"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(title, None, True)[0], title)

    def test_a_new_grad_title_is_still_new_grad_not_open_level(self):
        _, _, kind = self.m.evaluate_full(
            "New Grad Software Engineer", None, allow_open_level=True)
        self.assertEqual(kind, "new grad")

    def test_an_internship_is_still_an_internship(self):
        _, _, kind = self.m.evaluate_full(
            "Software Engineering Intern", None, allow_open_level=True)
        self.assertEqual(kind, "internship")

    def test_the_reason_says_which_tier_it_came_from(self):
        _, reason, _ = self.m.evaluate_full("Developer, Rust", None, True)
        self.assertIn("no seniority stated", reason)


class YearsOfExperience(unittest.TestCase):
    """A role that asks for at most a few years, read from its description.

    Stronger evidence than "no seniority stated", because the posting states
    the bar. Only the sources that return a description can supply it.
    """

    def setUp(self):
        self.m = Matcher(CFG)

    def test_reads_the_minimum_not_the_maximum(self):
        # "2+ years, 5 preferred" is reachable at two.
        self.assertEqual(matcher_min("You have 2 years experience, 5 preferred"), 2)
        self.assertEqual(matcher_min("1-3 years of relevant experience"), 1)
        self.assertEqual(matcher_min("0 to 2 years experience"), 0)

    def test_reads_a_floor_phrased_as_a_minimum(self):
        self.assertEqual(matcher_min("At least 3 years of professional experience."), 3)
        self.assertEqual(matcher_min("Minimum of 5 years of experience."), 5)

    def test_years_without_experience_nearby_are_not_a_requirement(self):
        # A description mentioning the company's age or its revenue history is
        # not stating a hiring bar.
        self.assertIsNone(matcher_min("Our company was founded 10 years ago."))
        self.assertIsNone(matcher_min("Revenue grew over the last 3 years."))

    def test_saying_nothing_is_not_zero(self):
        # Most postings say nothing. Reading that as zero would accept every
        # senior req whose description happens not to mention years.
        self.assertIsNone(matcher_min("A great opportunity on our platform team."))
        self.assertIsNone(matcher_min(""))
        self.assertIsNone(matcher_min(None))

    def test_a_role_asking_three_years_or_fewer_matches(self):
        for years, desc in ((0, "0 to 2 years experience"),
                            (1, "1-3 years of experience"),
                            (2, "2+ years of experience"),
                            (3, "At least 3 years of experience")):
            with self.subTest(desc=desc):
                matched, reason, kind = self.m.evaluate_full(
                    "Software Engineer", years=matcher_min(desc))
                self.assertTrue(matched, reason)
                self.assertEqual(kind, "0 to 3 years")
                self.assertIn("year", reason)

    def test_a_role_asking_more_is_still_dropped(self):
        for desc in ("5+ years of experience", "8 years of experience required",
                     "Minimum of 10 years of experience"):
            with self.subTest(desc=desc):
                self.assertFalse(self.m.evaluate_full(
                    "Software Engineer", years=matcher_min(desc))[0], desc)

    def test_the_title_gates_still_run_first(self):
        # A senior req asking for "1 year of leadership experience" must not
        # get in through the years door.
        years = matcher_min("3+ years experience; 1 year of leadership experience")
        self.assertEqual(years, 1)
        for title in ("Senior Software Engineer", "Staff Engineer",
                      "Engineering Manager", "Software Engineer III",
                      "Director, Product Marketing"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(title, years=years)[0], title)

    def test_a_new_grad_title_stays_new_grad(self):
        _, _, kind = self.m.evaluate_full("New Grad Software Engineer", years=2)
        self.assertEqual(kind, "new grad")

    def test_years_beat_open_level_when_both_apply(self):
        # Both would accept it; the one with stated evidence should label it.
        _, _, kind = self.m.evaluate_full(
            "Developer, Rust", None, allow_open_level=True, years=2)
        self.assertEqual(kind, "0 to 3 years")


class BroaderRoleWording(unittest.TestCase):
    """Development, product development, automation and infrastructure, in
    titles carrying none of engineer, developer or programmer.

    Those three words already cover DevOps Engineer, Cloud Engineer and Web
    Developer, so only the rest needed adding. The phrases are two words on
    purpose: a bare "automation" takes Marketing Automation Specialist.
    """

    def setUp(self):
        self.m = Matcher(CFG)
        self.years = matcher_min("We want 2+ years of experience.")

    def test_the_new_wordings_match(self):
        for title in ("Automation Specialist", "Infrastructure Analyst",
                      "Product Development Associate",
                      "Software Development Analyst", "DevOps Specialist",
                      "Site Reliability Analyst", "Systems Analyst",
                      "Applications Analyst", "Programmer Analyst",
                      "QA Automation Analyst", "Cloud Specialist",
                      "Test Automation Technician",
                      "Web Development Specialist"):
            with self.subTest(title=title):
                matched, reason, _ = self.m.evaluate_full(
                    title, None, False, self.years)
                self.assertTrue(matched, "%s (%s)" % (title, reason))

    def test_a_non_technical_automation_role_is_excluded(self):
        # The false positive the broadened list introduced, caught before it
        # shipped: "automation specialist" matches inside this.
        for title in ("Marketing Automation Specialist",
                      "Sales Automation Analyst",
                      "CRM Automation Specialist"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, False, self.years)[0], title)

    def test_a_coordinator_or_manager_is_still_not_a_role(self):
        for title in ("Infrastructure Project Coordinator",
                      "Automation Program Manager",
                      "Director of Product Development"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, False, self.years)[0], title)

    def test_seniority_still_applies_to_the_new_wordings(self):
        for title in ("Senior Automation Specialist",
                      "Automation Specialist III",
                      "Lead Infrastructure Analyst",
                      "Staff Site Reliability Analyst"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, False, self.years)[0], title)

    def test_the_new_wordings_still_need_an_early_career_signal(self):
        # Widening the role gate must not widen the level gates. With no years
        # stated and no new grad wording these stay out, except on Canadian
        # boards where open level is allowed.
        for title in ("Automation Specialist", "Infrastructure Analyst"):
            with self.subTest(title=title):
                self.assertFalse(self.m.matches(title), title)


class NonSoftwareEngineering(unittest.TestCase):
    """The cost of a bare "engineer" role keyword, trimmed on measurement.

    Once the 0 to 3 years tier widened the feed to 2405 roles, a sample found
    197 of them were non software engineering arriving through that keyword.
    These are all real titles from that sample.
    """

    def setUp(self):
        self.m = Matcher(CFG)
        self.years = matcher_min("2+ years of experience")

    def test_the_measured_noise_is_excluded(self):
        for title in ("Materials Engineer (New Grad Summer 2027)",
                      "Launch Fluids Engineer I",
                      "Supplier Quality Engineer - 2142",
                      "Quality & Continuous Improvement Engineer",
                      "Propulsion Engineer", "Avionics Engineer",
                      "Thermal Engineer", "Optical Engineer",
                      "Welding Engineer", "Composites Engineer"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, False, self.years)[0], title)

    def test_the_software_variant_of_each_survives(self):
        # The whole reason these are two word phrases. "avionics engineer" is
        # not adjacent in "Avionics Software Engineer", so it does not fire.
        for title in ("2027 Early Career Flight Software Engineer",
                      "Avionics Software Engineer",
                      "Materials Software Engineer",
                      "Propulsion Software Engineer"):
            with self.subTest(title=title):
                matched, reason, _ = self.m.evaluate_full(
                    title, None, False, self.years)
                self.assertTrue(matched, "%s (%s)" % (title, reason))

    def test_ordinary_software_titles_are_untouched(self):
        for title in ("Software Engineer, New Grad", "Firmware Engineer",
                      "New Grad Software Engineer", "Data Engineer"):
            with self.subTest(title=title):
                self.assertTrue(self.m.evaluate_full(
                    title, None, False, self.years)[0], title)


class RemoteScope(unittest.TestCase):
    """Where a remote role will actually hire, which is a different question
    from where it is listed."""

    def scope(self, locs, desc=""):
        return locations.remote_scope(locs, desc)

    def test_a_worldwide_remote_role_abroad_is_reachable(self):
        # The case the old filter threw away on its city.
        for locs, desc in (
            (["Berlin, Germany"], "Fully remote, we hire from any country."),
            (["Lisbon, Portugal"], "This is a fully remote role, work from anywhere."),
            (["Remote - Worldwide"], ""),
        ):
            with self.subTest(locs=locs):
                self.assertEqual(self.scope(locs, desc), locations.GLOBAL)
                region, _ = locations.classify(locs)
                self.assertTrue(locations.allowed(region, CFG, locations.GLOBAL))

    def test_a_remote_role_naming_canada_is_reachable(self):
        self.assertEqual(self.scope(["Remote - Canada"]), locations.CA_OK)

    def test_a_geo_locked_role_is_marked_locked(self):
        for locs, desc in (
            (["Remote"], "Fully remote. Must be located in the United States."),
            (["Remote"], "You must reside in the United States."),
            (["Remote, EMEA"], "EMEA only"),
            (["Bangalore, India"], "Fully remote, India only."),
        ):
            with self.subTest(desc=desc):
                self.assertEqual(self.scope(locs, desc), locations.LOCKED)

    def test_a_locked_foreign_role_is_still_dropped(self):
        region, _ = locations.classify(["Bangalore, India"])
        self.assertFalse(locations.allowed(region, CFG, locations.LOCKED))

    def test_an_on_site_foreign_role_is_still_dropped(self):
        for locs, desc in ((["Sydney, Australia"], "On site, Sydney office."),
                           (["London, UK"], "We support remote work on Fridays.")):
            with self.subTest(locs=locs):
                self.assertIsNone(self.scope(locs, desc))
                region, _ = locations.classify(locs)
                self.assertFalse(locations.allowed(region, CFG, None))

    def test_a_bare_remote_is_not_treated_as_open_to_canada(self):
        # "Remote" is the most over-claimed word on a job board and usually
        # means one country. Presenting a US-only role as Canada-open costs an
        # application, so it takes an explicit signal.
        self.assertEqual(self.scope(["Remote"]), locations.UNKNOWN)

    def test_the_word_remote_in_a_description_is_not_enough(self):
        # Half of all descriptions mention remote work as a perk. Only phrases
        # that describe the role itself count.
        self.assertIsNone(
            self.scope(["Munich, Germany"],
                       "We offer remote days and a remote friendly culture."))

    def test_a_restriction_beats_a_global_claim_in_the_same_posting(self):
        # A posting can say both. The restriction is the operative half.
        self.assertEqual(
            self.scope(["Remote"],
                       "Work from anywhere! Must be authorized to work in the US."),
            locations.LOCKED)

    def test_a_non_remote_role_has_no_scope(self):
        self.assertIsNone(self.scope(["Toronto, ON"]))


if __name__ == "__main__":
    unittest.main()
