"""Command line entry point.

    python poller/cli.py verify   # ping every configured board, report reality
    python poller/cli.py discover # mine ATS slugs out of the SimplifyJobs feed
    python poller/cli.py seed     # mark everything currently open as seen
    python poller/cli.py run      # one sweep: fetch, diff, notify, write state

`verify` is the command to run first. It reports which boards return real data
so the dead ones can be pruned. It never writes state.
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import tomllib
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import canada
import discover
import locations
import rank
import sources
import state
import workauth
from matcher import Matcher, min_years
from notify import Notifier

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")

# More than this many new matches in one sweep means something changed
# structurally (new slugs, a reset state file). Send one summary instead of
# carpet bombing the phone at 3am.
NOTIFY_BURST_LIMIT = 12

# Parallel fetches. Small enough to stay polite to any single ATS, large
# enough that a sweep finishes well inside the cron interval.
FETCH_WORKERS = 12

# How long a role survives on a board that has stopped confirming it. The
# rotation reaches every board about every twelve minutes, so anything near
# that is plenty; three days is the safety valve for a board that is renamed,
# deleted or permanently broken, whose roles would otherwise sit in the feed
# for good because nothing ever contradicts them.
CARRY_MAX_SECONDS = 3 * 86400


def load_config(path=CONFIG):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# --------------------------------------------------------------------------
# Board selection: hot boards every sweep, cold boards on rotation.
# --------------------------------------------------------------------------

def select_targets(cfg, registry, health):
    """All targets, and the subset to poll this sweep.

    Discovery turns ~70 boards into ~360. Polling all of them every minute
    would be roughly half a million requests a day and would get the tool
    blocked, so a board that recently produced a match stays "hot" and is
    polled every sweep, while the rest rotate through a fixed-size slice.
    """
    rotation = cfg.get("rotation", {})
    slice_size = int(rotation.get("cold_slice", 40))
    hot_seconds = int(rotation.get("hot_days", 21)) * 86400
    now = time.time()

    all_targets = list(sources.iter_configured(cfg, registry))
    hot, cold = [], []
    for target in all_targets:
        source, key = target
        # The aggregator and Amazon are always hot: they are few and they are
        # the backbone. Every discoverable ATS board goes through rotation,
        # which now matters a great deal more: discovery finds ~370 Workday
        # boards on its own, and polling those every sweep would be abuse.
        if source not in discover.SOURCES:
            hot.append(target)
            continue
        entry = registry.get(source, {}).get(key, {})
        # "hunted-ca" boards are hot for the same reason config ones are: they
        # were put there deliberately. They are the 75 Canadian employers the
        # hunt confirmed, and leaving them in a rotation that takes about
        # twelve minutes to come round would mean the roles this tool was
        # widened to catch are the ones it sees last. They are conditional
        # requests, so an unchanged board costs a 304 and no payload.
        if entry.get("origin") in ("config", "hunted-ca") or (
            entry.get("last_match") and now - entry["last_match"] < hot_seconds
        ):
            hot.append(target)
        else:
            cold.append(target)

    if not cold:
        return all_targets, hot

    offset = int(health.get("rotation_offset", 0)) % len(cold)
    window = cold[offset:offset + slice_size]
    if len(window) < slice_size:
        window += cold[: slice_size - len(window)]
    health["rotation_offset"] = (offset + slice_size) % len(cold)
    health["cold_boards"] = len(cold)
    health["hot_boards"] = len(hot)

    return all_targets, hot + window


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------

def sweep(cfg, health, registry, quiet=False, previous=None):
    """Fetch, filter, classify, dedupe and rank. Never raises for one source.

    `previous` is the last committed job list. It is required, not optional
    polish: a source answering 304 Not Modified has not said "I have no jobs",
    it has said "nothing changed since your ETag". Treating those the same
    silently empties the feed on the second sweep, which is exactly what
    happened before this argument existed.
    """
    matcher = Matcher(cfg)
    jobs = []
    results = []
    now = int(time.time())

    # Indexed source -> slug -> jobs. The lookup key is not always the job's
    # slug: the aggregator is configured as "SimplifyJobs/New-Grad-Positions"
    # but stamps its jobs with slug "listings", so single-board sources are
    # matched on source alone.
    carried = {}
    for job in previous or []:
        carried.setdefault(job.get("source"), {}).setdefault(
            job.get("slug"), []).append(job)

    # A change to the matching rules has to reach roles already in the feed.
    # A 304 carries them forward whole, classification and all, so without
    # this a widened matcher reaches that board only when it next changes its
    # listings. In practice one board out of 1072 holds an ETag, but it is the
    # aggregator: about 400 of the feed's 1071 roles.
    fingerprint = state.match_fingerprint(cfg)
    if health.get("match_fingerprint") != fingerprint:
        dropped = state.drop_etags(health)
        health["match_fingerprint"] = fingerprint
        if not quiet:
            print("  ~~ matching rules changed, dropped %d ETags so every board "
                  "is read fresh and reclassified" % dropped)

    all_targets, targets = select_targets(cfg, registry, health)
    polled = set(targets)

    def carry(source, key):
        """Keep a board's known roles when this sweep learned nothing new.

        A board is authoritative about its own roles only when it was polled
        AND answered with a payload. Three other things can happen, and all
        three mean "no news" rather than "no jobs":

          the rotation did not select it   ~900 boards, 80 a sweep
          it answered 304 Not Modified     nothing changed since the ETag
          the request failed               a timeout is not a closure

        Only the second was handled. The first is why a role could be pushed
        to the phone and then be missing from the dashboard a minute later:
        its board sat out the next rotation, so `jobs` was rebuilt without it
        and jobs.json was overwritten. At 987 boards and 80 a sweep a cold
        board is polled about once every twelve minutes, so its roles were in
        the feed for one sweep in twelve and gone for the other eleven.
        """
        by_slug = carried.get(source, {})
        if source in (sources.SIMPLIFY, sources.AMAZON):
            known = [j for group in by_slug.values() for j in group]
        else:
            known = by_slug.get(key, [])
        kept_jobs = []
        for job in known:
            # Stamped on the way through, so a job written before this field
            # existed gets a real clock instead of being carried forever.
            job.setdefault("confirmed_at", now)
            if now - job["confirmed_at"] <= CARRY_MAX_SECONDS:
                kept_jobs.append(job)
        jobs.extend(kept_jobs)
        return kept_jobs

    def fetch_one(target):
        source, key = target
        etag = state.etag_for(health, source, key)
        try:
            return target, sources.fetch(source, key, cfg, etag=etag)
        except Exception as exc:  # noqa: BLE001 - belt and braces
            result = sources.SourceResult(source, key)
            result.error = "uncaught: %s: %s" % (type(exc).__name__, exc)
            return target, result

    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        fetched = list(pool.map(fetch_one, targets))

    # URLs to mine ATS slugs out of. The aggregator was the only contributor
    # for a long time, and it is a US new grad feed, which is precisely why the
    # registry knew ~900 American boards and almost no Canadian ones. Job Bank,
    # Eluta and the Getro networks link to the employer's own ATS, so feeding
    # their results in here is what makes Canadian coverage compound: a board
    # learned once is polled directly forever after.
    discovery_urls = []

    for (source, key), result in fetched:
        results.append(result)
        state.record_source(health, source, key, result)

        if not result.ok:
            # A failed request is not a closure. Greenhouse timing out once
            # must not delete that company's roles from the feed.
            kept_before = carry(source, key)
            if not quiet:
                print("  !! %s/%s failed: %s (%d carried forward)"
                      % (source, key, result.error, len(kept_before)))
            continue
        if result.not_modified:
            # Unchanged, not empty. The known jobs were already filtered and
            # classified, so they carry forward as they are.
            kept_before = carry(source, key)
            if not quiet and kept_before:
                print("  == %s/%s unchanged (304), %d carried forward"
                      % (source, key, len(kept_before)))
            continue

        if source == sources.SIMPLIFY or source in canada.DISCOVERY_SOURCES:
            discovery_urls.extend(j.get("url") for j in result.jobs)

        # The Canadian aggregators are searched by keyword, and Job Bank shows
        # the NOC title rather than the employer's, so an early career signal
        # that was in the search never reaches the title. Where the query
        # itself carried one, the source vouches for it.
        signal = None
        if source in canada.SOURCES:
            signal = (canada.EARLY_CAREER_SOURCES.get(source)
                      or matcher.early_career_query(key))

        # Unlabelled software roles are accepted from CANADIAN boards only.
        # Turning them on everywhere would add them from all ~990 boards and
        # bury the new grad results under US mid-level ones, which is the
        # opposite of what was asked for. An audit measured 63 of them on the
        # Canadian boards against 122 total matches, so this is the largest
        # recoverable group there and a small, contained change here.
        canadian_board = (
            source in canada.SOURCES
            or registry.get(source, {}).get(key, {}).get("origin") == "hunted-ca"
        )
        open_level = canadian_board and cfg["match"].get("include_open_level", False)

        kept = 0
        for job in result.jobs:
            # Read out of the description, which only some sources return, and
            # read before the description is dropped below.
            years = min_years(job.get("description", ""))
            matched, reason, kind = matcher.evaluate_full(
                job.get("title", ""), signal, open_level, years)
            if not matched:
                continue
            region, evidence = locations.classify(job.get("locations"))
            # Where a remote role will actually hire, read from the location
            # strings and the description together, because a posting can say
            # "fully remote" in the headline and "must reside in the United
            # States" in the body.
            scope = locations.remote_scope(
                job.get("locations"), job.get("description", ""))
            if not locations.allowed(region, cfg, scope):
                continue

            status, auth_evidence = workauth.classify(
                title=job.get("title", ""),
                description=job.get("description", ""),
                company=job.get("company", ""),
            )
            job["region"] = region
            # Every region the posting covers, so the dashboard's Canada filter
            # also finds a role open in Toronto and New York. `region` alone
            # calls that one US and hides it.
            job["regions"] = locations.classify_all(job.get("locations"))
            # Remote is a property of the role, not a place. The region tag
            # prefers a country, so "Remote - US" is tagged US and a Remote
            # filter built on the region found 1 posting where 18 said remote.
            # A scope at all means the role is remote, including when only the
            # description said so, so the Remote filter finds it too.
            job["remote"] = bool(scope) or locations.is_remote(job.get("locations"))
            job["remote_scope"] = scope
            if scope in (locations.GLOBAL, locations.CA_OK):
                # So the dashboard's Canada filter finds a worldwide remote
                # role too. On a Canadian passport it is a Canadian option.
                job["regions"] = sorted(
                    set(job["regions"]) | {locations.GLOBAL, locations.CA})
            # "new grad" or "internship". Kept apart rather than blended: the
            # two have different deadlines and different value.
            job["kind"] = kind
            job["location_evidence"] = evidence
            job["match_reason"] = reason
            job["work_auth"] = status
            job["work_auth_evidence"] = auth_evidence

            # When this board last vouched for this role. It is what lets an
            # unpolled board's roles be carried without carrying them forever:
            # a board that is renamed or deleted stops refreshing the stamp,
            # and its roles age out.
            job["confirmed_at"] = now

            # The description is scanned for work authorization above and then
            # dropped. It is never committed: everything in data/ is public.
            job.pop("description", None)
            jobs.append(job)
            kept += 1

        if kept and source in discover.SOURCES:
            registry.setdefault(source, {}).setdefault(key, {})["last_match"] = now

        if not quiet and result.count:
            print("  ok %s/%s: %d postings, %d kept" % (source, key, result.count, kept))

    # Every board the rotation did not reach this sweep. Without this the feed
    # is not "all open roles", it is "roles on the ~80 boards polled in the
    # last minute", which is what made a pushed role missing from the board.
    unpolled = 0
    for target in all_targets:
        if target in polled:
            continue
        unpolled += len(carry(*target))
    if not quiet and unpolled:
        print("  .. %d roles carried from %d boards not polled this sweep"
              % (unpolled, len(all_targets) - len(polled)))

    # Discovery runs off payloads already fetched, so it costs no extra request.
    if discovery_urls and cfg.get("discovery", {}).get("enabled", True):
        found = discover.discover([{"url": url} for url in discovery_urls])
        before = sum(len(v) for v in registry.values())
        registry.update(discover.merge(registry, found, now))
        after = sum(len(v) for v in registry.values())
        if not quiet:
            print("  discovery: %d boards known (+%d new)" % (after, after - before))

    lag_samples = []
    jobs = rank.dedupe(jobs, lag_samples)
    rank.apply_ranking(jobs, now)

    # The aggregator's indexing lag, measured from roles that arrived both
    # ways. The dashboard adds it to the age of aggregator-only roles so the
    # number on screen is time since the company posted, not time since the
    # aggregator noticed.
    if lag_samples:
        lag_samples.sort()
        health.setdefault("source_lag_seconds", {})["simplify"] = int(
            lag_samples[len(lag_samples) // 2]
        )
        health["source_lag_samples"] = len(lag_samples)

    return jobs, results


def _by_uid(jobs):
    return {job["uid"]: job for job in jobs}


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------

def cmd_discover(cfg, args):
    print("Mining ATS slugs from the SimplifyJobs feed.\n")
    result = sources.fetch_simplify(cfg["sources"]["simplify"])
    if not result.ok:
        print("could not fetch the feed: %s" % result.error)
        return 1

    registry = state.load_slugs()
    now = int(time.time())
    before = {s: len(v) for s, v in registry.items()}

    found = discover.discover([{"url": j.get("url")} for j in result.jobs])
    registry = discover.merge(registry, found, now)
    registry = discover.seed_from_config(registry, cfg, now)
    state.save_slugs(registry)

    for source in discover.SOURCES:
        print("  %-16s %4d discovered | %4d known (was %d)"
              % (source, len(found.get(source, {})), len(registry.get(source, {})),
                 before.get(source, 0)))
    print("\n%d boards in data/slugs.json." % sum(len(v) for v in registry.values()))
    print("Run `verify` to find out which of them actually answer.")
    return 0


# --------------------------------------------------------------------------
# notify-test
# --------------------------------------------------------------------------

def cmd_notify_test(cfg, args):
    """Send one push, so the ntfy setup can be proved without waiting for a
    genuinely new role to appear."""
    notifier = Notifier(cfg)
    if not notifier.topic:
        print("NTFY_TOPIC is not set. Add it as an Actions secret.")
        return 2

    ok = notifier.job({
        "company": "Job watcher",
        "title": "Notifications are working",
        "locations": ["this is a test push, not a real role"],
        "region": "TEST",
        "source": "notify-test",
        "url": "https://htarek.systems/jobs",
    })
    if ok:
        print("Sent. If nothing arrives, the topic in the secret does not match "
              "the one the phone is subscribed to.")
        return 0
    print("Send failed. See the error above.")
    return 1


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def cmd_verify(cfg, args):
    print("Verifying configured boards. Nothing is written.\n")
    registry = state.load_slugs() if args.include_discovered else {}
    matcher = Matcher(cfg)
    rows = []

    targets = list(sources.iter_configured(cfg, registry))
    print("%d boards to check.\n" % len(targets))

    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        fetched = list(pool.map(lambda t: (t, sources.fetch(t[0], t[1], cfg)), targets))

    for (source, key), result in fetched:
        matched = sum(
            1 for job in result.jobs if matcher.matches(job.get("title", ""))
        ) if result.ok else 0

        if not result.ok:
            verdict, detail = "DEAD", (result.error or "no data")
        elif result.count == 0:
            verdict, detail = "EMPTY", "answered, returned zero postings"
        else:
            verdict = "OK"
            detail = "%d postings, %d match filters" % (result.count, matched)

        rows.append((verdict, source, key, result.status, int(result.seconds * 1000), detail))

    rows.sort(key=lambda r: (r[0] != "OK", r[1], str(r[2])))
    for verdict, source, key, _s, _ms, detail in rows:
        print("  %-5s  %-16s  %-30s  %s" % (verdict, source, str(key)[:30], detail))

    ok = [r for r in rows if r[0] == "OK"]
    empty = [r for r in rows if r[0] == "EMPTY"]
    dead = [r for r in rows if r[0] == "DEAD"]

    print("\n" + "=" * 72)
    print("OK    %3d   returning real postings" % len(ok))
    print("EMPTY %3d   reachable but zero postings" % len(empty))
    print("DEAD  %3d   unreachable or error" % len(dead))
    print("=" * 72)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Source verification\n\n")
            fh.write("**%d OK, %d empty, %d dead.**\n\n" % (len(ok), len(empty), len(dead)))
            fh.write("| verdict | source | board | status | ms | detail |\n|---|---|---|---|---|---|\n")
            for verdict, source, key, status, ms, detail in rows:
                fh.write("| %s | %s | `%s` | %s | %d | %s |\n"
                         % (verdict, source, key, status, ms, detail))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# probe
#
# The Canadian sources are HTML pages and undocumented JSON, not published
# APIs, and this sandbox has no egress to any of them, so their parsers cannot
# be written by looking at a response. `probe` is the substitute: it runs where
# egress is real, reports exactly what each endpoint answered, and on --dump
# prints enough of the raw body to repair a parser from the log.
#
# Nothing here writes state, and a source stays disabled in config.toml until
# a probe run has shown it returning real postings.
# --------------------------------------------------------------------------

PROBE_QUERY = "software developer"


def cmd_probe(cfg, args):
    import canada as ca
    import net

    targets = []
    # Probed includes the ones measured unreachable: the point of a probe is to
    # find out whether that is still true.
    for name in ca.SOURCES + ca.UNREACHABLE:
        block = cfg.get("sources", {}).get(name, {})
        keys = block.get("queries") or block.get("collections")
        if name == ca.GETRO and not keys:
            # Getro is addressed by network id, and a search term is not one.
            # Probing it with the default query only produced a confusing 404
            # about a URL containing a space.
            print("  SKIP     getro        no network ids configured\n")
            continue
        targets.append((name, str((keys or [PROBE_QUERY])[0])))

    print("Probing %d Canadian sources. Nothing is written.\n" % len(targets))
    rows = []

    for name, key in targets:
        result = ca.fetch(name, key)
        titles = [j["title"] for j in result.jobs if j.get("title")][:5]
        if not result.ok:
            verdict, detail = "DEAD", result.error or "no data"
        elif not result.jobs:
            verdict = "NOPARSE"
            detail = "HTTP %s answered but the parser found nothing" % result.status
        else:
            verdict = "OK"
            detail = "%d postings: %s" % (result.count, "; ".join(titles))
        rows.append((verdict, name, key, result.status,
                     int(result.seconds * 1000), detail))
        print("  %-8s %-12s %-24s %s" % (verdict, name, key[:24], detail[:110]))

        # Finding the right path one workflow run at a time is slow, so try
        # every candidate shape in the same run. Done for a working source too,
        # not only a failing one: for Job Bank the open question is not whether
        # the URL answers but which sort parameter orders by date, and that is
        # only visible by comparing what each one returns.
        if args.candidates:
            for url in ca.probe_urls(name, key)[1:]:
                # No retries. A candidate is being tested for whether it is the
                # right path, and repeating a wrong one three times with
                # backoff is how one probe run took nearly nine minutes.
                probe = net.get(url, headers=ca._BROWSER, retries=0,
                                timeout=ca.JOBBANK_TIMEOUT)
                # Path and query only. These candidates differ in their last
                # few characters, so a left truncated full URL printed four
                # identical looking lines.
                shown = url.split("//", 1)[-1].split("/", 1)[-1]
                print("      try %-58s HTTP %-4s %7d bytes  %s"
                      % (shown[-58:], probe.status, len(probe.body or ""),
                         _probe_parse(ca, name, key, probe.body)))

    if args.dump:
        print("\n" + "=" * 72)
        print("Raw bodies. This is here so a parser can be fixed from the log.")
        print("=" * 72)
        for name, key in targets:
            print("\n--- %s / %s ---" % (name, key))
            _dump_body(ca, name, key, args.dump)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Canadian source probe\n\n")
            fh.write("| verdict | source | key | status | ms | detail |\n"
                     "|---|---|---|---|---|---|\n")
            for verdict, name, key, status, ms, detail in rows:
                fh.write("| %s | %s | `%s` | %s | %d | %s |\n"
                         % (verdict, name, key, status, ms,
                            detail.replace("|", "\\|")[:300]))
    return 0 if any(r[0] == "OK" for r in rows) else 1


def _probe_parse(ca, name, key, body):
    """What this source's parser makes of a candidate page.

    A status code alone does not separate the right URL from a soft 200 error
    page, and for Job Bank it does not answer the other open question either:
    which sort parameter actually orders by date. Printing the newest posting
    the parser finds answers both from one run.
    """
    if not body:
        return ""
    parser = {
        ca.JOBBANK: ca._parse_jobbank,
        ca.JOBILLICO: ca._parse_jobillico,
        ca.TALENTEGG: ca._parse_talentegg,
        ca.ELUTA: ca._parse_eluta,
    }.get(name)
    if not parser:
        return ""
    try:
        jobs = parser(key, body)
    except Exception as exc:  # noqa: BLE001
        return "parser raised %s" % type(exc).__name__
    if not jobs:
        return "0 parsed"
    newest = max((j.get("posted_at") or 0) for j in jobs)
    return "%d parsed, newest %s, first %r" % (
        len(jobs),
        time.strftime("%Y-%m-%d", time.gmtime(newest)) if newest else "unknown",
        jobs[0]["title"][:40],
    )


_LINKISH = re.compile(
    r"job|emploi|search|recherche|poste|career|carriere|carrière|offre|opening",
    re.I)


def _dump_body(ca, name, key, limit):
    """Dump the first URL for this source that returns anything.

    Deliberately not the primary URL. A probe run showed Jobillico and
    TalentEgg answering 404 and 500 to every search path guessed for them while
    their front pages returned 322 KB and 185 KB, so dumping the primary just
    reprinted "0 bytes" and taught nothing. The front page is the thing that
    names the real search path, in its own navigation.
    """
    import net

    if name == ca.GETRO:
        resp = net.post(ca.GETRO_API % key,
                        json.dumps({"hitsPerPage": 5, "page": 0}).encode("utf-8"),
                        headers={"Content-Type": "application/json"})
    else:
        resp = None
        for url in ca.probe_urls(name, key):
            resp = net.get(url, headers=ca._BROWSER, retries=0)
            if resp.body:
                print("dumping %s" % url)
                break

    if not resp or not resp.body:
        print("HTTP %s, nothing to dump (%s)"
              % (resp.status if resp else 0, resp.error if resp else "no url"))
        return
    print("HTTP %s, %d bytes" % (resp.status, len(resp.body or "")))

    blocks = ca._LD_BLOCK.findall(resp.body)
    print("schema.org script blocks on page: %d" % len(blocks))
    for block in blocks[:2]:
        print("  LD: " + block.strip()[:600])

    # The paths this site uses for jobs, which is what the search URL has to be
    # built from. Reading them off the page beats another round of guessing:
    # six guesses at Jobillico's search path all 404'd.
    paths = []
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', resp.body):
        if not _LINKISH.search(href) or href.startswith(("mailto:", "tel:")):
            continue
        path = href.split("?")[0]
        if path not in paths:
            paths.append(path)
    print("\njob-ish link paths on the page (%d):" % len(paths))
    for path in paths[:40]:
        print("   " + path[:120])

    found = re.search(r"<article\b.*?</article>", resp.body, re.S | re.I)
    if found:
        print("\nfirst <article>:\n" + found.group(0)[:limit])
    else:
        print("\nno <article> element. First %d bytes:\n%s"
              % (limit, resp.body[:limit]))


# --------------------------------------------------------------------------
# audit
#
# `hunt` confirmed 75 Canadian boards and they contributed one role to the
# feed between them. The boards are alive, so the loss is downstream of the
# fetch, and there are four gates it could be. Guessing which one has already
# cost several rounds tonight, so this command polls the Canadian boards and
# reports every posting it drops and the reason, which turns "not many
# Canadian jobs" into a list of specific rejections to argue with.
#
# Writes nothing.
# --------------------------------------------------------------------------

def cmd_audit(cfg, args):
    import canada as ca

    registry = state.load_slugs()
    matcher = Matcher(cfg)

    targets = [(source, slug)
               for source, block in (registry.get("sources") or registry).items()
               for slug, entry in block.items()
               if isinstance(entry, dict) and entry.get("origin") == "hunted-ca"]
    targets += list(ca.iter_configured(cfg))
    print("Auditing %d Canadian boards and queries.\n" % len(targets))

    with concurrent.futures.ThreadPoolExecutor(max_workers=HUNT_WORKERS) as pool:
        fetched = list(pool.map(
            lambda t: (t, sources.fetch(t[0], t[1], cfg)), targets))

    postings = kept = 0
    canadian = []
    reasons = {}
    dead = 0

    for (source, key), result in fetched:
        if not result.ok:
            dead += 1
            continue
        signal = None
        if source in ca.SOURCES:
            signal = (ca.EARLY_CAREER_SOURCES.get(source)
                      or matcher.early_career_query(key))
        for job in result.jobs:
            postings += 1
            region, _ = locations.classify(job.get("locations"))
            remote = locations.is_remote(job.get("locations"))
            if region != locations.CA and not remote:
                continue
            matched, reason, _kind = matcher.evaluate_full(
                job.get("title", ""), signal)
            if matched:
                kept += 1
                continue
            # Collapse "excluded by 'senior'" and friends into one bucket each.
            bucket = reason.split(" on ")[0]
            reasons[bucket] = reasons.get(bucket, 0) + 1
            canadian.append((source, key, job.get("company") or "",
                             job.get("title") or "", bucket))

    print("%d postings across %d live boards (%d boards failed)."
          % (postings, len(targets) - dead, dead))
    print("%d matched. %d are in Canada or remote and were dropped.\n"
          % (kept, len(canadian)))

    print("Why they were dropped:")
    for bucket, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("  %4d  %s" % (count, bucket))

    print("\nA sample of what is being dropped, by reason:")
    for bucket, _count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("\n  --- %s ---" % bucket)
        shown = [row for row in canadian if row[4] == bucket][:12]
        for source, key, company, title, _b in shown:
            print("    %-14s %-24s %s" % (source, company[:24], title[:64]))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Canadian board audit\n\n")
            fh.write("%d postings, %d matched, %d Canadian or remote dropped.\n\n"
                     % (postings, kept, len(canadian)))
            fh.write("| dropped | reason |\n|---|---|\n")
            for bucket, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
                fh.write("| %d | %s |\n" % (count, bucket))
    return 0


# --------------------------------------------------------------------------
# hunt
#
# The other half of the Canadian gap. Discovery reads slugs out of SimplifyJobs
# posting URLs, and SimplifyJobs is a US new grad aggregator, so the registry
# ended up knowing ~900 American boards and almost none here. A Canadian
# company can be on Greenhouse with an obvious slug and still be invisible,
# because nothing ever mentioned it.
#
# So: take a list of Canadian employers, try each name as a slug on every
# platform whose board key is a company name, and keep only the ones that
# answer with real postings. That is guessing, but it is guessing that gets
# checked before anything is written, which is the difference between this and
# the 24 hand written slugs that were all dead.
#
# Workday is excluded. Its key is tenant plus data centre plus site, and none
# of those three follow from a company name.
# --------------------------------------------------------------------------

HUNT_SOURCES = (sources.GREENHOUSE, sources.LEVER, sources.ASHBY,
                sources.SMARTRECRUITERS, sources.BAMBOOHR, sources.RIPPLING,
                sources.WORKABLE, sources.RECRUITEE)

SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "canada_seed.txt")

# Higher than a sweep's, because these are one round trip each against eight
# different hosts rather than a sustained load on any one of them.
HUNT_WORKERS = 32


def load_names(path):
    names = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                names.append(line)
    # Preserve order but drop repeats, so the same name in two regional
    # sections does not cost a second round of requests.
    return list(dict.fromkeys(names))


def cmd_hunt(cfg, args):
    import net

    names = load_names(args.names)
    if args.offset or args.limit:
        names = names[args.offset:args.offset + (args.limit or len(names))]
    matcher = Matcher(cfg)
    targets = [(source, name) for name in names for source in HUNT_SOURCES]

    # Almost every one of these is a slug that does not exist, and a miss is a
    # verdict rather than a transient failure. Retrying each one three times
    # with backoff pushed the first run past the workflow timeout without
    # learning anything, so retries are off and the pool is wider: nearly all
    # of these requests end in a 404 that costs one round trip.
    net.RETRIES = 0
    print("Testing %d names across %d platforms: %d boards to try.\n"
          % (len(names), len(HUNT_SOURCES), len(targets)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=HUNT_WORKERS) as pool:
        fetched = list(pool.map(
            lambda t: (t, sources.fetch(t[0], t[1], cfg)), targets))

    found = []
    for (source, name), result in fetched:
        if not result.ok or not result.jobs:
            continue
        matched = sum(1 for j in result.jobs if matcher.matches(j.get("title", "")))
        # A board's location mix is the thing worth knowing here: a slug that
        # answers but only ever posts in the US adds nothing this registry did
        # not already have.
        canadian = 0
        for job in result.jobs:
            region, _ = locations.classify(job.get("locations"))
            if region == locations.CA:
                canadian += 1
        found.append((source, name, result.count, canadian, matched))

    found.sort(key=lambda r: (-r[3], -r[4], r[0], r[1]))
    for source, name, count, canadian, matched in found:
        print("  LIVE  %-16s %-24s %4d postings, %3d in Canada, %2d match"
              % (source, name, count, canadian, matched))

    live_ca = [r for r in found if r[3] > 0]
    print("\n" + "=" * 72)
    print("%d of %d tried boards answered with postings." % (len(found), len(targets)))
    print("%d of those post in Canada." % len(live_ca))
    print("=" * 72)

    if args.write and found:
        registry = state.load_slugs()
        now = int(time.time())
        added = promoted = 0

        # "hunted-ca" now carries two consequences: the board is polled every
        # sweep rather than rotated, and unlabelled software roles are accepted
        # from it. Both are meant for boards that actually post in Canada, so
        # only those get the tag. A board that answers with nothing Canadian is
        # still recorded, so it is polled on rotation, but as a plain find.
        for source, name, _count, canadian, _matched in found:
            block = registry.setdefault(source, {})
            entry = block.get(name)
            origin = "hunted-ca" if canadian else "hunted"

            if entry is None:
                block[name] = {"first_seen": now, "last_seen": now,
                               "origin": origin}
                added += 1
                continue
            # A config board is already hot and was chosen by hand; leave it.
            if entry.get("origin") == "config" or entry.get("origin") == origin:
                continue
            # Discovery already knew this board, but as a cold one, which is
            # the case the first version of this skipped: 86 boards were added
            # to the registry by discovery and stayed on a twelve minute
            # rotation even after a hunt confirmed they post in Canada.
            if origin == "hunted-ca":
                entry["origin"] = origin
                entry["last_seen"] = now
                promoted += 1

        state.save_slugs(registry)
        print("\nAdded %d new boards, promoted %d that discovery already knew."
              % (added, promoted))
    elif found:
        print("\nNothing written. Re-run with --write to add these to the registry.")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Canadian board hunt\n\n")
            fh.write("**%d live boards, %d of them posting in Canada, from %d tried.**\n\n"
                     % (len(found), len(live_ca), len(targets)))
            fh.write("| source | slug | postings | in Canada | match filters |\n"
                     "|---|---|---|---|---|\n")
            for source, name, count, canadian, matched in found:
                fh.write("| %s | `%s` | %d | %d | %d |\n"
                         % (source, name, count, canadian, matched))
    return 0


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------

def cmd_seed(cfg, args):
    print("Seeding. Everything currently open is marked seen. No notifications.\n")
    health = state.load_health()
    registry = state.load_slugs()
    previous = state.load_jobs().get("jobs", [])
    jobs, _ = sweep(cfg, health, registry, previous=previous)

    seen = state.load_seen()
    now = int(time.time())
    for uid in _by_uid(jobs):
        seen.setdefault(uid, now)

    state.save_seen(seen)
    state.save_jobs(list(_by_uid(jobs).values()), now)
    state.save_slugs(registry)
    health["last_sweep"] = now
    health["seeded_at"] = now
    state.save_health(health)

    print("\nSeeded %d matching roles. Zero notifications sent." % len(jobs))
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_run(cfg, args):
    started = time.time()
    health = state.load_health()
    registry = state.load_slugs()

    if not health.get("seeded_at") and not args.allow_unseeded:
        print("State has never been seeded. Refusing to run, because the first "
              "sweep would push every currently open role at once.\n"
              "Run `python poller/cli.py seed` first, or pass --allow-unseeded.")
        return 2

    previous = {j["uid"]: j for j in state.load_jobs().get("jobs", [])}
    jobs, results = sweep(cfg, health, registry, previous=list(previous.values()))
    now = int(time.time())

    seen = state.load_seen()
    current = _by_uid(jobs)

    # Closure detection. A uid that was open last sweep and is gone now has
    # closed; its lifetime is the number that tests the 48 hour premise.
    closed = state.load_closures()
    for uid, job in previous.items():
        if uid in current:
            continue
        first = seen.get(uid) or job.get("posted_at")
        if not first:
            continue
        closed.append({
            "uid": uid,
            "company": job.get("company"),
            "title": job.get("title"),
            "closed_at": now,
            "hours_open": round((now - first) / 3600.0, 1),
        })

    new = [job for uid, job in current.items() if uid not in seen]
    # Best first, not merely newest: a blocked or stale role should not be the
    # thing that wakes you up.
    new.sort(key=lambda j: (-j.get("score", 0), -(j.get("posted_at") or 0)))

    takeable = [j for j in new if workauth.is_takeable(j.get("work_auth"))]
    blocked = len(new) - len(takeable)

    notifier = Notifier(cfg, dry_run=args.dry_run)
    if not notifier.topic and not args.dry_run:
        print("\nNTFY_TOPIC is not set. State written, nothing pushed.")

    print("\n%d matching roles open, %d new (%d blocked and not alerted)."
          % (len(current), len(new), blocked))

    if takeable:
        if len(takeable) > NOTIFY_BURST_LIMIT:
            print("  burst of %d exceeds limit %d, sending one summary"
                  % (len(takeable), NOTIFY_BURST_LIMIT))
            notifier.batch_summary(len(takeable))
        else:
            for job in takeable:
                notifier.job(job)
                print("  -> [%3d] %s - %s" % (job.get("score", 0),
                                              job.get("company"), job.get("title")))

    for uid in current:
        seen.setdefault(uid, now)

    state.save_seen(seen)
    state.save_jobs(list(current.values()), now)
    state.save_slugs(registry)
    state.save_closures(closed)

    health["last_sweep"] = now
    health["last_sweep_seconds"] = round(time.time() - started, 2)
    health["sources_ok"] = sum(1 for r in results if r.ok)
    health["sources_failed"] = sum(1 for r in results if not r.ok)
    health["boards_known"] = sum(len(v) for v in registry.values())
    health["closures"] = state.closure_summary(closed)
    state.save_health(health)

    print("Sweep finished in %.1fs. %d pushed, %d push failures."
          % (time.time() - started, notifier.sent, notifier.failed))
    return 0


# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="poller")
    parser.add_argument("--config", default=CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    ver = sub.add_parser("verify", help="ping every board and report reality")
    ver.add_argument("--include-discovered", action="store_true",
                     help="also verify the ~360 auto-discovered boards")
    sub.add_parser("discover", help="mine ATS slugs from the SimplifyJobs feed")

    pro = sub.add_parser("probe", help="check the Canadian sources answer, and how")
    pro.add_argument("--dump", type=int, nargs="?", const=3000, default=0,
                     metavar="BYTES",
                     help="print raw response bodies, to fix a parser from the log")
    pro.add_argument("--candidates", action="store_true",
                     help="for a source that failed, try every known URL shape")

    sub.add_parser("audit", help="report what the Canadian boards drop, and why")

    hunt = sub.add_parser(
        "hunt", help="test Canadian company names as slugs on every platform")
    hunt.add_argument("--names", default=SEED_FILE)
    hunt.add_argument("--write", action="store_true",
                      help="add the boards that answered to data/slugs.json")
    hunt.add_argument("--offset", type=int, default=0)
    hunt.add_argument("--limit", type=int, default=0,
                      help="only test this many names, so a long list can be "
                           "split across runs")

    sub.add_parser("seed", help="mark everything currently open as seen")
    sub.add_parser("notify-test", help="send one push to prove ntfy is wired up")

    run = sub.add_parser("run", help="one sweep")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--allow-unseeded", action="store_true")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    return {
        "verify": cmd_verify,
        "discover": cmd_discover,
        "probe": cmd_probe,
        "hunt": cmd_hunt,
        "audit": cmd_audit,
        "notify-test": cmd_notify_test,
        "seed": cmd_seed,
        "run": cmd_run,
    }[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
