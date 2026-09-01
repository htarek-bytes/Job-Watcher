"""Command line entry point.

    python poller/cli.py verify   # ping every configured slug, report reality
    python poller/cli.py seed     # mark everything currently open as seen, no pushes
    python poller/cli.py run      # one sweep: fetch, diff, notify, write state

`verify` is the command to run first. It reports which configured slugs return
real data so the dead ones can be pruned. It never writes state.
"""

import argparse
import concurrent.futures
import os
import sys
import time
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import locations
import sources
import state
from matcher import Matcher
from notify import Notifier

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")

# More than this many new matches in one sweep means something changed
# structurally (a new slug, a reset state file). Send one summary instead of
# carpet bombing the phone at 3am.
NOTIFY_BURST_LIMIT = 12

# Parallel fetches. Small enough to stay polite to any single ATS, large
# enough that a sweep finishes well inside the five minute cron interval.
FETCH_WORKERS = 12


def load_config(path=CONFIG):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def cmd_verify(cfg, args):
    print("Verifying configured sources. Nothing is written.\n")
    rows = []
    matcher = Matcher(cfg)

    targets = list(sources.iter_configured(cfg))
    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        fetched = list(pool.map(
            lambda t: (t, sources.fetch(t[0], t[1], cfg)), targets
        ))

    for (source, key), result in fetched:
        matched = 0
        if result.ok:
            for job in result.jobs:
                if matcher.matches(job.get("title", "")):
                    matched += 1

        if not result.ok:
            verdict = "DEAD"
            detail = result.error or "no data"
        elif result.count == 0:
            verdict = "EMPTY"
            detail = "endpoint answered, returned zero postings"
        else:
            verdict = "OK"
            detail = "%d postings, %d match filters" % (result.count, matched)

        rows.append((verdict, source, key, result.status, int(result.seconds * 1000), detail))
        print("  %-5s  %-10s  %-28s  %s" % (verdict, source, str(key)[:28], detail))

    print()
    ok = [r for r in rows if r[0] == "OK"]
    empty = [r for r in rows if r[0] == "EMPTY"]
    dead = [r for r in rows if r[0] == "DEAD"]

    print("=" * 72)
    print("OK    %3d   returning real postings" % len(ok))
    print("EMPTY %3d   reachable but zero postings (usually a wrong slug)" % len(empty))
    print("DEAD  %3d   unreachable or error" % len(dead))
    print("=" * 72)

    if empty or dead:
        print("\nPrune these from poller/config.toml:\n")
        for verdict, source, key, status, _ms, detail in empty + dead:
            print("  [%s] %s / %s  ->  %s (%s)" % (verdict, source, key, detail, status))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Source verification\n\n")
            fh.write("| verdict | source | slug | status | ms | detail |\n")
            fh.write("|---|---|---|---|---|---|\n")
            for verdict, source, key, status, ms, detail in rows:
                fh.write("| %s | %s | `%s` | %s | %d | %s |\n"
                         % (verdict, source, key, status, ms, detail))
            fh.write("\n**%d OK, %d empty, %d dead.**\n" % (len(ok), len(empty), len(dead)))

    return 0 if ok else 1


# --------------------------------------------------------------------------
# sweep, shared by seed and run
# --------------------------------------------------------------------------

def sweep(cfg, health):
    """Fetch every source. Returns (jobs, results). Never raises for one source.

    Fetches run in parallel. Sequentially, ~70 slugs with retries and backoff
    took over six minutes in testing, which is longer than the five minute cron
    interval -- sweeps would queue behind each other and the backlog would grow
    all day. The work is entirely network-bound, so threads are the right tool
    and the pool stays small enough not to look like abuse to any one API.
    """
    matcher = Matcher(cfg)
    jobs = []
    results = []

    targets = list(sources.iter_configured(cfg))

    def fetch_one(target):
        source, key = target
        etag = state.etag_for(health, source, key)
        try:
            return target, sources.fetch(source, key, cfg, etag=etag)
        except Exception as exc:  # noqa: BLE001 - belt and braces around a fetcher
            result = sources.SourceResult(source, key)
            result.error = "uncaught: %s: %s" % (type(exc).__name__, exc)
            return target, result

    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        fetched = list(pool.map(fetch_one, targets))

    for (source, key), result in fetched:
        results.append(result)
        state.record_source(health, source, key, result)

        if not result.ok:
            print("  !! %s/%s failed: %s" % (source, key, result.error))
            continue
        if result.not_modified:
            print("  == %s/%s unchanged (304)" % (source, key))
            continue

        kept = 0
        for job in result.jobs:
            matched, reason = matcher.evaluate(job.get("title", ""))
            if not matched:
                continue
            region, evidence = locations.classify(job.get("locations"))
            if not locations.allowed(region, cfg):
                continue
            job["region"] = region
            job["location_evidence"] = evidence
            job["match_reason"] = reason
            # The description is only needed for the Phase 2 work authorization
            # scan; it is not committed, because jobs.json is served publicly.
            job.pop("description", None)
            jobs.append(job)
            kept += 1

        print("  ok %s/%s: %d postings, %d kept" % (source, key, result.count, kept))

    return jobs, results


def _by_uid(jobs):
    out = {}
    for job in jobs:
        out[job["uid"]] = job
    return out


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------

def cmd_seed(cfg, args):
    print("Seeding. Everything currently open is marked seen. No notifications.\n")
    health = state.load_health()
    jobs, _ = sweep(cfg, health)

    seen = state.load_seen()
    now = int(time.time())
    added = 0
    for uid in _by_uid(jobs):
        if uid not in seen:
            seen[uid] = now
            added += 1

    state.save_seen(seen)
    state.save_jobs(list(_by_uid(jobs).values()), now)
    health["last_sweep"] = now
    health["seeded_at"] = now
    state.save_health(health)

    print("\nSeeded %d matching roles (%d newly recorded). Zero notifications sent."
          % (len(jobs), added))
    print("The next `run` will only alert on genuinely new postings.")
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_run(cfg, args):
    started = time.time()
    health = state.load_health()

    if not health.get("seeded_at") and not args.allow_unseeded:
        print("State has never been seeded. Refusing to run, because the first "
              "sweep would push every currently open role at once.\n"
              "Run `python poller/cli.py seed` first, or pass --allow-unseeded.")
        return 2

    jobs, results = sweep(cfg, health)
    now = int(time.time())

    seen = state.load_seen()
    current = _by_uid(jobs)
    new = [job for uid, job in current.items() if uid not in seen]
    new.sort(key=lambda j: j.get("posted_at") or 0, reverse=True)

    notifier = Notifier(cfg, dry_run=args.dry_run)
    if not notifier.topic and not args.dry_run:
        print("\nNTFY_TOPIC is not set. State will be written, nothing will be pushed.")

    print("\n%d matching roles open, %d new since last sweep." % (len(current), len(new)))

    if new:
        if len(new) > NOTIFY_BURST_LIMIT:
            print("  burst of %d exceeds limit %d, sending one summary"
                  % (len(new), NOTIFY_BURST_LIMIT))
            notifier.batch_summary(len(new))
        else:
            for job in new:
                notifier.job(job)
                print("  -> %s - %s" % (job.get("company"), job.get("title")))

    for uid in current:
        seen.setdefault(uid, now)

    state.save_seen(seen)
    state.save_jobs(list(current.values()), now)
    health["last_sweep"] = now
    health["last_sweep_seconds"] = round(time.time() - started, 2)
    health["sources_ok"] = sum(1 for r in results if r.ok)
    health["sources_failed"] = sum(1 for r in results if not r.ok)
    state.save_health(health)

    print("Sweep finished in %.1fs. %d pushed, %d push failures."
          % (time.time() - started, notifier.sent, notifier.failed))
    return 0


# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="poller")
    parser.add_argument("--config", default=CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("verify", help="ping every configured slug and report reality")
    sub.add_parser("seed", help="mark everything currently open as seen, no pushes")

    run = sub.add_parser("run", help="one sweep: fetch, diff, notify, write state")
    run.add_argument("--dry-run", action="store_true",
                     help="print notifications instead of sending them")
    run.add_argument("--allow-unseeded", action="store_true",
                     help="run even though state was never seeded")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "verify":
        return cmd_verify(cfg, args)
    if args.command == "seed":
        return cmd_seed(cfg, args)
    return cmd_run(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
