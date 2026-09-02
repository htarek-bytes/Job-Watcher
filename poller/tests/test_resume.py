import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import resume


class SkillExtraction(unittest.TestCase):
    def test_finds_plain_technologies(self):
        found = resume.extract(
            "You will write Python and Java services, deploy on Kubernetes, "
            "and query PostgreSQL."
        )
        self.assertEqual(found, {"Python", "Java", "Kubernetes", "PostgreSQL"})

    def test_java_does_not_match_javascript(self):
        self.assertNotIn("Java", resume.extract("Strong JavaScript experience required."))
        self.assertIn("JavaScript", resume.extract("Strong JavaScript experience required."))

    def test_bare_go_does_not_fire_on_the_english_verb(self):
        # The whole reason Go is case-sensitive and context-bound.
        for prose in ("You will go above and beyond for customers.",
                      "Projects go from design to launch in weeks.",
                      "we want people who go deep"):
            with self.subTest(prose=prose):
                self.assertNotIn("Go", resume.extract(prose))

    def test_go_is_found_when_it_is_the_language(self):
        self.assertIn("Go", resume.extract("Experience with Go, Rust or C++."))
        self.assertIn("Go", resume.extract("Backend written in Golang."))

    def test_bare_c_needs_a_language_context(self):
        self.assertNotIn("C", resume.extract("A C-level executive sponsor."))
        self.assertIn("C", resume.extract("Systems programming in C/C++."))

    def test_cplusplus_and_csharp(self):
        self.assertIn("C++", resume.extract("Modern C++17 codebase."))
        self.assertIn("C#", resume.extract("Our stack is C# and .NET."))

    def test_empty_input(self):
        self.assertEqual(resume.extract(""), set())
        self.assertEqual(resume.extract(None), set())


class Gap(unittest.TestCase):
    def test_gap_is_what_you_do_not_claim(self):
        have = resume.profile_skills({"technical": ["Java", "Python"]})
        self.assertEqual(resume.gap({"Java", "Kafka", "Python"}, have), ["Kafka"])

    def test_also_known_counts_as_claimed(self):
        have = resume.profile_skills({"technical": ["Java"], "also_known": ["Kafka"]})
        self.assertEqual(resume.gap({"Java", "Kafka"}, have), [])

    def test_comparison_is_case_insensitive(self):
        have = resume.profile_skills({"technical": ["postgresql"]})
        self.assertEqual(resume.gap({"PostgreSQL"}, have), [])


class Demand(unittest.TestCase):
    def test_ranked_by_role_count_and_flags_cv_coverage(self):
        have = resume.profile_skills({"technical": ["Java"]})
        rows = resume.demand({"Java": 5, "Kafka": 9}, have, 10)
        self.assertEqual(rows[0]["skill"], "Kafka")
        self.assertEqual(rows[0]["roles"], 9)
        self.assertEqual(rows[0]["pct"], 90.0)
        self.assertFalse(rows[0]["on_cv"])
        self.assertTrue(rows[1]["on_cv"])

    def test_no_roles_does_not_divide_by_zero(self):
        self.assertEqual(resume.demand({"Java": 0}, set(), 0)[0]["pct"], 0.0)


class BulletLint(unittest.TestCase):
    GOOD = ("Cut container shutdown from a 10 second grace period to 3 milliseconds "
            "by closing a race between fork and signal handler installation, "
            "verified across 13 automated tests")

    def test_a_good_bullet_has_no_findings(self):
        self.assertEqual(resume.lint_bullet(self.GOOD), [])

    def test_missing_number_is_flagged(self):
        text = ("Debugged production issues end to end with no one to escalate to, "
                "moving from an ambiguous report through root cause to a documented "
                "fix for the client")
        self.assertTrue(any("no number" in f for f in resume.lint_bullet(text)))

    def test_number_not_at_the_end_is_flagged(self):
        text = ("Cut 45 percent of page weight and then went on to rewrite the "
                "templating layer, the routing table and the asset pipeline for "
                "the client platform")
        self.assertTrue(any("end on the outcome" in f for f in resume.lint_bullet(text)))

    def test_weak_opener_is_flagged(self):
        text = ("Responsible for maintaining the deployment pipeline across 3 "
                "environments and 12 services, reducing failed releases by 40 percent "
                "over 6 months")
        self.assertTrue(any("weak opener" in f for f in resume.lint_bullet(text)))

    def test_first_person_is_flagged(self):
        text = ("Built a REST API over a PostgreSQL schema I modeled and indexed, "
                "containerised each tier and brought the stack up in 1 command "
                "across 3 services")
        self.assertTrue(any("first person" in f for f in resume.lint_bullet(text)))

    def test_length_bounds(self):
        self.assertTrue(any("too short" in f for f in resume.lint_bullet("Shipped 3 things")))
        self.assertTrue(any("too long" in f
                            for f in resume.lint_bullet("Shipped " + "word " * 40 + "5 times")))


class BulletParsing(unittest.TestCase):
    def test_splits_on_bullet_characters_and_rejoins_wrapped_lines(self):
        text = ("• Built a thing that does something useful across 3 services and "
                "5 environments\nreducing latency by 40 percent\n"
                "• Shipped another thing with 12 tests and 2 alert rules in place "
                "for the team")
        self.assertEqual(len(resume.parse_bullets(text)), 2)

    def test_skips_skill_dumps_and_coursework(self):
        text = ("• Technical skills: Java, JavaScript, Python, C, C++, SQL, Bash\n"
                "• Coursework: data structures and algorithms, operating systems\n"
                "• Built a real thing with 5 components and 3 services for the client team")
        self.assertEqual(len(resume.parse_bullets(text)), 1)

    def test_dash_bullets(self):
        text = ("- Built a thing that does something useful across 3 services and 5 "
                "environments for the team")
        self.assertEqual(len(resume.parse_bullets(text)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
