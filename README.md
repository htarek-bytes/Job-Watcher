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

**The live dashboard is <https://htarek.systems/jobs>.** This repo is public, so
the dashboard reads `data/jobs.json` and `data/health.json` straight from it
over raw.githubusercontent, which sends `Access-Control-Allow-Origin: *`. There
is no separate data repo and no write token: one less secret, and one less
thing to expire silently.

Public also matters for cost. On a private repo GitHub Free gives 2,000 Actions
minutes a month, and this workflow's cadence burns roughly 1,440 minutes a day,
so the quota would be gone in under two days. Public repos are unlimited.

## Secrets

One, `NTFY_TOPIC`: the ntfy.sh topic your phone subscribes to. Anyone who knows
it can read your alerts, so treat it as a password. Prove it works with
Actions -> poll -> Run workflow -> mode `notify-test`, which sends a single
push and reports what happened.

## Cadence

The workflow carries five cron entries offset by a minute each, so it fires
roughly every minute rather than every five. That shortens the gap between
sweeps. It does **not** remove the 10-20 minute scheduler jitter, and does not
stop GitHub dropping scheduled runs under load — it only means a dropped fire is
followed by another a minute later instead of five. Getting under the jitter
floor needs a scheduler outside Actions.

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
python poller/cli.py probe --candidates  # check the Canadian sources, and how
python poller/cli.py hunt                # test Canadian names as slugs everywhere
python poller/cli.py hunt --write        # and keep the ones that answered
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
| Workday | `POST {tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | 20 per page, unsorted, so queried by search term |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{slug}/postings` | cleanest public API here |
| BambooHR | `{slug}.bamboohr.com/careers/list` | small, plain JSON |
| Rippling | `api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` | small |
| Workable | `apply.workable.com/api/v1/widget/accounts/{slug}` | small and mid sized employers |
| Recruitee | `{slug}.recruitee.com/api/offers/` | same |
| Amazon | `www.amazon.jobs/en/search.json` | unofficial and brittle, isolated |
| SimplifyJobs | `.github/scripts/listings.json` on branch `dev` | coverage net |

### Canada

Everything above is keyed by a company slug, and the slug registry is mined
from SimplifyJobs, which is a US new grad aggregator. The result was a feed of
496 US roles against 24 Canadian ones. That is structural: the pipeline had no
way to learn that a 150 person company in Waterloo exists.

These are national aggregators instead, one endpoint covering many employers.

| source | endpoint | notes |
|---|---|---|
| Job Bank | `www.jobbank.gc.ca/jobsearch/jobsearch?searchstring={q}&sort=M` | the federal board, the widest Canadian coverage there is |
| Jobillico | `www.jobillico.com/recherche-emploi?skwd={q}` | Quebec's largest, invisible from outside the province |
| TalentEgg | `talentegg.ca/find-a-job/keyword/{q}` | Canadian, new grad and campus only |

None of the three publishes an API contract, and none of these URLs were
written from memory. `cli.py probe` runs them from a machine with real egress
and reports what came back; a source is only enabled in `config.toml` once it
has shown real postings parsing. Getting there took several rounds:

* Job Bank shows the **NOC title**, so a req the employer called "Junior
  Software Developer" is listed as plain "software developer" and fails the
  matcher's fourth gate. Where the search term itself carried an early career
  signal, the source vouches for it. That substitutes for the fourth gate and
  nothing else: the exclusions and the seniority suffix are still read off the
  real title.
* Job Bank puts a `jsessionid` in every result link. Taken verbatim, the same
  posting gets a different URL on every fetch, which defeats dedupe and makes
  the diff report old roles as new forever. The posting id is read out of the
  path and the URL rebuilt.
* Jobillico 404s on `/recherche-emploi/{keyword}` even though its own front
  page links to that shape; those segments are canned category pages. Only the
  query string form searches.
* Eluta refuses the TLS handshake to a non browser client, on every attempt.
  It is listed as unreachable and never polled.

Canadian company boards come from `cli.py hunt`, which takes the employers in
`poller/canada_seed.txt`, tries each name as a slug on all eight platforms
whose key is a company name, and writes only the ones that answered with real
postings. A run on 2026-09-02 confirmed **75 boards** this way. Those are
polled every sweep rather than rotated, for the same reason config boards are:
they were added deliberately.

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

## Merging a branch, and why data/ conflicts

The poller commits `data/` on every sweep, about once a minute, so any branch
that lives longer than a minute conflicts with the default branch on files
nobody edits by hand. Resolving those by picking a side is wrong:
`data/seen.json` is what stops a role being pushed to the phone twice, so
dropping the uids the other side had already notified re-notifies all of them.

`tools/merge-state.py` merges each of those files on its own terms. Run this
once per clone, because a merge driver has to live in git config and does not
travel with the repository:

```bash
git config merge.jobstate.name "job watcher state merge"
git config merge.jobstate.driver "python3 tools/merge-state.py %O %A %B %P"
```

Note the limit: **GitHub's merge button does not run it.** The web merge
happens on GitHub's servers, which never see the driver, so a pull request can
still report a conflict there. This helps when merging locally, which is where
these get resolved. `union`, git's usual answer for append-only files, is not
an option: these are JSON documents and union interleaves both sides' lines
into something the poller cannot parse.

## State files

| file | contents |
|---|---|
| `data/jobs.json` | current open matching roles, what the dashboard renders |
| `data/seen.json` | every uid ever seen, so a role alerts exactly once |
| `data/health.json` | per source last success, error, latency, ETag, zero-result streak |

Job descriptions are deliberately **not** committed. They are fetched for the
Phase 2 work authorization scan and dropped before writing, because everything
in `data/` is served publicly by Pages.

## Coverage, and how it is maintained

The hand-written slug lists were guesswork: of 70, **5** appeared in the live
feed. `discover` mines ATS slugs out of the SimplifyJobs posting URLs instead,
which yields **~420 boards that are correct by construction** and keeps finding
new ones on every sweep. Slugs are never auto-dropped — a company with no new
grad req today still has a working board — so pruning stays a `verify`
decision.

Discovery also cuts latency: hitting Greenhouse directly sees a req the moment
it is live, hours before the same role reaches an aggregator.

**Rotation.** ~420 boards at a one-minute cadence would be roughly half a
million requests a day and would get the tool blocked. Each sweep polls every
*hot* board (hand-configured, or one that produced a match in the last 21 days)
plus a rotating slice of the rest, so the whole set is covered every few
minutes while per-sweep request count stays bounded. Tune under `[rotation]`.

Measured platform share of the feed, which is what drove the two new fetchers:

| platform | share of active postings | supported |
|---|---|---|
| Workday | 27.8% | yes (new) |
| Greenhouse | 10.6% | yes |
| SmartRecruiters | 6.9% | yes (new) |
| Oracle | 6.3% | not yet |
| Ashby | 6.1% | yes |
| iCIMS | 4.5% | not yet |
| Lever | 4.0% | yes |

## Work authorization

Four outcomes, most severe first: `blocked`, `closed`, `open`, `unknown`.

`blocked` is the addition that matters. A security clearance, an explicit US
citizenship requirement, or an ITAR-gated employer is not a sponsorship
question — it cannot be applied to at all. **11% of matching roles (47 of 448
in the seed sweep)** fall here: RTX, SpaceX, Johns Hopkins APL, L3Harris,
Peraton, Leidos, CACI. They are scored 0, never notified, and hidden by default
on the dashboard.

Every call carries the phrase that produced it. This is a keyword guess on
someone else's prose and is never presented as a decision.

Note that SimplifyJobs carries **no job descriptions**, so roles arriving only
from there can be classified by employer name but not by posting text. Direct
ATS sources return full descriptions and classify properly — another reason
discovery matters.

## Dedupe and ranking

Duplicates are collapsed on `(company, normalized title)`, keeping the
direct-ATS copy and the earliest posting time, and recording what was absorbed.
The seed sweep collapsed **527 raw matches to 448** — one staffing firm was
posting the same role twenty times.

Ranking is a 0-100 score, sorted best-first by default, with every adjustment
listed in `score_reasons` so a bad ordering can be diagnosed rather than
guessed at. Sponsorship signal, freshness, direct-ATS sourcing, and Canadian
location push a role up; staffing firms, no-sponsorship statements, heavy
reposting, and age push it down.

## Does speed actually matter?

`closures.json` records how long each role stayed open, and `health.json`
carries the median. This is the number that tests the premise the tool was
built on. If reqs really do close inside 48 hours, minute-level polling earns
its keep. If the median turns out to be two weeks, the effort belongs in
targeting instead, and the dashboard will say so.

## Not built yet

`health.json` records zero-result streaks per source, but nothing alerts on
them yet — that is the "the tool is broken" notification.

Oracle Recruiting (6.3%) and iCIMS (4.5%) have no fetcher yet; together they
are the next ~11% of coverage.

`docs/index.html` is a placeholder — the real dashboard is the Next.js page in
the portfolio repo.

## Non goals

No auto apply. No LinkedIn or Indeed scraping — both block it and it would get
the tool banned. No database. No login.
