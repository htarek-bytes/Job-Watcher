# new grad job watcher

Polls job board APIs from GitHub Actions, pushes anything new that matches to
your phone via ntfy, and commits the results as JSON for a static dashboard.

**Status: Phase 1 complete. The slug lists in `poller/config.toml` are UNVERIFIED.**
Run the verify workflow before trusting them. See [Verify first](#verify-first).

## How it fits together

```
GitHub Actions (cron */5)          the only thing that runs code
   |
   |-- poller/  fetch -> match -> diff against data/seen.json
   |                                |
   |                                +--> ntfy.sh  --> your phone (urgent)
   |                                +--> commit data/*.json back to the repo
   |
GitHub Pages (docs/)  fetches data/jobs.json and renders it. Display only.
```

There is no server. The repo is the database: Actions can write to it, Pages
serves it, so a committed JSON file is both the poller's memory and the
dashboard's API.

## Why Python, standard library only

Actions runners ship Python 3.11 already. The poller imports nothing outside
the standard library, so there is no `pip install` step in the sweep: no
dependency resolution, no lockfile churn in a job that runs 288 times a day,
no third party code in the path of a credential-bearing workflow, and nothing
that can break because an index was briefly unavailable. `tomllib` (3.11+)
reads the config, `urllib` does the fetching.

Fetches run on a small thread pool. Sequentially, ~70 slugs with retries and
backoff measured **372 seconds** — longer than the five minute cron interval,
which would have made sweeps queue behind each other and grow a backlog all
day. In parallel the same run takes **33 seconds**.

## Real latency, not the advertised number

`*/5` is the floor GitHub allows on `schedule`. It is not what you get.
Scheduled workflows are queued on shared infrastructure and are routinely
**10 to 20 minutes late**, occasionally worse at the top of the hour when
every cron in the world fires at once. GitHub documents the delay but not a
bound.

So the honest end-to-end expectation is roughly **5 to 25 minutes** from a
posting appearing in an API to your phone buzzing, and the API itself may lag
the company's careers page by minutes more. Phase 2 measures this properly and
puts the observed median on the dashboard, so the number you act on is
measured rather than assumed. If it comes out bad, Phase 4 is the discussion
about a faster lane.

## Setup

1. **Pick an ntfy topic.** Choose something long and unguessable, e.g.
   `newgrad-a7f3c9d2e1`. Anyone who knows the topic name can read your alerts,
   so treat it as a password.
2. **Install ntfy** on your phone and subscribe to that topic. Allow
   notifications to bypass Do Not Disturb, otherwise the urgent priority is
   wasted.
3. **Add the secret.** Repo Settings → Secrets and variables → Actions → New
   repository secret, named `NTFY_TOPIC`, value = your topic name.
   It goes here and nowhere else. Never in `config.toml`, never in `docs/` —
   everything in this repo is publicly served by Pages.
4. **Verify the slugs** (below), and prune the dead ones.
5. **Seed**, so the first real sweep does not push hundreds of alerts:
   Actions → poll → Run workflow → mode `seed`.
6. The cron takes over from there.

## Verify first

The slug lists shipped in `poller/config.toml` are **candidates, not a tested
seed list**. They were written from knowledge of which companies use which
ATS, and have not been confirmed against the live endpoints.

Actions → **verify sources** → Run workflow.

It pings every configured slug and prints a table:

| verdict | meaning | action |
|---|---|---|
| `OK` | returned real postings | keep |
| `EMPTY` | endpoint answered, zero postings | almost always a wrong slug — delete it |
| `DEAD` | 404, error, or unreachable | delete it |

Delete every `EMPTY` and `DEAD` row from `config.toml`. The report is also
written to the workflow summary and uploaded as an artifact.

`verify` writes no state and sends no notifications, so it is safe to run any
time.

## Commands

```bash
python poller/cli.py verify              # ping every slug, report reality
python poller/cli.py seed                # mark everything open as seen, no pushes
python poller/cli.py run --dry-run       # full sweep, print alerts instead of sending
python poller/cli.py run                 # the real thing
python -m unittest discover -s poller/tests
```

`run` refuses to start if state was never seeded, because the first sweep on
empty state would push every currently open role at once. Override with
`--allow-unseeded` if you actually want that.

## Matching

Four gates, in `poller/matcher.py`:

1. the title names a role worth waking up for (`role_keywords`)
2. the title does not hit an `exclude_keyword`
3. the title does not end in a seniority level above I
4. the title shows a new grad signal — an explicit phrase, or a level-I suffix

Every decision carries a human readable reason (`match_reason` in
`jobs.json`), so a wrong call can be checked rather than guessed at.

Two traps this is built around, both hit by hand first and both covered by
tests:

- **A bare `" 2"` exclude kills every posting containing 2027.** Four digit
  years are stripped before any level reasoning runs.
- **Substring matching on `"ii"` fires inside ordinary words** (`Hawaii`).
  Every keyword test is anchored on word boundaries.

Tuning lives entirely in `config.toml`. Note that bare `"engineer"` is an
include keyword, so that `New Grad Engineer` matches; the cost is some
non-software noise (`Hardware Engineer 1`, `Outcome Engineer`). Remove it from
`role_keywords` if that bothers you more than the misses would.

### Locations

US + Canada + Remote are kept, other countries dropped, unplaceable postings
kept and tagged `UNKNOWN` — losing a real US req to keep the feed tidy is the
expensive mistake. Configured under `[locations]`.

## Sources

| source | endpoint | notes |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | includes full description |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | includes full description |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}` | includes full description |
| Amazon | `www.amazon.jobs/en/search.json` | unofficial and brittle, isolated |
| SimplifyJobs | `.github/scripts/listings.json` on branch `dev` | coverage net |

Amazon is wrapped in its own exception boundary: it is an undocumented
endpoint that can change shape without warning, and it must not be able to
take down a sweep. Every other fetcher is equally non-raising, but Amazon is
the one expected to break.

The SimplifyJobs path was confirmed by reading the repository, not guessed:
`.github/scripts/listings.json`, branch `dev`, about 13 MB and 19k records, of
which roughly 3.2k are active. It is fetched with a conditional request
(`If-None-Match`), so the payload only crosses the wire when it actually
changed — otherwise 288 sweeps a day would pull ~3.7 GB from
raw.githubusercontent.

That feed carries its own `sponsorship` field. **It is not usable as a work
authorization signal**: on inspection, 3,229 of 3,240 active rows said
`"Other"`. It is recorded but never trusted. The Phase 2 classifier reads
actual job descriptions instead.

## State files

| file | contents |
|---|---|
| `data/jobs.json` | current open matching roles, what the dashboard renders |
| `data/seen.json` | every uid ever seen, so a role alerts exactly once |
| `data/health.json` | per source last success, error, latency, ETag, zero-result streak |

Job descriptions are deliberately **not** committed. They are fetched for the
Phase 2 work authorization scan and dropped before writing, because everything
in `data/` is served publicly by Pages.

## Not built yet

Phase 2 (work authorization classifier, time-to-alert median, application
tracker, source-health alerting, cross-source dedupe) and Phase 3 (the
dashboard) are not implemented. `docs/index.html` is a placeholder.

`health.json` already records zero-result streaks per source, but nothing
alerts on them yet — that is the Phase 2 "the tool is broken" notification.

Cross-source dedupe is also Phase 2, so until then the same role arriving from
both an ATS and SimplifyJobs will alert twice.

## Non goals

No auto apply. No LinkedIn or Indeed scraping — both block it and it would get
the tool banned. No database. No login.
