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

# Job Bank's results list, copied from what a probe run actually returned
# rather than from memory. The first version of this fixture was invented, the
# parser passed against it, and it then found nothing in 284 KB of real page:
# the class is `noctitle` not `noc-title`, the date is a bare text node, the
# location opens with two icon spans, and every href carries a jsessionid.
JOBBANK_HTML = """
<html><body>
<article id="article-50112716" class="action-buttons"><a href="/jobsearch/jobposting/50112716;jsessionid=58AEF7931F9F1FC410F9B6542FF4617F.jobsearch76?source=searchresults" class="resultJobItem">
    <h3 class="title">
      <span class="flag"><span class="telework">On site</span><span class="appmethod">Direct Apply</span></span>
      <span class="job-source job-source-icon-16"><span class="wb-inv">Job Bank</span></span>
      <span class="noctitle"> junior software developer
      </span>
    </h3>
    <ul class="list-unstyled">
      <li class="date">August 19, 2026
      </li>
      <li class="business">Toronto Sun Wah Trading Ltd</li>
      <li class="location"><span class="fas fa-map-marker-alt" aria-hidden="true"></span> <span class="wb-inv">Location</span>
             Etobicoke (ON)
      </li>
      <li class="salary"><span class="fa fa-dollar" aria-hidden="true"></span> $50.00 hourly</li>
      <li class="source"><span class="wb-inv">Job number:</span> 3651548</li>
    </ul></a>
</article>
<article id="article-50112717" class="action-buttons"><a href="/jobsearch/jobposting/50112717;jsessionid=ABC.jobsearch76">
    <h3 class="title"><span class="noctitle">programmeur junior</span></h3>
    <ul class="list-unstyled">
      <li class="date">2 septembre 2026</li>
      <li class="business">Lightspeed Commerce</li>
      <li class="location"><span class="wb-inv">Location</span> Montr&eacute;al (QC)</li>
    </ul></a>
</article>
<article id="article-50112718"><a href="/jobsearch/jobposting/50112718"><h3 class="title"><span class="noctitle">
</span></h3></a></article>
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

    def test_reads_employer(self):
        jobs = canada._parse_jobbank("q", JOBBANK_HTML)
        self.assertEqual(jobs[0]["company"], "Toronto Sun Wah Trading Ltd")

    def test_title_is_trimmed(self):
        jobs = canada._parse_jobbank("q", JOBBANK_HTML)
        self.assertEqual(jobs[0]["title"], "junior software developer")

    def test_location_skips_the_icon_spans(self):
        jobs = canada._parse_jobbank("q", JOBBANK_HTML)
        self.assertEqual(jobs[0]["locations"], ["Etobicoke (ON)"])

    def test_screen_reader_labels_are_not_the_location(self):
        # <span class="wb-inv">Location</span> is there for screen readers. Left
        # in, every Canadian role reads as being in a city called "Location".
        for job in canada._parse_jobbank("q", JOBBANK_HTML):
            self.assertNotIn("Location", job["locations"][0])

    def test_entities_are_decoded(self):
        jobs = canada._parse_jobbank("q", JOBBANK_HTML)
        self.assertEqual(jobs[1]["locations"], ["Montréal (QC)"])

    def test_url_drops_the_session_id(self):
        # Job Bank stamps a jsessionid into every href. Kept, the same posting
        # gets a different URL on every fetch, which makes dedupe fail and the
        # diff report an old job as new, over and over.
        jobs = canada._parse_jobbank("q", JOBBANK_HTML)
        self.assertEqual(
            jobs[0]["url"],
            "https://www.jobbank.gc.ca/jobsearch/jobposting/50112716")
        self.assertFalse(any("jsessionid" in j["url"] for j in jobs))

    def test_url_is_stable_across_fetches(self):
        first = canada._parse_jobbank("q", JOBBANK_HTML)
        second = canada._parse_jobbank(
            "q", JOBBANK_HTML.replace("58AEF7931F9F1FC410F9B6542FF4617F", "DEADBEEF"))
        self.assertEqual([j["url"] for j in first], [j["url"] for j in second])
        self.assertEqual([j["uid"] for j in first], [j["uid"] for j in second])

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


# Copied from a probe run against /recherche-emploi?skwd=developpeur+logiciel,
# which answered 200 with 456 KB. The path form, /recherche-emploi/<keyword>,
# 404s: those segments are canned category pages, not a search.
JOBILLICO_HTML = """
<html><body>
<article ref="firstJob" class="card card--clickable"
         data-job-url="exfo-inc/developpeur-logiciel-linux/17341496?&amp;upg=12"
         data-company-id="4842">
  <div class="card__content">
    <a href="/ajouter-aux-emplois-favoris/17341496/L3JlY2g=" class="icon icon--favorite"> Ajouter aux favoris </a>
    <span class="image-container"><a target="_blank" href="/voir-entreprise/exfo-inc?&amp;upg=14" title="EXFO inc"></a></span>
    <header class="relative font-0">
      <h2 class="h3 my0 pr5 word-break"><a href="/fr/offre-d-emploi/exfo-inc/developpeur-logiciel-linux/17341496?&amp;upg=12" id="0" rel='nofollow'>D&eacute;veloppeur Logiciel Junior</a></h2>
      <h3 class="h4"><a class="link companyLink js--stopPropagationEvent" target="_blank" href="/voir-entreprise/exfo-inc?&amp;upg=18">EXFO inc</a></h3>
    </header>
    <p class="xs word-break">Job Description: Titre du poste [...]</p>
    <ul class="list list--has-no-bullets">
      <li class="list__item mb1"><span class="icon icon--information icon--information--position"></span>
        <p class="inline xs valign-middle">Qu&eacute;bec - QC                    </p>
      </li>
      <li class="list__item mb0">
        <span class="icon icon--information icon--information--calendar"></span>
        <p class="inline valign-middle"><time class="xs" datetime="2026-09-01">1 jour(s)</time></p>
      </li>
    </ul>
  </div>
</article>
<article data-job-url="siga-informatique-inc/developpeur-logiciel-junior/17450649?&amp;upg=12">
  <header><h2 class="h3"><a href="/fr/offre-d-emploi/siga/x/17450649">Programmeur junior</a></h2>
  <h3 class="h4"><a class="link companyLink" href="/voir-entreprise/siga">SIGA Informatique</a></h3></header>
</article>
<article data-job-url="x/y/17000000"><header><h2><a href="/fr/offre-d-emploi/x/y/17000000">
</a></h2></header></article>
</body></html>
"""


class TestJobillico(unittest.TestCase):
    def test_reads_the_result_cards(self):
        jobs = canada._parse_jobillico("q", JOBILLICO_HTML)
        self.assertEqual(len(jobs), 2)

    def test_keeps_the_employer(self):
        # The JSON-LD on this page is an ItemList of ListItem, which carries
        # only a title and a link. A posting with no company name is far less
        # useful to act on, which is why the cards are read first.
        jobs = canada._parse_jobillico("q", JOBILLICO_HTML)
        self.assertEqual(jobs[0]["company"], "EXFO inc")

    def test_reads_the_machine_readable_date(self):
        jobs = canada._parse_jobillico("q", JOBILLICO_HTML)
        self.assertEqual(jobs[0]["posted_at"], sources._epoch("2026-09-01"))

    def test_location_skips_the_icon_span(self):
        jobs = canada._parse_jobillico("q", JOBILLICO_HTML)
        self.assertEqual(jobs[0]["locations"], ["Québec - QC"])

    def test_url_drops_the_tracking_parameters(self):
        jobs = canada._parse_jobillico("q", JOBILLICO_HTML)
        self.assertEqual(
            jobs[0]["url"],
            "https://www.jobillico.com/fr/offre-d-emploi/"
            "exfo-inc/developpeur-logiciel-linux/17341496")
        self.assertFalse(any("upg=" in j["url"] for j in jobs))

    def test_a_card_missing_a_location_still_counts(self):
        jobs = canada._parse_jobillico("q", JOBILLICO_HTML)
        self.assertEqual(jobs[1]["locations"], ["Quebec, Canada"])

    def test_a_card_with_no_title_is_dropped(self):
        self.assertTrue(all(j["title"] for j in canada._parse_jobillico("q", JOBILLICO_HTML)))

    def test_search_url_uses_the_query_string_form(self):
        self.assertEqual(
            canada.search_url(canada.JOBILLICO, "developpeur logiciel"),
            "https://www.jobillico.com/recherche-emploi?skwd=developpeur+logiciel")


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

    def test_a_source_measured_unreachable_is_never_polled(self):
        # Eluta refuses the TLS handshake. Leaving it in the poll list would
        # spend a request and three retries on it every single sweep.
        for name in canada.UNREACHABLE:
            self.assertNotIn(name, canada.SOURCES)
            self.assertNotIn(name, canada.DISCOVERY_SOURCES)

    def test_an_unreachable_source_still_has_a_fetcher(self):
        # It is still probed, so it has to be callable.
        for name in canada.UNREACHABLE:
            self.assertIn(name, canada._FETCHERS)


class TestSearchUrls(unittest.TestCase):
    """These paths were measured, not remembered. Seven guesses at Jobillico's
    search path returned 404 and six at TalentEgg's returned 500, so these
    tests exist to stop the wrong shapes coming back."""

    def test_jobillico_uses_a_query_string_not_a_path(self):
        # /recherche-emploi/<keyword> looks right, and the front page links to
        # it, but those segments are canned category pages and a keyword there
        # 404s. Only the query string form searches.
        self.assertEqual(
            canada.search_url(canada.JOBILLICO, "software developer"),
            "https://www.jobillico.com/recherche-emploi?skwd=software+developer")

    def test_talentegg_uses_find_a_job(self):
        self.assertEqual(
            canada.search_url(canada.TALENTEGG, "software developer"),
            "https://talentegg.ca/find-a-job/keyword/software%20developer")

    def test_the_two_sites_encode_a_space_differently(self):
        # Jobillico writes emploi+etudiant, TalentEgg writes Entry%20Level.
        # One encoding for both would be wrong for one of them.
        self.assertIn("+", canada.search_url(canada.JOBILLICO, "a b"))
        self.assertIn("%20", canada.search_url(canada.TALENTEGG, "a b"))

    def test_no_candidate_url_has_a_stray_format_token(self):
        # A template without %s is used verbatim, so a %%20 written for
        # substitution would reach the network as a literal %%20.
        for source in canada.SOURCES + canada.UNREACHABLE:
            if source == canada.GETRO:
                continue
            for url in canada.probe_urls(source, "software developer"):
                self.assertNotIn("%s", url, url)
                self.assertNotIn("%%", url, url)

    def test_the_primary_url_is_probed_first(self):
        for source in canada.SEARCH_URLS:
            self.assertEqual(canada.probe_urls(source, "x")[0],
                             canada.search_url(source, "x"))


class TestHuntSeed(unittest.TestCase):
    """The seed list is candidates, not confirmed boards. These tests guard the
    reading of it; `cli.py hunt` is what decides which names are real."""

    def setUp(self):
        import cli
        self.cli = cli
        self.names = cli.load_names(cli.SEED_FILE)

    def test_comments_and_blanks_are_dropped(self):
        self.assertTrue(all(n and not n.startswith("#") for n in self.names))

    def test_names_are_deduplicated(self):
        # The same company appears under two regional headings often enough
        # that duplicates would quietly double the request count.
        self.assertEqual(len(self.names), len(set(self.names)))

    def test_no_whitespace_in_a_slug(self):
        self.assertTrue(all(" " not in n for n in self.names))

    def test_workday_is_not_hunted(self):
        # A Workday key is tenant, data centre and site. None of the three
        # follow from a company name, so guessing one is guaranteed waste.
        import sources
        self.assertNotIn(sources.WORKDAY, self.cli.HUNT_SOURCES)

    def test_every_hunted_source_can_be_fetched_by_name(self):
        import discover
        for source in self.cli.HUNT_SOURCES:
            self.assertIn(source, discover.SOURCES)


if __name__ == "__main__":
    unittest.main()
