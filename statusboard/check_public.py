"""Read the public status from another host and report publisher/coverage health."""

import argparse
from datetime import datetime
import json
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from statusboard import MAX_AGE, STATES, WINDOW


def age_of(value, now):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.utcoffset() is None:
            return None
        return now - timestamp.timestamp()
    except (ValueError, AttributeError, OverflowError, OSError):
        return None


def check(payload, now):
    if (not isinstance(payload, dict) or payload.get("schema") != 2
            or payload.get("source") not in ("control_plane", "vpn_e2e")):
        return "invalid_status"
    generated = age_of(payload.get("generatedAt"), now)
    if generated is None or not -5 <= generated <= MAX_AGE:
        return "publisher_stale"
    observed = age_of(payload.get("observedAt"), now)
    if observed is None or not -5 <= observed <= MAX_AGE or observed < generated:
        return "collector_stale"
    locations = payload.get("locations")
    if not isinstance(locations, list):
        return "invalid_status"
    states = [entry[1] if isinstance(entry, list) and len(entry) == 2 else None for entry in locations]
    if not states or any(state not in ("working", "offline", "degraded", "maintenance", "unknown")
                         for state in states):
        return "invalid_status"
    history = payload.get("history")
    if (not isinstance(history, list) or len(history) != WINDOW
            or any(not isinstance(entry, dict) or entry.get("state") not in STATES for entry in history)):
        return "invalid_status"
    if "unknown" in states:
        return "missing_observations"
    return "ok"


def read(url):
    target = urlsplit(url)
    if target.scheme != "https" or not target.hostname or target.username is not None:
        raise ValueError("expected credential-free HTTPS status URL")
    with urlopen(url, timeout=8) as response:
        if response.geturl() != url:
            raise ValueError("unexpected status redirect")
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("oversized status JSON")
    return json.loads(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args()
    try:
        state = check(read(args.url), time.time())
    except (TypeError, ValueError, RecursionError, OSError):
        state = "status_fetch_failed"
    print(state, file=sys.stdout if state == "ok" else sys.stderr)
    sys.exit(0 if state == "ok" else 1)
