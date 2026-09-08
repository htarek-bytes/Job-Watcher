"""The infrastructure track: twelve job families treated as one labour market.

Systems administration, systems analysis, infrastructure, networking, IT
operations, cloud and platform operations, security operations, OT and SCADA,
telecommunications, rail and transportation technology, energy and utilities
technology, and public sector IT.

Two things make this track different from the software one, and both are the
point rather than an accident:

* It is not gated on the word junior. A Systems Administrator posting almost
  never says "new grad", so requiring it would empty the track. The level is
  still recorded, so "level not stated" stays filterable.
* Its permissive tier is held to Canada. Restricting the WHOLE track was tried
  first and measured against the live feed before shipping: it would have
  dropped 176 roles the board already carried, 174 of which held a real early
  career signal. Only the open level tier needed holding back.
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
    match = dict(cfg["match"])
    match["infrastructure"] = dict(match["infrastructure"], enabled=False)
    return dict(cfg, match=match)


class Families(unittest.TestCase):
    """One title from each family the request named."""

    def setUp(self):
        self.m = Matcher(CFG)

    def test_every_family_is_reachable(self):
        for family, title in (
            ("systems administration", "Systems Administrator"),
            ("systems administration", "Linux Systems Administrator"),
            ("systems analysis", "Information Systems Analyst"),
            ("systems analysis", "Technical Systems Analyst"),
            ("infrastructure", "IT Infrastructure Analyst"),
            ("infrastructure", "Infrastructure Operations Specialist"),
            ("networking", "Network Operations Analyst"),
            ("networking", "NOC Technician"),
            ("it operations", "Production Support Analyst"),
            ("it operations", "Application Support Analyst"),
            ("cloud and platform", "Cloud Operations Specialist"),
            ("cloud and platform", "Platform Operations Specialist"),
            ("security operations", "SOC Analyst"),
            ("security operations", "Information Security Analyst"),
            ("ot and scada", "SCADA Systems Analyst"),
            ("ot and scada", "Operational Technology Analyst"),
            ("ot and scada", "Industrial Cybersecurity Analyst"),
            ("ot and scada", "Control Systems Analyst"),
            ("ot and scada", "Telemetry Specialist"),
            ("telecommunications", "Telecommunications Systems Specialist"),
            ("telecommunications", "Wireless Network Specialist"),
            ("rail and transport", "Railway Signal Systems Analyst"),
            ("rail and transport", "Train Control Systems Specialist"),
            ("rail and transport", "Transportation Systems Analyst"),
        ):
            with self.subTest(family=family, title=title):
                self.assertEqual(self.m.track(title), m.INFRASTRUCTURE, title)
                matched, reason, _ = self.m.evaluate_full(title)
                self.assertTrue(matched, "%s (%s)" % (title, reason))

    def test_the_french_wordings_match_with_their_accents(self):
        # Job Bank and Jobillico are bilingual and a French search returns
        # French titles. Before accent folding these failed the role gate
        # outright, so every French query was spending requests for nothing.
        for title in ("Analyste de systèmes", "Administrateur de systèmes",
                      "Analyste réseau", "Spécialiste en cybersécurité",
                      "Technicien en informatique"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.INFRASTRUCTURE, title)
                self.assertTrue(self.m.evaluate_full(title)[0], title)


class NotGatedOnJunior(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_a_plain_title_is_accepted_with_no_early_career_word(self):
        for title in ("Systems Administrator", "Network Analyst",
                      "SCADA Specialist", "IT Operations Analyst"):
            with self.subTest(title=title):
                matched, reason, kind = self.m.evaluate_full(title)
                self.assertTrue(matched, "%s (%s)" % (title, reason))
                self.assertEqual(kind, m.OPEN_LEVEL)

    def test_two_years_preferred_is_not_a_rejection(self):
        years = min_years("2+ years of experience preferred")
        matched, _, kind = self.m.evaluate_full("Systems Analyst", years=years)
        self.assertTrue(matched)
        self.assertEqual(kind, m.JUNIOR)

    def test_four_years_is_reachable_here_but_not_on_the_software_track(self):
        # Per-track, so the software side keeps its three year ceiling.
        years = min_years("4 years of experience required")
        self.assertTrue(self.m.evaluate_full("Network Administrator",
                                             years=years)[0])
        self.assertFalse(self.m.evaluate_full("Software Engineer",
                                              years=years)[0])

    def test_five_or_more_years_is_still_excluded(self):
        for desc in ("5+ years of experience", "7 years of experience required",
                     "Minimum of 10 years of experience"):
            with self.subTest(desc=desc):
                self.assertFalse(self.m.evaluate_full(
                    "Systems Administrator", None, True,
                    min_years(desc))[0], desc)

    def test_seniority_still_applies(self):
        for title in ("Senior Systems Administrator", "Lead Network Analyst",
                      "Principal Systems Engineer", "Systems Analyst III",
                      "Manager, IT Operations", "Director of Infrastructure",
                      "Staff Security Analyst"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, True, 1)[0], title)


class KeptOut(unittest.TestCase):
    def setUp(self):
        self.m = Matcher(CFG)

    def test_the_low_end_it_job_is_excluded(self):
        # Carries the right words and none of the substance, and is capped
        # well under the target.
        for title in ("Help Desk Technician", "Desktop Support Specialist",
                      "IT Support Technician", "Computer Repair Technician",
                      "Service Desk Technician", "PC Technician",
                      "Call Centre IT Support"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, True, 1)[0], title)

    def test_the_field_trade_is_excluded(self):
        # Where the technology is incidental to physical labour. A
        # Telecommunications Technician climbing poles is not the same job as
        # one running a network.
        for title in ("Fibre Splicer", "Tower Technician", "Cable Technician",
                      "Line Technician", "Installation Technician",
                      "Field Service Technician", "Millwright",
                      "Industrial Electrician"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, True, 1)[0], title)

    def test_the_non_technical_role_is_excluded(self):
        for title in ("Business Analyst", "Senior Business Analyst",
                      "IT Project Manager", "Program Manager, Infrastructure",
                      "Scrum Master", "Data Entry Clerk",
                      "Technical Account Manager"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, True, 1)[0], title)

    def test_the_audited_noise_is_excluded(self):
        """Real titles from the first audit of the live track, 441 rows.

        37 of them rested on a bare "operations analyst" alone, and that
        keyword was redundant as well as wrong: every qualified form it was
        meant to catch was already listed. An internal HR or recruiting system
        is a system, and administering it is not this job.
        """
        for title in ("Loss Prevention & Operations Analyst",
                      "Marketing Operations Analyst (RevOps)",
                      "Procurement Operations Analyst",
                      "Revenue Operations Analyst",
                      "Business Operations Analyst",
                      "Trading Operations Analyst",
                      "HR Operations Analyst",
                      "People Systems Analyst",
                      "Recruiting Systems Specialist",
                      "Electrical Design Engineer New Grad"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, True, 1)[0], title)

    def test_the_qualified_operations_wordings_still_match(self):
        # Dropping the bare keyword must not drop the real ones with it.
        for title in ("IT Operations Analyst", "Network Operations Analyst",
                      "Security Operations Analyst",
                      "Infrastructure Operations Analyst",
                      "Cloud Operations Analyst",
                      "Site Reliability Operations Analyst"):
            with self.subTest(title=title):
                self.assertTrue(self.m.evaluate_full(title)[0], title)

    def test_seniority_in_french_is_excluded_too(self):
        # Job Bank and Jobillico are bilingual, so an English-only seniority
        # list was doing half its job: this one walked straight into the feed.
        for title in ("Directeur Gouvernance TI & Cybersécurité",
                      "Gestionnaire, Infrastructure technologique",
                      "Chef d'équipe, Administrateur de systèmes",
                      "Architecte de systèmes",
                      "Développeur logiciel sénior, DevOps"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, True, 1)[0], title)

    def test_the_c_level_is_excluded_but_officer_is_not(self):
        # "Officer" is a mid-level classification right across the Canadian
        # public service, and this track exists to find those roles, so only
        # "chief" is excluded.
        self.assertFalse(self.m.evaluate_full(
            "Chief Information Security Officer (CISO)", None, True, 1)[0])
        for title in ("Information Technology Officer", "Systems Officer",
                      "Computer Systems Analyst"):
            with self.subTest(title=title):
                self.assertTrue(self.m.evaluate_full(title)[0], title)

    def test_a_role_needing_a_licence_is_excluded(self):
        # A professional engineering licence is a multi-year credential, not a
        # preference, so the posting is not reachable.
        for title in ("Control Systems Engineer, P.Eng",
                      "Electrical Engineer, Substation Automation",
                      "Engineer In Training, Systems"):
            with self.subTest(title=title):
                self.assertFalse(self.m.evaluate_full(
                    title, None, True, 1)[0], title)


class TrackBoundary(unittest.TestCase):
    """Where infrastructure ends and the other two tracks begin."""

    def setUp(self):
        self.m = Matcher(CFG)

    def test_an_unambiguously_software_title_stays_software(self):
        for title in ("Software Systems Analyst", "Software Engineer",
                      "Backend Engineer", "Data Engineer",
                      "Machine Learning Engineer", "Firmware Engineer",
                      "Programmer Analyst"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.SOFTWARE, title)

    def test_the_software_engineering_operations_roles_stay_software(self):
        # Deliberately not taken. These are software engineering jobs at a
        # technology company, and moving them would empty them out of the
        # software filter, which is not what was asked for.
        for title in ("DevOps Engineer", "Site Reliability Engineer",
                      "Platform Engineer", "Security Engineer",
                      "Infrastructure Software Engineer"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.SOFTWARE, title)

    def test_the_engineer_keeps_its_track_when_the_analyst_changes_track(self):
        # The pair the track boundary is easiest to get wrong on. Listing
        # "devops analyst" here must move the analyst and leave the engineer
        # alone, and the same for SRE. An earlier derived version of the rule
        # got this backwards in one direction and then the other.
        for engineer, analyst in (("DevOps Engineer", "DevOps Analyst"),
                                  ("Site Reliability Engineer", "SRE Analyst")):
            with self.subTest(pair=engineer):
                self.assertEqual(self.m.track(engineer), m.SOFTWARE, engineer)
                self.assertEqual(self.m.track(analyst), m.INFRASTRUCTURE, analyst)

    def test_the_operations_cousins_do_come_here(self):
        for title in ("DevOps Analyst", "SRE Analyst",
                      "Platform Operations Specialist",
                      "Security Operations Analyst"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.INFRASTRUCTURE, title)

    def test_the_titles_it_takes_from_software_are_taken_cleanly(self):
        # These sat in the software list only because an earlier widening put
        # them there when there was nowhere else for them to go.
        for title in ("Systems Analyst", "Infrastructure Analyst",
                      "Infrastructure Specialist", "Cloud Specialist",
                      "Automation Specialist", "Applications Analyst"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.INFRASTRUCTURE, title)

    def test_a_sales_title_still_goes_to_the_sales_track(self):
        for title in ("Sales Engineer", "Solutions Consultant"):
            with self.subTest(title=title):
                self.assertEqual(self.m.track(title), m.PRESALES, title)


class OffByDefault(unittest.TestCase):
    def test_disabling_it_puts_the_titles_back_where_they_were(self):
        off = Matcher(_off(CFG))
        # Back on the software track, and back to needing a signal.
        self.assertEqual(off.track("Systems Analyst"), m.SOFTWARE)
        self.assertFalse(off.evaluate_full("Systems Administrator")[0])
        # And the ones software never had stay out entirely.
        self.assertFalse(off.evaluate_full("SCADA Specialist", None, True)[0])


class OpenLevelIsHeldToCanada(unittest.TestCase):
    """The region rule lives in cli.py, but the track has to declare it."""

    def setUp(self):
        self.m = Matcher(CFG)

    def test_the_track_declares_the_restriction(self):
        track = self.m.by_track[m.INFRASTRUCTURE]
        self.assertEqual(track.open_level_regions, ["CA"])

    def test_the_other_tracks_do_not(self):
        self.assertEqual(self.m.by_track[m.PRESALES].open_level_regions, [])
        self.assertEqual(self.m.software.open_level_regions, [])

    def test_only_the_open_level_tier_is_meant_to_be_held(self):
        # The measurement that produced the rule: a US posting saying new grad
        # or stating nought to three years is not open level, so it is not
        # affected. 174 of the 176 rows at stake were in this position.
        _, _, kind = self.m.evaluate_full("Junior Systems Analyst")
        self.assertEqual(kind, m.NEW_GRAD)
        _, _, kind = self.m.evaluate_full("Systems Analyst", years=2)
        self.assertEqual(kind, m.JUNIOR)


if __name__ == "__main__":
    unittest.main()
