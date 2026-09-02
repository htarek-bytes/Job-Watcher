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
from matcher import Matcher
from notify import Notifier

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")

# More than this many new matches in one sweep means something changed
# structurally (new slugs, a reset state file). Send one summary instead of
# carpet bombing the phone at 3am.
NOTIFY_BURST_LIMIT = 12

# Parallel fetches. Small enough to stay polite to any single ATS, large
# enough that a sweep finishes well inside the cron interval.
FETCH_WORKERS = 12


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
        if entry.get("origin") == "config" or (
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

    # Indexed source -> slug -> jobs. The lookup key is not always the job's
    # slug: the aggregator is configured as "SimplifyJobs/New-Grad-Positions"
    # but stamps its jobs with slug "listings", so single-board sources are
    # matched on source alone.
    carried = {}
    for job in previous or []:
        carried.setdefault(job.get("source"), {}).setdefault(
            job.get("slug"), []).append(job)

    _, targets = select_targets(cfg, registry, health)

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

    now = int(time.time())
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
            if not quiet:
                print("  !! %s/%s failed: %s" % (source, key, result.error))
            continue
        if result.not_modified:
            # Unchanged, not empty. Carry the last known jobs for this board
            # forward; they were already filtered and classified.
            by_slug = carried.get(source, {})
            if source in (sources.SIMPLIFY, sources.AMAZON):
                kept_before = [j for group in by_slug.values() for j in group]
            else:
                kept_before = by_slug.get(key, [])
            jobs.extend(kept_before)
            if not quiet and kept_before:
                print("  == %s/%s unchanged (304), %d carried forward"
                      % (source, key, len(kept_before)))
            continue

        if source == sources.SIMPLIFY or source in canada.DISCOVERY_SOURCES:
            discovery_urls.extend(j.get("url") for j in result.jobs)

        kept = 0
        for job in result.jobs:
            matched, reason = matcher.evaluate(job.get("title", ""))
            if not matched:
                continue
            region, evidence = locations.classify(job.get("locations"))
            if not locations.allowed(region, cfg):
                continue

            status, auth_evidence = workauth.classify(
                title=job.get("title", ""),
                description=job.get("description", ""),
                company=job.get("company", ""),
            )
            job["region"] = region
            job["location_evidence"] = evidence
            job["match_reason"] = reason
            job["work_auth"] = status
            job["work_auth_evidence"] = auth_evidence

            # The description is scanned for work authorization above and then
            # dropped. It is never committed: everything in data/ is public.
            job.pop("description", None)
            jobs.append(job)
            kept += 1

        if kept and source in discover.SOURCES:
            registry.setdefault(source, {}).setdefault(key, {})["last_match"] = now

        if not quiet and result.count:
            print("  ok %s/%s: %d postings, %d kept" % (source, key, result.count, kept))

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

    targets = []
    for name in ca.SOURCES:
        block = cfg.get("sources", {}).get(name, {})
        keys = block.get("queries") or block.get("collections") or [PROBE_QUERY]
        targets.append((name, str(keys[0])))

    print("Probing %d Canadian sources. Nothing is written.\n" % len(targets))
    rows = []

    for name, key in targets:
        result = ca.fetch(name, key)
        body = getattr(result, "_body", None)
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


def _dump_body(ca, name, key, limit):
    """Refetch one endpoint raw, so the log shows what the parser had to work
    with: the markup around a result, and any schema.org block on the page."""
    import net

    urls = {
        ca.JOBBANK: ca.JOBBANK_SEARCH % urllib.parse.quote_plus(key),
        ca.JOBILLICO: ca.JOBILLICO_SEARCH % urllib.parse.quote_plus(key),
        ca.TALENTEGG: ca.TALENTEGG_SEARCH % urllib.parse.quote_plus(key),
        ca.ELUTA: ca.ELUTA_SEARCH % urllib.parse.quote_plus(key),
    }
    if name == ca.GETRO:
        resp = net.post(ca.GETRO_API % key,
                        json.dumps({"hitsPerPage": 5, "page": 0}).encode("utf-8"),
                        headers={"Content-Type": "application/json"})
    else:
        resp = net.get(urls[name], headers=ca._BROWSER)

    print("HTTP %s, %d bytes" % (resp.status, len(resp.body or "")))
    if not resp.body:
        print("(empty body: %s)" % resp.error)
        return

    blocks = ca._LD_BLOCK.findall(resp.body)
    print("schema.org script blocks on page: %d" % len(blocks))
    for block in blocks[:2]:
        print("  LD: " + block.strip()[:600])

    # The first result element, which is what a site specific parser reads.
    found = re.search(r"<article\b.*?</article>", resp.body, re.S | re.I)
    if found:
        print("\nfirst <article>:\n" + found.group(0)[:limit])
    else:
        print("\nno <article> element. First %d bytes:\n%s"
              % (limit, resp.body[:limit]))


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
        "notify-test": cmd_notify_test,
        "seed": cmd_seed,
        "run": cmd_run,
    }[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
