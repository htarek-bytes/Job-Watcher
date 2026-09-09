"""The locations field, and the bug that made it 2.1 MB.

Lever's additionalPlain is a plain-text STRING. The Lever parser did list() on
it, which spreads a string into one entry per character, so 231 roles arrived
carrying their whole description letter by letter: 407,460 single-character
entries, 2.1 MB of a 7.1 MB feed downloaded on every dashboard poll, and a
location classifier being asked to place the letter "o".

The call site is fixed. The guard exists so that none of the thirteen places
that build a job can do it again.
"""

import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import sources


class CleanLocations(unittest.TestCase):
    def test_a_bare_string_is_wrapped_not_spread(self):
        # The whole bug in one line.
        self.assertEqual(sources.clean_locations("Toronto, ON"), ["Toronto, ON"])

    def test_single_characters_are_dropped(self):
        spread = list("Compensation: $95,000")
        self.assertEqual(sources.clean_locations(["Toronto, ON"] + spread),
                         ["Toronto, ON"])

    def test_the_list_is_capped(self):
        many = ["City %d" % i for i in range(200)]
        self.assertEqual(len(sources.clean_locations(many)), sources.MAX_LOCATIONS)

    def test_empties_and_non_strings_go(self):
        self.assertEqual(
            sources.clean_locations(["Toronto", "", None, 42, {"a": 1}, "  "]),
            ["Toronto"])

    def test_duplicates_collapse(self):
        self.assertEqual(sources.clean_locations(["Remote", "Remote", "Toronto"]),
                         ["Remote", "Toronto"])

    def test_real_locations_survive_untouched(self):
        real = ["Toronto, ON", "Remote - Canada", "San Francisco, CA"]
        self.assertEqual(sources.clean_locations(real), real)

    def test_none_is_an_empty_list(self):
        self.assertEqual(sources.clean_locations(None), [])


class LeverParser(unittest.TestCase):
    """The call site itself, against a payload shaped like Lever's."""

    def payload(self):
        return [{
            "id": "abc-123",
            "text": "Software Engineer, New Grad",
            "hostedUrl": "https://jobs.lever.co/acme/abc-123",
            "createdAt": 1757000000000,
            "categories": {"location": "Toronto, ON",
                           "allLocations": ["Toronto, ON", "Remote - Canada"]},
            # A STRING, which is what it really is.
            "additionalPlain": "Compensation\nThe salary range is $95,000 - $120,000 CAD.",
            "descriptionPlain": "Build things with us.",
            "lists": [{"content": "<li>Write code</li>"}],
        }]

    def parse(self):
        class FakeHTTP:
            def get(self, url, etag=None, **kw):
                class R:
                    ok, status, seconds, error, not_modified = True, 200, 0.1, None, False
                    body = json.dumps(LeverParser().payload())
                    def json(self):
                        return json.loads(self.body)
                return R()
        real = sources._http
        sources._http = FakeHTTP()
        try:
            return sources.fetch_lever("acme")
        finally:
            sources._http = real

    def test_the_description_does_not_leak_into_locations(self):
        job = self.parse().jobs[0]
        self.assertEqual(job["locations"], ["Toronto, ON", "Remote - Canada"])

    def test_all_locations_is_extended_not_nested(self):
        # A list nested inside the list is dropped by clean_locations, which
        # would have been a quieter version of the same mistake.
        job = self.parse().jobs[0]
        self.assertIn("Remote - Canada", job["locations"])
        self.assertTrue(all(isinstance(x, str) for x in job["locations"]))

    def test_additional_text_goes_into_the_description_where_it_is_useful(self):
        # Lever boards routinely put the compensation range in this field, and
        # the salary parser reads descriptions.
        job = self.parse().jobs[0]
        self.assertIn("95,000", job["description"])

    def test_and_the_salary_parser_can_read_it(self):
        import salary
        job = self.parse().jobs[0]
        pay = salary.parse(job["description"], "CA")
        self.assertIsNotNone(pay, "the compensation line was not readable")
        self.assertEqual((pay["min"], pay["max"]), (95000, 120000))


class CarryDoesNotPropagateTheBug(unittest.TestCase):
    """A fix to the shape of a field has to reach roles already in the feed.

    Fixing the parser fixed only the boards the rotation happened to reach.
    231 roles sat in the feed with 407,460 junk entries between them, healing
    over half an hour if their board answered 200, and never if it answered
    304, because carry copies the stored role verbatim.

    So the board here answers 304, which is the case where it matters most.
    """

    def setUp(self):
        import cli
        self.cli = cli
        # Swapped for the duration and put back in tearDown. Leaving it swapped
        # broke ten tests in two other files that happen to run after this one
        # alphabetically, which was a good deal harder to read than the failure
        # it was hiding.
        self.real_sources = cli.sources

    def tearDown(self):
        self.cli.sources = self.real_sources

    def carried(self, locations):
        from test_carry import CFG, FakeSources, _job as carry_job
        board = ("greenhouse", "acme")
        self.cli.sources = FakeSources({board: "304"})
        job = carry_job(*board, "1")
        job["locations"] = locations
        job["confirmed_at"] = int(time.time())
        jobs, _ = self.cli.sweep(CFG, {}, {}, quiet=True, previous=[job])
        found = [j for j in jobs if j["uid"] == job["uid"]]
        self.assertEqual(len(found), 1, "the role was not carried at all")
        return found[0]

    def test_a_carried_role_is_cleaned_on_the_way_through(self):
        job = self.carried(["Toronto, ON"] + list("Compensation is great"))
        self.assertEqual(job["locations"], ["Toronto, ON"])

    def test_a_healthy_carried_role_is_untouched(self):
        job = self.carried(["Toronto, ON", "Remote - Canada"])
        self.assertEqual(job["locations"], ["Toronto, ON", "Remote - Canada"])


class FeedSize(unittest.TestCase):
    def test_a_role_cannot_carry_an_unbounded_location_list(self):
        # The feed is fetched by a phone. 2.1 MB of spread strings is not a
        # cosmetic problem.
        job = sources._job("lever", "acme", "1", company="acme", title="t",
                           url="u", locations=list("a very long description"))
        self.assertEqual(job["locations"], [])


if __name__ == "__main__":
    unittest.main()
