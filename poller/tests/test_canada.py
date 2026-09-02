"""Canadian source parser tests.

There are two separable questions about a source: does the endpoint answer,
and can the response be read. Only the second can be tested here, because no
job board is reachable from a sandbox. So these tests pin the parsers against
fixtures shaped like the real pages, and `python poller/cli.py probe` answers
the first from a machine with real egress. Passing tests here mean the parser
is correct, not that the source works.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import canada
import discover
import sources


# schema.org JobPosting, which all four HTML sources emit for search engines
# and which is tried before any site specific markup.
JSON_LD_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"JobPosting","title":"Junior Software Developer",
  "datePosted":"2026-09-01",
  "hiringOrganization":{"@type":"Organization","name":"Coveo"},
  "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
    "addressLocality":"Quebec City","addressRegion":"QC",
    "addressCountry":"CA"}},
  "url":"https://www.jobbank.gc.ca/jobsearch/jobposting/41999001",
  "description":"<p>Write <b>code</b>.</p>"},
 {"@type":"JobPosting","title":"Software Engineer, New Grad",
  "datePosted":"2026-09-02T10:00:00Z",
  "hiringOrganization":{"name":"Kinaxis"},
  "jobLocationType":"TELECOMMUTE",
  "jobLocation":[{"address":{"addressLocality":"Ottawa","addressRegion":"ON"}}],
  "url":"/jobsearch/jobposting/41999002"}
]}
</script>
</head><body></body></html>
"""

# Job Bank's results list, for when the page carries no JSON-LD.
JOBBANK_HTML = """
<html><body>
<article id="article-41888001" class="resultJobItem">
  <a href="/jobsearch/jobposting/41888001?source=searchresults">
    <span class="noc-title">software developer</span>
    <ul class="list-unstyled">
      <li class="business">SHOPIFY INC.</li>
      <li class="location">Ottawa (ON)</li>
      <li class="date"><span>September 2, 2026</span></li>
    </ul>
  </a>
</article>
<article id="article-41888002" class="resultJobItem">
  <a href="/jobsearch/jobposting/41888002">
    <span class="noc-title">junior programmer analyst</span>
    <ul class="list-unstyled">
      <li class="business">Lightspeed Commerce</li>
      <li class="location">Montr&eacute;al (QC)</li>
      <li class="date"><span>2 septembre 2026</span></li>
    </ul>
  </a>
</article>
<article id="article-41888003"><a href="/jobsearch/jobposting/41888003"></a></article>
</body></html>
"""


class TestJsonLd(unittest.TestCase):
    def test_reads_both_postings(self):
        jobs = canada._from_json_ld(
            canada.JOBBANK, "q", JSON_LD_PAGE, base=canada.JOBBANK_BASE)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["title"], "Junior Software Developer")
        self.assertEqual(jobs[0]["company"], "Coveo")

    def test_flattens_nested_address(self):
        jobs = canada._from_json_ld(canada.JOBBANK, "q", JSON_LD_PAGE)
        self.assertIn("Quebec City, QC, CA", jobs[0]["locations"])

    def test_telecommute_becomes_remote(self):
        jobs = canada._from_json_ld(canada.JOBBANK, "q", JSON_LD_PAGE)
        self.assertIn("Remote", jobs[1]["locations"])

    def test_relative_url_is_absolute(self):
        jobs = canada._from_json_ld(
            canada.JOBBANK, "q", JSON_LD_PAGE, base=canada.JOBBANK_BASE)
        self.assertTrue(jobs[1]["url"].startswith("https://www.jobbank.gc.ca/"))

    def test_html_is_stripped_from_description(self):
        jobs = canada._from_json_ld(canada.JOBBANK, "q", JSON_LD_PAGE)
        self.assertEqual(jobs[0]["description"], "Write code .")

    def test_one_broken_block_does_not_lose_the_page(self):
        page = ('<script type="application/ld+json">{not json</script>'
                + JSON_LD_PAGE)
        self.assertEqual(len(canada._from_json_ld(canada.JOBBANK, "q", page)), 2)

    def test_no_json_ld_returns_empty_not_an_error(self):
        self.assertEqual(canada._from_json_ld(canada.JOBBANK, "q", "<html></html>"), [])


class TestJobBankMarkup(unittest.TestCase):
    def test_parses_the_results_list(self):
        jobs = canada._parse_jobbank("software developer", JOBBANK_HTML)
        self.assertEqual(len(jobs), 2)

    def test_reads_employer_and_location(self):
        jobs = canada._parse_jobbank("q", JOBBANK_HTML)
        self.assertEqual(jobs[0]["company"], "SHOPIFY INC.")
        self.assertEqual(jobs[0]["locations"], ["Ottawa (ON)"])

    def test_entities_are_decoded(self):
        jobs = canada._parse_jobbank("q", JOBBANK_HTML)
        self.assertEqual(jobs[1]["locations"], ["Montréal (QC)"])

    def test_url_is_absolute_and_query_free(self):
        jobs = canada._parse_jobbank("q", JOBBANK_HTML)
        self.assertEqual(
            jobs[0]["url"],
            "https://www.jobbank.gc.ca/jobsearch/jobposting/41888001")

    def test_a_result_with_no_title_is_dropped_not_blank(self):
        # The third article has no noc-title. A blank row in the feed is worse
        # than a missing one: it cannot be matched and cannot be applied to.
        titles = [j["title"] for j in canada._parse_jobbank("q", JOBBANK_HTML)]
        self.assertNotIn("", titles)

    def test_json_ld_wins_when_the_page_has_both(self):
        jobs = canada._parse_jobbank("q", JSON_LD_PAGE + JOBBANK_HTML)
        self.assertEqual([j["company"] for j in jobs], ["Coveo", "Kinaxis"])

    def test_english_date(self):
        self.assertEqual(canada._jobbank_date("September 2, 2026"),
                         sources._epoch("2026-09-02"))

    def test_french_date(self):
        self.assertEqual(canada._jobbank_date("2 septembre 2026"),
                         sources._epoch("2026-09-02"))

    def test_unparseable_date_is_none_not_now(self):
        # A posting stamped "now" because its date could not be read would show
        # as brand new and green in the dashboard. Unknown has to stay unknown.
        self.assertIsNone(canada._jobbank_date("Posted recently"))
        self.assertIsNone(canada._jobbank_date(None))


class TestEluta(unittest.TestCase):
    PAGE = """<html><body>
      <a href="https://boards.greenhouse.io/wealthsimple/jobs/123">SWE</a>
      <a href="https://jobs.lever.co/clio/abc">Dev</a>
      <a href="https://www.eluta.ca/about">About us</a>
      <a href="https://careers.somebank.ca/apply/9">Analyst</a>
      <a href="https://www.linkedin.com/company/x">LinkedIn</a>
    </body></html>"""

    def test_keeps_only_links_an_ats_parser_understands(self):
        jobs = canada._parse_eluta("q", self.PAGE)
        self.assertEqual(len(jobs), 2)

    def test_kept_links_resolve_to_boards(self):
        found = [discover.slug_from_url(j["url"]) for j in canada._parse_eluta("q", self.PAGE)]
        self.assertIn(("greenhouse", "wealthsimple"), found)
        self.assertIn(("lever", "clio"), found)

    def test_self_links_are_dropped(self):
        urls = [j["url"] for j in canada._parse_eluta("q", self.PAGE)]
        self.assertFalse(any("eluta.ca" in u for u in urls))


class TestGetro(unittest.TestCase):
    PAYLOAD = {"results": {"count": 2, "jobs": [
        {"id": 11, "title": "Software Engineer (New Grad)",
         "url": "https://jobs.ashbyhq.com/cohere/x",
         "organization": {"name": "Cohere"},
         "locations": [{"city": "Toronto", "region": "ON", "country": "Canada"}],
         "created_at": "2026-09-01T12:00:00Z"},
        {"id": 12, "title": "", "url": "https://example.com/none"},
    ]}}

    def test_reads_the_nested_results_envelope(self):
        jobs = canada._parse_getro("42", self.PAYLOAD)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["company"], "Cohere")

    def test_flattens_location_objects(self):
        jobs = canada._parse_getro("42", self.PAYLOAD)
        self.assertEqual(jobs[0]["locations"], ["Toronto, ON, Canada"])

    def test_accepts_a_flat_jobs_key_too(self):
        flat = {"jobs": self.PAYLOAD["results"]["jobs"]}
        self.assertEqual(len(canada._parse_getro("42", flat)), 1)

    def test_titleless_rows_are_dropped(self):
        self.assertTrue(all(j["title"] for j in canada._parse_getro("42", self.PAYLOAD)))

    def test_empty_payload_is_not_an_error(self):
        self.assertEqual(canada._parse_getro("42", {}), [])


class TestWiring(unittest.TestCase):
    CFG = {"sources": {
        "jobbank": {"enabled": True, "queries": ["software developer"]},
        "jobillico": {"enabled": False, "queries": ["x"]},
        "getro": {"enabled": True, "collections": [123]},
    }}

    def test_only_enabled_sources_are_polled(self):
        got = list(canada.iter_configured(self.CFG))
        self.assertIn(("jobbank", "software developer"), got)
        self.assertNotIn(("jobillico", "x"), got)

    def test_collection_ids_become_string_keys(self):
        self.assertIn(("getro", "123"), list(canada.iter_configured(self.CFG)))

    def test_disabled_by_default(self):
        self.assertEqual(list(canada.iter_configured({"sources": {"jobbank": {}}})), [])

    def test_sources_fetch_dispatches_to_canada(self):
        # sources.fetch is the single entry point the sweep uses. A Canadian
        # source reaching it must not fall through to the "unknown source"
        # raise, which would take the whole sweep down.
        for name in canada.SOURCES:
            self.assertIn(name, canada._FETCHERS)

    def test_every_canadian_source_is_polled_every_sweep(self):
        # They are national queries, not company boards. Rotation exists to
        # keep ~900 ATS boards polite; putting a handful of aggregators in it
        # would just make the Canadian side stale.
        for name in canada.SOURCES:
            self.assertNotIn(name, discover.SOURCES)

    def test_discovery_sources_are_real_sources(self):
        for name in canada.DISCOVERY_SOURCES:
            self.assertIn(name, canada.SOURCES)


if __name__ == "__main__":
    unittest.main()
