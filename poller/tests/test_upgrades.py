"""Tests for slug discovery, work authorization, dedupe and ranking."""

import json
import os
import sys
import time
import tomllib
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import discover
import rank
import state
import workauth

with open(os.path.join(os.path.dirname(HERE), "config.toml"), "rb") as fh:
    CFG = tomllib.load(fh)

FIXTURE = os.environ.get("SIMPLIFY_FIXTURE")


class SlugDiscovery(unittest.TestCase):
    def test_real_url_shapes(self):
        cases = [
            ("https://boards.greenhouse.io/embed/job_app?token=123&for=stripe",
             ("greenhouse", "stripe")),
            ("https://job-boards.greenhouse.io/databricks/jobs/7", ("greenhouse", "databricks")),
            ("https://jobs.lever.co/palantir/abc-def", ("lever", "palantir")),
            ("https://jobs.ashbyhq.com/mechanize/1ef2/application", ("ashby", "mechanize")),
            # Workday is supported now; see MultiPlatformDiscovery.
            ("https://gdit.wd5.myworkdayjobs.com/x/job/y", ("workday", "gdit.wd5/x")),
            ("https://fa-evmr.fa.ocs.oraclecloud.com/job/27744", None),
            ("", None),
            (None, None),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(discover.slug_from_url(url), expected)

    def test_noise_segments_rejected(self):
        self.assertIsNone(discover.slug_from_url("https://boards.greenhouse.io/embed"))

    def test_merge_never_drops_a_known_board(self):
        now = int(time.time())
        first = discover.merge({}, {"greenhouse": {"stripe": 2}}, now)
        # A later pass that does not see stripe must keep it: a company with no
        # new grad req today still has a working board.
        second = discover.merge(first, {"greenhouse": {"figma": 1}}, now + 60)
        self.assertIn("stripe", second["greenhouse"])
        self.assertIn("figma", second["greenhouse"])

    def test_config_slugs_are_marked(self):
        reg = discover.seed_from_config({}, CFG, 0)
        self.assertEqual(reg["ashby"]["openai"]["origin"], "config")


class WorkAuth(unittest.TestCase):
    def test_clearance_is_blocked(self):
        for text in ("Active TS/SCI with Poly Required",
                     "requires an active Secret clearance",
                     "must pass a full scope poly"):
            with self.subTest(text=text):
                self.assertEqual(workauth.classify(title=text)[0], workauth.BLOCKED)

    def test_citizenship_is_blocked(self):
        status, why = workauth.classify(description="Must be a U.S. citizen to comply with ITAR.")
        self.assertEqual(status, workauth.BLOCKED)
        self.assertTrue(why)

    def test_us_person_employer_is_blocked_without_a_description(self):
        # SimplifyJobs carries no description, so the employer list is the only
        # signal available for these.
        status, why = workauth.classify(title="Software Engineer New Grad", company="SpaceX")
        self.assertEqual(status, workauth.BLOCKED)
        self.assertIn("SpaceX", why)

    def test_no_sponsorship_beats_the_word_sponsorship(self):
        status, why = workauth.classify(
            description="Applicants must be authorized to work in the US without sponsorship."
        )
        self.assertEqual(status, workauth.CLOSED)
        self.assertIn("sponsorship", why.lower())

    def test_explicit_sponsorship_is_open(self):
        status, _ = workauth.classify(description="Visa sponsorship is available for this role.")
        self.assertEqual(status, workauth.OPEN)

    def test_unknown_is_the_default_and_carries_no_evidence(self):
        status, why = workauth.classify(title="Software Engineer I", description="We build things.")
        self.assertEqual(status, workauth.UNKNOWN)
        self.assertEqual(why, "")

    def test_evidence_is_always_present_for_a_call(self):
        for text in ("TS/SCI required", "we do not sponsor", "we will sponsor"):
            status, why = workauth.classify(description=text)
            with self.subTest(text=text):
                self.assertNotEqual(status, workauth.UNKNOWN)
                self.assertTrue(why.strip())

    def test_blocked_is_not_takeable(self):
        self.assertFalse(workauth.is_takeable(workauth.BLOCKED))
        self.assertTrue(workauth.is_takeable(workauth.UNKNOWN))


def _job(**kw):
    base = {"uid": "x", "source": "simplify", "company": "Acme",
            "title": "New Grad Software Engineer", "url": "u", "locations": [],
            "posted_at": int(time.time()), "region": "US",
            "work_auth": workauth.UNKNOWN}
    base.update(kw)
    return base


class Dedupe(unittest.TestCase):
    def test_collapses_same_company_and_title(self):
        jobs = [_job(uid="a"), _job(uid="b"), _job(uid="c")]
        self.assertEqual(len(rank.dedupe(jobs)), 1)

    def test_prefers_the_direct_ats_link(self):
        jobs = [
            _job(uid="s", source="simplify", url="https://simplify/x"),
            _job(uid="g", source="greenhouse", url="https://boards.greenhouse.io/acme/1"),
        ]
        [kept] = rank.dedupe(jobs)
        self.assertEqual(kept["source"], "greenhouse")
        self.assertEqual(kept["duplicate_count"], 2)
        self.assertEqual(kept["duplicate_sources"], ["greenhouse", "simplify"])

    def test_keeps_the_earliest_posting_time(self):
        old = int(time.time()) - 90000
        [kept] = rank.dedupe([_job(uid="a", posted_at=old),
                              _job(uid="b", posted_at=int(time.time()))])
        self.assertEqual(kept["posted_at"], old)

    def test_year_and_level_variants_collapse(self):
        self.assertEqual(
            rank.normalize_title("Software Engineer, New Grad 2027"),
            rank.normalize_title("Software Engineer New Grad 2026"),
        )

    def test_different_roles_do_not_collapse(self):
        jobs = [_job(uid="a", title="New Grad Software Engineer"),
                _job(uid="b", title="New Grad Data Engineer")]
        self.assertEqual(len(rank.dedupe(jobs)), 2)


class Ranking(unittest.TestCase):
    def test_blocked_scores_zero(self):
        value, reasons = rank.score(_job(work_auth=workauth.BLOCKED,
                                         work_auth_evidence="TS/SCI"))
        self.assertEqual(value, 0)
        self.assertTrue(reasons)

    def test_sponsoring_beats_unknown_beats_closed(self):
        now = time.time()
        openv = rank.score(_job(work_auth=workauth.OPEN), now)[0]
        unknownv = rank.score(_job(work_auth=workauth.UNKNOWN), now)[0]
        closedv = rank.score(_job(work_auth=workauth.CLOSED), now)[0]
        self.assertGreater(openv, unknownv)
        self.assertGreater(unknownv, closedv)

    def test_fresh_beats_stale(self):
        now = time.time()
        fresh = rank.score(_job(posted_at=int(now - 3600)), now)[0]
        stale = rank.score(_job(posted_at=int(now - 90 * 86400)), now)[0]
        self.assertGreater(fresh, stale)

    def test_staffing_firm_is_penalised(self):
        now = time.time()
        normal = rank.score(_job(company="Acme"), now)[0]
        shop = rank.score(_job(company="DellFor Technologies"), now)[0]
        self.assertLess(shop, normal)

    def test_every_score_is_explainable(self):
        job = _job(company="DellFor Technologies", work_auth=workauth.CLOSED)
        _, reasons = rank.score(job)
        self.assertTrue(all(r.strip() for r in reasons))

    def test_apply_ranking_sorts_best_first(self):
        jobs = [_job(uid="bad", work_auth=workauth.CLOSED),
                _job(uid="good", work_auth=workauth.OPEN)]
        rank.apply_ranking(jobs)
        self.assertEqual(jobs[0]["uid"], "good")


class ClosureStats(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(state.closure_summary([])["samples"], 0)

    def test_median_and_bucket(self):
        closed = [{"hours_open": h} for h in (10, 20, 30, 100, 500)]
        out = state.closure_summary(closed)
        self.assertEqual(out["samples"], 5)
        self.assertEqual(out["median_hours_open"], 30)
        self.assertEqual(out["under_48h"], 3)


@unittest.skipUnless(FIXTURE and os.path.exists(FIXTURE or ""),
                     "set SIMPLIFY_FIXTURE to the real listings.json")
class AgainstRealFeed(unittest.TestCase):
    """The measurements that motivated these changes, as regression tests."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, "r", encoding="utf-8") as fh:
            cls.records = [r for r in json.load(fh)
                           if r.get("active") and r.get("is_visible", True)]

    def test_discovery_finds_far_more_boards_than_the_config(self):
        found = discover.discover(self.records)
        total = sum(len(v) for v in found.values())
        configured = sum(len(CFG["sources"][s]["slugs"])
                         for s in ("greenhouse", "lever", "ashby"))
        self.assertGreater(total, configured * 3,
                           "discovery should dwarf the hand-written list")

    def test_us_person_employers_are_actually_caught(self):
        blocked = [r for r in self.records
                   if workauth.classify(title=r.get("title", ""),
                                        company=r.get("company_name", ""))[0]
                   == workauth.BLOCKED]
        companies = {r["company_name"] for r in blocked}
        for expected in ("SpaceX", "RTX"):
            self.assertTrue(any(expected in c for c in companies),
                            "%s should be flagged" % expected)

    def test_dedupe_collapses_the_known_spam(self):
        jobs = [{"uid": r["id"], "source": "simplify", "company": r["company_name"],
                 "title": r["title"], "url": r.get("url", ""), "locations": [],
                 "posted_at": r.get("date_posted")} for r in self.records]
        collapsed = rank.dedupe(jobs)
        self.assertLess(len(collapsed), len(jobs))
        worst = max(collapsed, key=lambda j: j.get("duplicate_count", 1))
        self.assertGreater(worst.get("duplicate_count", 1), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class NotModifiedCarryForward(unittest.TestCase):
    """A 304 means unchanged, not empty.

    Treating them the same wiped the committed feed on the second sweep: the
    first run stored an ETag, the second got 304, and jobs.json was rewritten
    with zero roles.
    """

    def test_aggregator_carries_forward_despite_a_slug_mismatch(self):
        """The bug inside the fix.

        The aggregator is configured as "SimplifyJobs/New-Grad-Positions" but
        stamps its jobs with slug "listings". Keying the carry map on the job
        slug and looking it up by the config key silently found nothing, so the
        304 path still emptied the feed.
        """
        import cli
        previous = [{"uid": "a", "source": "simplify", "slug": "listings"},
                    {"uid": "b", "source": "greenhouse", "slug": "stripe"}]
        cfg = dict(CFG)
        cfg["sources"] = {"simplify": {"enabled": True,
                                       "repo": "SimplifyJobs/New-Grad-Positions"}}
        health = {"sources": {"simplify:SimplifyJobs/New-Grad-Positions":
                              {"etag": "x"}}}

        import sources as srcmod
        original = srcmod.fetch

        def unchanged(source, key, config, etag=None):
            res = srcmod.SourceResult(source, key)
            res.ok, res.not_modified, res.status = True, True, 304
            return res

        srcmod.fetch = unchanged
        try:
            jobs, _ = cli.sweep(cfg, health, {}, quiet=True, previous=previous)
        finally:
            srcmod.fetch = original
        self.assertEqual(len(jobs), 1, "aggregator jobs must survive a 304")
        self.assertEqual(jobs[0]["uid"], "a")


class MultiPlatformDiscovery(unittest.TestCase):
    """Board keys recovered from posting URLs, per platform.

    Measured against the live feed: workday 371 boards over 927 postings,
    smartrecruiters 110 over 231, bamboohr 21 over 39, rippling 18 over 21.
    Workday was previously running on 8 hand-guessed boards.
    """

    def test_workday_key_is_tenant_dc_and_site(self):
        self.assertEqual(
            discover.slug_from_url(
                "https://gdit.wd5.myworkdayjobs.com/external_career_site/job/USA/Dev_RQ1"),
            ("workday", "gdit.wd5/external_career_site"))

    def test_workday_tolerates_a_locale_segment(self):
        self.assertEqual(
            discover.slug_from_url(
                "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x"),
            ("workday", "nvidia.wd5/NVIDIAExternalCareerSite"))

    def test_smartrecruiters_keeps_its_case(self):
        # SmartRecruiters company identifiers are case sensitive in its API,
        # so lowercasing them the way the other slugs are lowercased breaks it.
        self.assertEqual(
            discover.slug_from_url("https://jobs.smartrecruiters.com/ServiceNow/744000"),
            ("smartrecruiters", "ServiceNow"))

    def test_bamboohr_and_rippling(self):
        self.assertEqual(discover.slug_from_url("https://lexical.bamboohr.com/careers/42"),
                         ("bamboohr", "lexical"))
        self.assertEqual(discover.slug_from_url("https://ats.rippling.com/flexai/jobs/a"),
                         ("rippling", "flexai"))

    def test_every_discoverable_source_has_a_fetcher(self):
        import sources as srcmod
        for name in discover.SOURCES:
            with self.subTest(source=name):
                srcmod.fetch(name, "x", {"sources": {}}) if False else None
                self.assertTrue(hasattr(srcmod, "fetch_" + name.replace(".", "")),
                                "no fetch_%s" % name)

    @unittest.skipUnless(FIXTURE and os.path.exists(FIXTURE or ""),
                         "set SIMPLIFY_FIXTURE to the real listings.json")
    def test_discovery_scale_against_the_real_feed(self):
        with open(FIXTURE, "r", encoding="utf-8") as fh:
            records = [r for r in json.load(fh)
                       if r.get("active") and r.get("is_visible", True)]
        found = discover.discover(records)
        self.assertGreater(len(found["workday"]), 200, "workday discovery regressed")
        self.assertGreater(len(found["smartrecruiters"]), 50)
        self.assertGreater(sum(len(v) for v in found.values()), 700)


class AggregatorLag(unittest.TestCase):
    """The aggregator's date is when it noticed a posting, not when the
    company published it. The gap is measured from roles that arrive both
    ways, never assumed."""

    def _pair(self, ats_at, agg_at):
        return [
            _job(uid="g", source="greenhouse", posted_at=ats_at,
                 url="https://boards.greenhouse.io/acme/1"),
            _job(uid="s", source="simplify", posted_at=agg_at,
                 url="https://simplify/x"),
        ]

    def test_lag_is_measured_from_a_matched_pair(self):
        samples = []
        rank.dedupe(self._pair(1_000_000, 1_000_000 + 3 * 86400), samples)
        self.assertEqual(samples, [3 * 86400])

    def test_survivor_keeps_the_earliest_date_and_says_where_it_came_from(self):
        [kept] = rank.dedupe(self._pair(1_000_000, 1_000_000 + 3 * 86400))
        self.assertEqual(kept["posted_at"], 1_000_000)
        self.assertEqual(kept["posted_from"], "greenhouse")

    def test_negative_gaps_are_not_lag(self):
        # The ATS re-dating a req is not the aggregator being slow.
        samples = []
        rank.dedupe(self._pair(1_000_000 + 5000, 1_000_000), samples)
        self.assertEqual(samples, [])

    def test_absurd_gaps_are_rejected(self):
        samples = []
        rank.dedupe(self._pair(1_000_000, 1_000_000 + 200 * 86400), samples)
        self.assertEqual(samples, [])

    def test_single_source_roles_still_record_their_origin(self):
        [kept] = rank.dedupe([_job(uid="a", source="simplify", posted_at=1)])
        self.assertEqual(kept["posted_from"], "simplify")


class StableGroupKey(unittest.TestCase):
    """Application state hangs off this, so it must survive a source change."""

    def test_same_role_from_two_sources_shares_a_key(self):
        a = _job(uid="simplify:listings:abc", source="simplify")
        b = _job(uid="greenhouse:acme:7", source="greenhouse")
        self.assertEqual(rank.group_key(a), rank.group_key(b))

    def test_year_and_punctuation_variants_share_a_key(self):
        a = _job(title="Software Engineer, New Grad 2027")
        b = _job(title="Software Engineer New Grad 2026")
        self.assertEqual(rank.group_key(a), rank.group_key(b))

    def test_different_roles_differ(self):
        self.assertNotEqual(rank.group_key(_job(title="New Grad Software Engineer")),
                            rank.group_key(_job(title="New Grad Data Engineer")))
        self.assertNotEqual(rank.group_key(_job(company="Acme")),
                            rank.group_key(_job(company="Globex")))

    def test_dedupe_stamps_it(self):
        [kept] = rank.dedupe([_job(uid="a")])
        self.assertEqual(len(kept["group_key"]), 12)

    def test_key_is_stable_across_runs(self):
        self.assertEqual(rank.group_key(_job()), rank.group_key(_job()))
