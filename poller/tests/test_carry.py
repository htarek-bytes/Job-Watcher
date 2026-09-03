"""The feed must be every open role, not the roles on the boards polled last.

The reported symptom was a role pushed to the phone and then missing from the
dashboard, still missing after a refresh. The cause was in `sweep`: it built
the job list only from the boards the rotation selected, so at ~987 boards and
80 a sweep, a role on a cold board was in jobs.json for one sweep in twelve and
absent for the other eleven. jobs.json is overwritten each sweep, so the role
did not reappear until its board came round again.

These tests run a whole sweep against fake sources, because that is the level
the bug lived at: every individual piece behaved correctly.
"""

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import cli
import sources


def _job(source, slug, uid, title="Software Engineer, New Grad",
         locations=("Toronto, ON",), confirmed_at=None):
    job = {
        "uid": "%s:%s:%s" % (source, slug, uid),
        "source": source, "slug": slug,
        "company": slug, "title": title,
        "url": "https://example.com/%s/%s" % (slug, uid),
        "locations": list(locations),
        "posted_at": int(time.time()) - 600,
    }
    if confirmed_at is not None:
        job["confirmed_at"] = confirmed_at
    return job


class FakeSources:
    """Stands in for the sources module inside a sweep.

    `answers` maps (source, key) to "jobs", "304", or "fail". Anything not
    named is a board the rotation may or may not reach; the point of most of
    these tests is what happens to the ones it does not.
    """

    GREENHOUSE = sources.GREENHOUSE
    SIMPLIFY = sources.SIMPLIFY
    AMAZON = sources.AMAZON
    SourceResult = sources.SourceResult

    def __init__(self, answers):
        self.answers = answers
        self.fetched = []

    def fetch(self, source, key, cfg, etag=None):
        self.fetched.append((source, key))
        result = sources.SourceResult(source, key)
        answer = self.answers.get((source, key), "jobs")
        if answer == "fail":
            result.error = "HTTP 503"
            return result
        result.ok = True
        if answer == "304":
            result.not_modified = True
            return result
        result.jobs = [_job(source, key, "1")]
        return result

    def iter_configured(self, cfg, registry=None):
        return list(self.answers)


CFG = {
    "match": {
        "max_level": 1,
        "role_keywords": ["software engineer"],
        "new_grad_phrases": ["new grad"],
        "exclude_keywords": ["senior", "intern"],
    },
    "locations": {"countries": ["US", "CA"], "allow_remote": True,
                  "allow_unknown": True},
    "rotation": {"cold_slice": 1, "hot_days": 21},
    "discovery": {"enabled": False},
    "sources": {},
}


class SweepCase(unittest.TestCase):
    def setUp(self):
        self.real = cli.sources
        self.addCleanup(setattr, cli, "sources", self.real)

    def run_sweep(self, answers, previous=None, registry=None, health=None):
        fake = FakeSources(answers)
        cli.sources = fake
        jobs, _ = cli.sweep(CFG, health if health is not None else {},
                            registry if registry is not None else {},
                            quiet=True, previous=previous)
        return jobs, fake


class TestUnpolledBoards(SweepCase):
    """Four boards, a cold slice of one. Three sit out every sweep."""

    ANSWERS = {("greenhouse", "a"): "jobs", ("greenhouse", "b"): "jobs",
               ("greenhouse", "c"): "jobs", ("greenhouse", "d"): "jobs"}

    def test_the_rotation_really_does_skip_boards(self):
        # If this fails the rest of the class proves nothing.
        _, fake = self.run_sweep(self.ANSWERS)
        self.assertLess(len(fake.fetched), len(self.ANSWERS))

    def test_a_role_on_an_unpolled_board_stays_in_the_feed(self):
        previous = [_job("greenhouse", slug, "1") for slug in "abcd"]
        jobs, _ = self.run_sweep(self.ANSWERS, previous=previous)
        self.assertEqual(len(jobs), 4, [j["uid"] for j in jobs])

    def test_every_board_is_represented_after_one_sweep(self):
        previous = [_job("greenhouse", slug, "1") for slug in "abcd"]
        jobs, _ = self.run_sweep(self.ANSWERS, previous=previous)
        self.assertEqual({j["slug"] for j in jobs}, set("abcd"))

    def test_the_feed_does_not_shrink_sweep_after_sweep(self):
        # The actual reported behaviour: run several sweeps in a row, feeding
        # each one the previous output, and watch the feed stay whole.
        previous = [_job("greenhouse", slug, "1") for slug in "abcd"]
        health = {}
        for _ in range(6):
            fake = FakeSources(self.ANSWERS)
            cli.sources = fake
            previous, _ = cli.sweep(CFG, health, {}, quiet=True, previous=previous)
            self.assertEqual(len(previous), 4, [j["uid"] for j in previous])

    def test_a_polled_board_is_still_authoritative(self):
        # Carrying must not resurrect a role the board itself dropped. The
        # board answers with one job; a second, older one is gone for good.
        previous = [_job("greenhouse", "a", "1"), _job("greenhouse", "a", "2")]
        jobs, fake = self.run_sweep({("greenhouse", "a"): "jobs"},
                                    previous=previous)
        self.assertIn(("greenhouse", "a"), fake.fetched)
        self.assertEqual([j["uid"] for j in jobs], ["greenhouse:a:1"])


class TestFailedAndUnchanged(SweepCase):
    def test_a_failed_fetch_does_not_delete_the_roles(self):
        previous = [_job("greenhouse", "a", "1")]
        jobs, _ = self.run_sweep({("greenhouse", "a"): "fail"}, previous=previous)
        self.assertEqual([j["uid"] for j in jobs], ["greenhouse:a:1"])

    def test_a_304_does_not_delete_the_roles(self):
        previous = [_job("greenhouse", "a", "1")]
        jobs, _ = self.run_sweep({("greenhouse", "a"): "304"}, previous=previous)
        self.assertEqual([j["uid"] for j in jobs], ["greenhouse:a:1"])

    def test_an_empty_first_run_is_still_empty(self):
        jobs, _ = self.run_sweep({("greenhouse", "a"): "fail"}, previous=[])
        self.assertEqual(jobs, [])


class TestExpiry(SweepCase):
    """A carried role cannot live forever, or a deleted board keeps its roles."""

    def test_a_stale_role_ages_out(self):
        old = int(time.time()) - cli.CARRY_MAX_SECONDS - 60
        previous = [_job("greenhouse", "a", "1", confirmed_at=old)]
        jobs, _ = self.run_sweep({("greenhouse", "a"): "fail"}, previous=previous)
        self.assertEqual(jobs, [])

    def test_a_recent_role_survives(self):
        recent = int(time.time()) - 3600
        previous = [_job("greenhouse", "a", "1", confirmed_at=recent)]
        jobs, _ = self.run_sweep({("greenhouse", "a"): "fail"}, previous=previous)
        self.assertEqual(len(jobs), 1)

    def test_an_unstamped_role_gets_a_clock_rather_than_immortality(self):
        # Jobs written before confirmed_at existed have no stamp. Defaulting
        # them to "now" on every sweep would carry them forever, so the stamp
        # is written once and then ages normally.
        previous = [_job("greenhouse", "a", "1")]
        self.assertNotIn("confirmed_at", previous[0])
        jobs, _ = self.run_sweep({("greenhouse", "a"): "fail"}, previous=previous)
        self.assertIn("confirmed_at", jobs[0])

    def test_a_fresh_fetch_refreshes_the_stamp(self):
        old = int(time.time()) - cli.CARRY_MAX_SECONDS + 100
        previous = [_job("greenhouse", "a", "1", confirmed_at=old)]
        jobs, _ = self.run_sweep({("greenhouse", "a"): "jobs"}, previous=previous)
        self.assertGreater(jobs[0]["confirmed_at"], old)


class TestNotifiedRoleReachesTheBoard(SweepCase):
    """The user's report, end to end: a role is pushed, then the next sweeps
    must still be able to show it."""

    def test_a_role_notified_on_one_sweep_survives_the_next_eleven(self):
        answers = {("greenhouse", "board%d" % i): "jobs" for i in range(12)}
        health = {}

        # Sweep one: everything is found for the first time. This is the sweep
        # that would have pushed them.
        fake = FakeSources(answers)
        cli.sources = fake
        feed, _ = cli.sweep(CFG, health, {}, quiet=True, previous=[])
        notified = {j["uid"] for j in feed}
        self.assertTrue(notified)

        # The next eleven sweeps reach different boards. Every pushed role has
        # to still be in the feed the user opens.
        for _ in range(11):
            fake = FakeSources(answers)
            cli.sources = fake
            feed, _ = cli.sweep(CFG, health, {}, quiet=True, previous=feed)
            missing = notified - {j["uid"] for j in feed}
            self.assertEqual(missing, set(), "vanished from the feed: %s" % missing)


if __name__ == "__main__":
    unittest.main()
