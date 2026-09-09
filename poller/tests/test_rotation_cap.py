"""The cap on the hot set, and why the window alone could not do the job.

A board earns "hot" by producing a match, and it is re-stamped on every sweep
it keeps producing one. So hot_days never expires a board that stays
productive, and once the matcher widened to three tracks nearly every board
stayed productive. 578 went permanently hot, the sweep grew to 879 requests
and eight minutes, and the one-minute schedule became unmeetable: measured
gaps of 45 to 75 minutes between sweeps, which is the shape of "it stopped
polling".
"""

import os
import sys
import time
import tomllib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import cli
import discover

with open(os.path.join(os.path.dirname(HERE), "config.toml"), "rb") as fh:
    CFG = tomllib.load(fh)


def registry(productive, stale=0, pinned=0):
    """A registry of greenhouse boards: freshly matching, long stale, pinned."""
    now = time.time()
    block = {}
    for i in range(productive):
        # Staggered so "freshest first" has something to sort on.
        block["fresh%03d" % i] = {"origin": "discovered", "last_match": now - i}
    for i in range(stale):
        block["stale%03d" % i] = {"origin": "discovered",
                                  "last_match": now - 365 * 86400}
    for i in range(pinned):
        block["pin%03d" % i] = {"origin": "hunted-ca"}
    return {"greenhouse": block}


def config(max_hot=None, cold_slice=80):
    rot = dict(CFG["rotation"], cold_slice=cold_slice)
    if max_hot is not None:
        rot["max_hot"] = max_hot
    # Only greenhouse, so the counts in these tests are the boards themselves
    # and not the aggregators.
    srcs = {k: dict(v, enabled=False) for k, v in CFG["sources"].items()}
    srcs["greenhouse"] = dict(CFG["sources"]["greenhouse"], enabled=True, slugs=[])
    return dict(CFG, rotation=rot, sources=srcs, discovery={"enabled": False})


class TheWindowAloneDoesNotBound(unittest.TestCase):
    def test_a_productive_board_never_ages_out_of_the_window(self):
        # The measurement that produced the cap. Every board matched seconds
        # ago, so no window shorter than the sweep interval expires any of
        # them, and 21 days and 1 day give the same answer.
        reg = registry(productive=600)
        for days in (21, 7, 1):
            cfg = dict(config(max_hot=10_000), rotation=dict(
                CFG["rotation"], hot_days=days, cold_slice=80, max_hot=10_000))
            health = {}
            cli.select_targets(cfg, reg, health)
            with self.subTest(hot_days=days):
                self.assertEqual(health["hot_boards"], 600,
                                 "hot_days=%d still leaves every board hot" % days)


class TheCapBounds(unittest.TestCase):
    def test_it_holds_the_hot_set_to_the_cap(self):
        health = {}
        cli.select_targets(config(max_hot=150), registry(productive=600), health)
        self.assertEqual(health["hot_earned"], 150)
        self.assertEqual(health["hot_demoted"], 450)
        self.assertEqual(health["hot_boards"], 150)

    def test_the_freshest_matches_are_the_ones_kept(self):
        reg = registry(productive=300)
        _all, targets = cli.select_targets(config(max_hot=10), reg, {})
        hot = [key for _s, key in targets[:10]]
        # fresh000 matched most recently, fresh299 least.
        self.assertEqual(hot, ["fresh%03d" % i for i in range(10)])

    def test_the_demoted_boards_are_not_dropped_but_rotated(self):
        reg = registry(productive=600)
        health = {}
        all_targets, targets = cli.select_targets(config(max_hot=150), reg, health)
        self.assertEqual(len(all_targets), 600)
        self.assertEqual(health["cold_boards"], 450)
        # 150 hot + one 80-board window.
        self.assertEqual(len(targets), 230)

    def test_a_pinned_board_is_never_demoted(self):
        # config and hunted-ca boards were chosen by hand and are not subject
        # to the cap, which only governs boards that earned it by matching.
        health = {}
        cli.select_targets(config(max_hot=5),
                           registry(productive=100, pinned=40), health)
        self.assertEqual(health["hot_pinned"], 40)
        self.assertEqual(health["hot_earned"], 5)
        self.assertEqual(health["hot_boards"], 45)

    def test_the_rotation_still_covers_everything(self):
        # The demoted boards have to actually come round, or the cap would be
        # a way of quietly never polling them again.
        reg = registry(productive=400)
        cfg = config(max_hot=50, cold_slice=80)
        health = {}
        seen = set()
        for _ in range(10):
            _all, targets = cli.select_targets(cfg, reg, health)
            seen.update(key for _s, key in targets)
        self.assertEqual(len(seen), 400, "%d of 400 boards reached" % len(seen))

    def test_the_window_is_stable_between_sweeps(self):
        # The cold list is sorted, so the window walks a list that is not
        # moving underneath it. Two selections at the same offset must agree.
        reg = registry(productive=300)
        a, b = {"rotation_offset": 40}, {"rotation_offset": 40}
        _x, first = cli.select_targets(config(max_hot=20), reg, a)
        _y, second = cli.select_targets(config(max_hot=20), reg, b)
        self.assertEqual(first, second)


class TheBudget(unittest.TestCase):
    """What the cap is for: a sweep that fits inside its own schedule."""

    def test_requests_per_sweep_are_bounded_by_config(self):
        cfg = CFG
        rot = cfg["rotation"]
        # Pinned boards are the one unbounded part, and they are deliberate:
        # 64 from config plus the 115 the Canadian hunt confirmed.
        aggregator_queries = sum(
            len(cfg["sources"][name].get("queries", []))
            for name in ("jobbank", "jobillico", "talentegg", "amazon")
            if cfg["sources"][name].get("enabled"))
        worst = rot["max_hot"] + rot["cold_slice"] + aggregator_queries + 200
        self.assertLess(worst, 600,
                        "a sweep this size cannot finish inside its schedule")

    def test_a_dead_board_cannot_cost_more_than_a_few_seconds(self):
        import net
        # 25s and 2 retries meant about eighty seconds of a worker per dead
        # board, and two dozen fail every sweep.
        worst = net.TIMEOUT * (net.RETRIES + 1) + net.BACKOFF * net.RETRIES
        self.assertLess(worst, 35, "a dead board costs %.0fs of a worker" % worst)


if __name__ == "__main__":
    unittest.main()
