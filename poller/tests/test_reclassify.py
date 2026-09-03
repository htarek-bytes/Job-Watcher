"""A change to the matching rules has to reach roles already in the feed.

A 304 carries a board's roles forward whole, classification and all, so a
carried role keeps the classification it was given under the OLD rules and a
widened matcher reaches that board only when it next changes its listings.

Scope, measured rather than assumed: 1 board out of 1072 stores an ETag,
because 1000 answer 200 every time and are reclassified anyway. That one is
the SimplifyJobs aggregator, which is the largest single source in the feed at
roughly 400 of 1071 roles and the only place a 304 earns its keep, its payload
being 13 MB. Narrow, and still worth doing.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import state
from test_carry import CFG, FakeSources, SweepCase, _job

import cli


class Fingerprint(unittest.TestCase):
    def test_it_is_stable_for_the_same_config(self):
        self.assertEqual(state.match_fingerprint(CFG), state.match_fingerprint(CFG))

    def test_a_matching_change_changes_it(self):
        other = dict(CFG, match=dict(CFG["match"], max_level=2))
        self.assertNotEqual(state.match_fingerprint(CFG), state.match_fingerprint(other))

    def test_a_location_change_changes_it(self):
        # [locations] decides which regions and remote scopes survive, so it
        # counts as a matching rule for this purpose.
        other = dict(CFG, locations=dict(CFG["locations"], allow_remote=False))
        self.assertNotEqual(state.match_fingerprint(CFG), state.match_fingerprint(other))

    def test_an_unrelated_change_does_not(self):
        # Adding a board must not trigger a full refetch of all ~1200 of them.
        other = dict(CFG, rotation={"cold_slice": 999, "hot_days": 1})
        self.assertEqual(state.match_fingerprint(CFG), state.match_fingerprint(other))

    def test_dropping_etags_reports_how_many(self):
        health = {"sources": {"a": {"etag": "x"}, "b": {"etag": "y"}, "c": {}}}
        self.assertEqual(state.drop_etags(health), 2)
        self.assertNotIn("etag", health["sources"]["a"])

    def test_dropping_etags_keeps_the_rest_of_the_record(self):
        health = {"sources": {"a": {"etag": "x", "last_success": 42}}}
        state.drop_etags(health)
        self.assertEqual(health["sources"]["a"]["last_success"], 42)


class ReclassifyOnRuleChange(SweepCase):
    ANSWERS = {("greenhouse", "a"): "jobs"}

    def test_the_first_sweep_records_the_fingerprint(self):
        health = {}
        self.run_sweep(self.ANSWERS, previous=[], health=health)
        self.assertEqual(health["match_fingerprint"], state.match_fingerprint(CFG))

    def test_an_unchanged_config_keeps_the_etags(self):
        health = {"match_fingerprint": state.match_fingerprint(CFG),
                  "sources": {"greenhouse:a": {"etag": "keep"}}}
        self.run_sweep(self.ANSWERS, previous=[], health=health)
        # The sweep itself may refresh it, but it must not have been dropped
        # on the way in, which is what a rule change does.
        self.assertIn("greenhouse:a", health["sources"])

    def test_a_changed_config_drops_the_etags(self):
        health = {"match_fingerprint": "stale", "sources": {
            "greenhouse:a": {"etag": "gone"},
            "greenhouse:b": {"etag": "gone too"},
        }}
        fake = FakeSources(self.ANSWERS)
        cli.sources = fake
        cli.sweep(CFG, health, {}, quiet=True, previous=[])
        self.assertNotIn("etag", health["sources"]["greenhouse:b"])
        self.assertEqual(health["match_fingerprint"], state.match_fingerprint(CFG))

    def test_a_role_from_an_unpolled_board_keeps_its_old_kind(self):
        # Dropping the ETag makes each board return a payload the NEXT time it
        # is polled. It does not rewrite roles carried from boards the rotation
        # has not reached yet, and it must not pretend to: reclassification
        # arrives over one rotation cycle, not instantly.
        answers = {("greenhouse", "b%d" % i): "jobs" for i in range(6)}
        previous = []
        for i in range(6):
            job = _job("greenhouse", "b%d" % i, "1")
            job["kind"] = "stale kind"
            previous.append(job)

        health = {"match_fingerprint": "stale"}
        fake = FakeSources(answers)
        cli.sources = fake
        jobs, _ = cli.sweep(CFG, health, {}, quiet=True, previous=previous)

        polled = {key for _source, key in fake.fetched}
        self.assertTrue(polled, "the sweep polled nothing, so this proves nothing")
        unpolled = [j for j in jobs if j["slug"] not in polled]
        self.assertTrue(unpolled, "no board was skipped, so this proves nothing")
        for job in unpolled:
            self.assertEqual(job["kind"], "stale kind", job["uid"])


if __name__ == "__main__":
    unittest.main()
