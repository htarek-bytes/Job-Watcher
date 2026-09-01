"""Offline pipeline tests.

The SimplifyJobs parser is exercised against the real listings.json when a
local copy is available (set SIMPLIFY_FIXTURE), otherwise against a small
inline fixture with the same shape. The ATS parsers are tested against
recorded-shape payloads, since their live endpoints cannot be reached from a
test run.
"""

import json
import os
import sys
import tomllib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import locations
import net
import sources
from matcher import Matcher

with open(os.path.join(os.path.dirname(HERE), "config.toml"), "rb") as fh:
    CFG = tomllib.load(fh)

FIXTURE = os.environ.get("SIMPLIFY_FIXTURE")


class FakeResponse:
    def __init__(self, body, status=200):
        self.ok = True
        self.status = status
        self.body = body
        self.error = None
        self.etag = '"fixture"'
        self.seconds = 0.0
        self.not_modified = False

    def json(self):
        return json.loads(self.body)


class Patched:
    """Swap net.get for the duration of a block."""

    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def __enter__(self):
        self.orig = net.get
        net.get = lambda *a, **k: FakeResponse(self.body, self.status)
        return self

    def __exit__(self, *exc):
        net.get = self.orig


SIMPLIFY_INLINE = json.dumps([
    {"source": "Simplify", "company_name": "Acme", "id": "a1",
     "title": "New Grad Software Engineer 2027", "active": True, "is_visible": True,
     "date_posted": 1767841111, "url": "https://boards.greenhouse.io/acme/jobs/1",
     "locations": ["NYC"], "sponsorship": "Other"},
    {"source": "Simplify", "company_name": "Acme", "id": "a2",
     "title": "Senior Software Engineer", "active": True, "is_visible": True,
     "date_posted": 1767841111, "url": "https://x/2", "locations": ["SF"],
     "sponsorship": "Other"},
    {"source": "Simplify", "company_name": "Closed Co", "id": "a3",
     "title": "New Grad Software Engineer", "active": False, "is_visible": True,
     "date_posted": 1767841111, "url": "https://x/3", "locations": ["Toronto"],
     "sponsorship": "Other"},
    {"source": "Simplify", "company_name": "Overseas", "id": "a4",
     "title": "Graduate Software Engineer", "active": True, "is_visible": True,
     "date_posted": 1767841111, "url": "https://x/4", "locations": ["London, UK"],
     "sponsorship": "Other"},
])


class SimplifyParser(unittest.TestCase):
    def _fetch(self, body):
        with Patched(body):
            return sources.fetch_simplify(CFG["sources"]["simplify"])

    def test_inline_fixture(self):
        res = self._fetch(SIMPLIFY_INLINE)
        self.assertTrue(res.ok)
        # a3 is inactive and must be dropped by the fetcher itself.
        self.assertEqual({j["uid"].split(":")[-1] for j in res.jobs}, {"a1", "a2", "a4"})

    def test_uids_are_namespaced(self):
        res = self._fetch(SIMPLIFY_INLINE)
        self.assertTrue(all(j["uid"].startswith("simplify:listings:") for j in res.jobs))

    def test_posted_at_is_epoch_seconds(self):
        res = self._fetch(SIMPLIFY_INLINE)
        for job in res.jobs:
            self.assertIsInstance(job["posted_at"], int)
            self.assertGreater(job["posted_at"], 1_000_000_000)
            self.assertLess(job["posted_at"], 3_000_000_000)

    @unittest.skipUnless(FIXTURE and os.path.exists(FIXTURE or ""),
                         "set SIMPLIFY_FIXTURE to the real listings.json")
    def test_real_listings_file(self):
        with open(FIXTURE, "r", encoding="utf-8") as fh:
            body = fh.read()
        res = self._fetch(body)
        self.assertTrue(res.ok)
        self.assertGreater(len(res.jobs), 100)

        matcher = Matcher(CFG)
        kept = []
        for job in res.jobs:
            if not matcher.matches(job["title"]):
                continue
            region, _ = locations.classify(job["locations"])
            if locations.allowed(region, CFG):
                kept.append((job["title"], region))

        # Sanity: the filter must be selective but not empty.
        self.assertGreater(len(kept), 0, "filter rejected every real posting")
        self.assertLess(len(kept), len(res.jobs), "filter kept everything")
        for title, _ in kept:
            self.assertFalse(matcher.evaluate(title)[0] is False)


GREENHOUSE_BODY = json.dumps({"jobs": [
    {"id": 5, "title": "Software Engineer I", "absolute_url": "https://gh/5",
     "location": {"name": "Toronto, Canada"}, "offices": [{"name": "Toronto"}],
     "first_published": "2026-08-30T10:00:00Z",
     "content": "&lt;p&gt;We do not offer sponsorship&lt;/p&gt;"},
]})

LEVER_BODY = json.dumps([
    {"id": "abc", "text": "New Grad Software Engineer", "hostedUrl": "https://lev/abc",
     "categories": {"location": "San Francisco"}, "createdAt": 1767841111000,
     "descriptionPlain": "body", "lists": [{"content": "<li>reqs</li>"}]},
])

ASHBY_BODY = json.dumps({"name": "Acme", "jobs": [
    {"id": "z9", "title": "Software Engineer, New Grad", "jobUrl": "https://ash/z9",
     "location": "Remote", "secondaryLocations": [{"location": "NYC"}],
     "publishedAt": "2026-08-31T00:00:00Z", "descriptionHtml": "<p>hi</p>"},
]})


class AtsParsers(unittest.TestCase):
    def test_greenhouse(self):
        with Patched(GREENHOUSE_BODY):
            res = sources.fetch_greenhouse("acme")
        self.assertTrue(res.ok)
        job = res.jobs[0]
        self.assertEqual(job["uid"], "greenhouse:acme:5")
        self.assertEqual(job["url"], "https://gh/5")
        self.assertIn("Toronto", job["locations"][0])
        self.assertEqual(locations.classify(job["locations"])[0], "CA")

    def test_lever(self):
        with Patched(LEVER_BODY):
            res = sources.fetch_lever("acme")
        job = res.jobs[0]
        self.assertEqual(job["uid"], "lever:acme:abc")
        # createdAt arrives in milliseconds and must be normalized.
        self.assertLess(job["posted_at"], 3_000_000_000)

    def test_ashby(self):
        with Patched(ASHBY_BODY):
            res = sources.fetch_ashby("acme")
        job = res.jobs[0]
        self.assertEqual(job["company"], "Acme")
        self.assertEqual(locations.classify(job["locations"])[0], "US")

    def test_failure_is_returned_not_raised(self):
        orig = net.get
        net.get = lambda *a, **k: net.Response(ok=False, status=404, error="HTTP 404")
        try:
            res = sources.fetch_greenhouse("nope")
            self.assertFalse(res.ok)
            self.assertEqual(res.error, "HTTP 404")
            self.assertEqual(res.jobs, [])
        finally:
            net.get = orig

    def test_amazon_failure_is_isolated(self):
        orig = net.get

        def boom(*a, **k):
            raise RuntimeError("amazon changed its schema")

        net.get = boom
        try:
            res = sources.fetch_amazon("swe")
            self.assertFalse(res.ok)
            self.assertIn("isolated failure", res.error)
        finally:
            net.get = orig


class Locations(unittest.TestCase):
    def test_regions(self):
        cases = [
            (["Toronto, ON"], "CA"),
            (["Vancouver, British Columbia"], "CA"),
            (["San Francisco, CA"], "US"),
            (["NYC"], "US"),
            (["Seattle, WA"], "US"),
            (["London, UK"], "OTHER"),
            (["Bengaluru, India"], "OTHER"),
            (["Remote"], "REMOTE"),
            ([], "UNKNOWN"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(locations.classify(value)[0], expected)

    def test_multi_location_keeps_the_reachable_one(self):
        region, evidence = locations.classify(["London, UK", "Toronto, ON"])
        self.assertEqual(region, "CA")
        self.assertIn("Toronto", evidence)

    def test_allowed_respects_config(self):
        self.assertTrue(locations.allowed("US", CFG))
        self.assertTrue(locations.allowed("CA", CFG))
        self.assertTrue(locations.allowed("REMOTE", CFG))
        self.assertFalse(locations.allowed("OTHER", CFG))


if __name__ == "__main__":
    unittest.main(verbosity=2)
