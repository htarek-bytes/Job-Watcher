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
import os
import sys
import time
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
        # Non-slug sources (the aggregator, Amazon, explicitly configured
        # boards) are always hot; they are few and they are the backbone.
        if source not in (sources.GREENHOUSE, sources.LEVER, sources.ASHBY):
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

def sweep(cfg, health, registry, quiet=False):
    """Fetch, filter, classify, dedupe and rank. Never raises for one source."""
    matcher = Matcher(cfg)
    jobs = []
    results = []

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
    raw_simplify = None

    for (source, key), result in fetched:
        results.append(result)
        state.record_source(health, source, key, result)

        if not result.ok:
            if not quiet:
                print("  !! %s/%s failed: %s" % (source, key, result.error))
            continue
        if result.not_modified:
            continue

        if source == sources.SIMPLIFY:
            raw_simplify = result.jobs

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
            # Descriptions are scanned, never committed: data/ is published.
            job.pop("description", None)
            jobs.append(job)
            kept += 1

        if kept and source in (sources.GREENHOUSE, sources.LEVER, sources.ASHBY):
            registry.setdefault(source, {}).setdefault(key, {})["last_match"] = now

        if not quiet and result.count:
            print("  ok %s/%s: %d postings, %d kept" % (source, key, result.count, kept))

    # Discovery runs off the aggregator payload we already fetched.
    if raw_simplify is not None and cfg.get("discovery", {}).get("enabled", True):
        found = discover.discover(
            [{"url": j.get("url")} for j in raw_simplify]
        )
        before = sum(len(v) for v in registry.values())
        registry.update(discover.merge(registry, found, now))
        after = sum(len(v) for v in registry.values())
        if not quiet:
            print("  discovery: %d boards known (+%d new)" % (after, after - before))

    jobs = rank.dedupe(jobs)
    rank.apply_ranking(jobs, now)
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

    for source in (discover.GREENHOUSE, discover.LEVER, discover.ASHBY):
        print("  %-11s %3d discovered | %3d known (was %d)"
              % (source, len(found.get(source, {})), len(registry.get(source, {})),
                 before.get(source, 0)))
    print("\n%d boards in data/slugs.json." % sum(len(v) for v in registry.values()))
    print("Run `verify` to find out which of them actually answer.")
    return 0


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
# seed
# --------------------------------------------------------------------------

def cmd_seed(cfg, args):
    print("Seeding. Everything currently open is marked seen. No notifications.\n")
    health = state.load_health()
    registry = state.load_slugs()
    jobs, _ = sweep(cfg, health, registry)

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
    jobs, results = sweep(cfg, health, registry)
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
    sub.add_parser("seed", help="mark everything currently open as seen")

    run = sub.add_parser("run", help="one sweep")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--allow-unseeded", action="store_true")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    return {
        "verify": cmd_verify,
        "discover": cmd_discover,
        "seed": cmd_seed,
        "run": cmd_run,
    }[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
