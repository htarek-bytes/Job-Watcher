"""ntfy.sh notifier.

The topic is read from the NTFY_TOPIC environment variable, which the workflow
supplies from an Actions secret. It is never written to a file in the repo,
because everything in the repo is served publicly by Pages, and an ntfy topic
name is effectively its own password.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
TIMEOUT = 15


class Notifier:
    def __init__(self, cfg, topic=None, dry_run=False):
        self.topic = topic or os.environ.get("NTFY_TOPIC", "")
        self.dry_run = dry_run
        notify_cfg = cfg.get("notify", {})
        self.priority = notify_cfg.get("priority", "urgent")
        self.tags = notify_cfg.get("tags", "briefcase")
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self):
        return bool(self.topic) and not self.dry_run

    def _post(self, title, body, click=None, priority=None, tags=None):
        if self.dry_run or not self.topic:
            print("    [dry-run] %s | %s | %s" % (title, body.replace("\n", " ")[:80], click or "-"))
            return True

        headers = {
            "Title": title.encode("utf-8"),
            "Priority": priority or self.priority,
            "Tags": tags or self.tags,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if click:
            # One tap apply: the notification opens the application page itself,
            # not the dashboard and not a search page.
            headers["Click"] = click

        url = "%s/%s" % (BASE.rstrip("/"), self.topic)
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                resp.read()
            self.sent += 1
            return True
        except Exception as exc:  # noqa: BLE001 - a failed push must not end the sweep
            self.failed += 1
            print("    notify failed: %s: %s" % (type(exc).__name__, exc))
            return False

    def job(self, job):
        title = "%s - %s" % (job.get("company", "?"), job.get("title", "?"))
        locs = ", ".join(job.get("locations", [])[:3]) or "location not stated"
        region = job.get("region", "")
        lines = [locs]
        if region:
            lines.append("region: %s" % region)
        lines.append("via %s" % job.get("source", "?"))
        return self._post(
            title[:200],
            "\n".join(lines),
            click=job.get("url") or None,
        )

    def batch_summary(self, count):
        return self._post(
            "%d new roles" % count,
            "More matches than fit in individual alerts. Open the dashboard.",
            priority="high",
        )
