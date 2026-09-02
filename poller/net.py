"""Minimal HTTP helper. Standard library only, on purpose.

Nothing here raises out of a sweep; callers get a Response with .ok False and
an .error string instead, so one dead endpoint cannot take the run down.
"""

import gzip
import json
import time
import urllib.error
import urllib.request

USER_AGENT = "new-grad-watcher/1.0 (+https://github.com/)"
TIMEOUT = 25
RETRIES = 2
BACKOFF = 1.5


class Response:
    def __init__(self, ok, status=0, body=None, error=None, etag=None, seconds=0.0):
        self.ok = ok
        self.status = status
        self.body = body
        self.error = error
        self.etag = etag
        self.seconds = seconds

    @property
    def not_modified(self):
        return self.status == 304

    def json(self):
        if not self.body:
            return None
        return json.loads(self.body)


def post(url, body, headers=None, timeout=TIMEOUT, retries=1):
    """POST a body. Same contract as get: returns a Response, never raises.

    Workday's job endpoint is a POST, so this exists for it. Retries are lower
    because a POST that got through once should not be replayed eagerly.
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    last = None
    for attempt in range(retries + 1):
        started = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return Response(ok=True, status=resp.status,
                                body=raw.decode("utf-8", "replace"),
                                seconds=time.time() - started)
        except urllib.error.HTTPError as exc:
            last = Response(ok=False, status=exc.code, error="HTTP %d" % exc.code,
                            seconds=time.time() - started)
            if exc.code in (404, 410, 401, 403, 400):
                return last
        except Exception as exc:  # noqa: BLE001
            last = Response(ok=False, error="%s: %s" % (type(exc).__name__, exc),
                            seconds=time.time() - started)
        if attempt < retries:
            time.sleep(BACKOFF * (attempt + 1))
    return last


def get(url, headers=None, etag=None, timeout=TIMEOUT, retries=RETRIES):
    """GET a URL. Returns a Response, never raises.

    Passing `etag` sends If-None-Match, which is what keeps the 13 MB
    SimplifyJobs listing from being re-downloaded on every one of the ~288
    sweeps a day.
    """
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "application/json",
    }
    if headers:
        hdrs.update(headers)
    if etag:
        hdrs["If-None-Match"] = etag

    last = None
    for attempt in range(retries + 1):
        started = time.time()
        try:
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return Response(
                    ok=True,
                    status=resp.status,
                    body=raw.decode("utf-8", "replace"),
                    etag=resp.headers.get("ETag"),
                    seconds=time.time() - started,
                )
        except urllib.error.HTTPError as exc:
            elapsed = time.time() - started
            if exc.code == 304:
                return Response(ok=True, status=304, seconds=elapsed)
            last = Response(
                ok=False,
                status=exc.code,
                error="HTTP %d" % exc.code,
                seconds=elapsed,
            )
            # 404 and 410 are verdicts, not transient. Do not burn retries.
            if exc.code in (404, 410, 401, 403):
                return last
        except Exception as exc:  # noqa: BLE001 - a sweep must survive anything
            last = Response(
                ok=False,
                error="%s: %s" % (type(exc).__name__, exc),
                seconds=time.time() - started,
            )
        if attempt < retries:
            time.sleep(BACKOFF * (attempt + 1))
    return last
