"""Committed state: data/jobs.json, data/seen.json, data/health.json.

The repo is the database. That is the whole trick that lets this run on Pages
with no backend: Actions has write access to the repo, Pages serves the repo,
so a committed JSON file is both the poller's memory and the dashboard's API.
"""

import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

JOBS = os.path.join(DATA, "jobs.json")
SEEN = os.path.join(DATA, "seen.json")
HEALTH = os.path.join(DATA, "health.json")


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------

def load_seen():
    """uid -> epoch first seen."""
    data = _read(SEEN, {})
    return data.get("uids", {}) if isinstance(data, dict) else {}


def save_seen(uids):
    _write(SEEN, {"count": len(uids), "updated_at": int(time.time()), "uids": uids})


def load_jobs():
    return _read(JOBS, {"generated_at": 0, "jobs": []})


def save_jobs(jobs, swept_at=None):
    _write(JOBS, {
        "generated_at": int(swept_at or time.time()),
        "count": len(jobs),
        "jobs": jobs,
    })


def load_health():
    return _read(HEALTH, {"sources": {}, "last_sweep": 0})


def save_health(health):
    _write(HEALTH, health)


def source_key(source, key):
    return "%s:%s" % (source, key)


def record_source(health, source, key, result):
    """Update the per-source record. Zero-result streaks are counted here;
    acting on them (the "tool is broken" alert) is Phase 2."""
    sources = health.setdefault("sources", {})
    entry = sources.setdefault(source_key(source, key), {})
    now = int(time.time())

    entry["source"] = source
    entry["key"] = key
    entry["last_attempt"] = now
    entry["last_status"] = result.status
    entry["last_latency_ms"] = int(result.seconds * 1000)

    if result.ok:
        entry["last_success"] = now
        entry["last_error"] = None
        if not result.not_modified:
            entry["last_count"] = result.count
            entry["consecutive_zero"] = (
                entry.get("consecutive_zero", 0) + 1 if result.count == 0 else 0
            )
        etag = getattr(result, "etag", None)
        if etag:
            entry["etag"] = etag
    else:
        entry["last_error"] = result.error
        entry["consecutive_error"] = entry.get("consecutive_error", 0) + 1
        return entry

    entry["consecutive_error"] = 0
    return entry


def etag_for(health, source, key):
    return health.get("sources", {}).get(source_key(source, key), {}).get("etag")
