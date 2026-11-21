"""Collect, publish Remnawave status."""

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


INTERVAL = 900
WINDOW = 96
MAX_AGE = 1200
PROBE_MAX_AGE = 180
RETAIN_SLOTS = 7 * WINDOW
MSK = timezone(timedelta(hours=3))
STATES = ("up", "down", "degraded", "maintenance", "unknown")
LOCATION_STATES = {
    "up": "working", "down": "offline", "degraded": "degraded",
    "maintenance": "maintenance", "unknown": "unknown",
}


def aggregate(states):
    active = [state if state in STATES else "unknown" for state in states if state != "maintenance"]
    if not active:
        return "maintenance" if states else "unknown"
    if all(state == "down" for state in active):
        return "down"
    if any(state in ("down", "degraded") for state in active):
        return "degraded"
    return "unknown" if "unknown" in active else "up"


def panel_observation(records, locations):
    if not isinstance(records, list):
        raise ValueError("panel response must contain a list")
    grouped = {code: [] for code in locations}
    for node in records:
        if not isinstance(node, dict):
            raise ValueError("invalid panel node")
        code = node.get("countryCode")
        if not isinstance(code, str):
            raise ValueError("invalid panel location")
        if code not in grouped:
            continue
        disabled, connected = node.get("isDisabled"), node.get("isConnected")
        if disabled is True:
            continue
        if disabled is not False or type(connected) is not bool:
            grouped[code].append("unknown")
        else:
            grouped[code].append("up" if connected else "down")
    return {code: aggregate(states) for code, states in grouped.items()}


def probe_observation(path, routes, now):
    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized probe cache")
        doc = json.loads(raw)
        entries = doc.get("routes") if isinstance(doc, dict) and type(doc.get("version")) is int and doc["version"] == 1 else None
    except (OSError, ValueError, RecursionError):
        entries = None
    if not isinstance(entries, dict):
        return {code: "unknown" for code in routes}

    results = {}
    for code, route_ids in routes.items():
        outcomes = []
        for route_id in route_ids:
            entry = entries.get(route_id)
            state = "unknown"
            if isinstance(entry, dict) and entry.get("kind") == "vpn_e2e":
                stamp = entry.get("checked_at")
                if type(stamp) in (int, float) and now - PROBE_MAX_AGE <= stamp <= now:
                    state = {"ok": "up", "failed": "down"}.get(str(entry.get("state")), "unknown")
            outcomes.append(state)
        results[code] = aggregate(outcomes)
    return results


def load_settings(path):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    source = cfg["source"]
    if source not in ("control_plane", "vpn_e2e"):
        raise ValueError("unsupported source")
    locations = cfg["locations"]
    if not isinstance(locations, dict) or not 1 <= len(locations) <= 20:
        raise ValueError("expected 1–20 locations")
    if any(not code.isascii() or not code.isalpha() or not 2 <= len(code) <= 12
           or not isinstance(name, str) or not name.strip() or len(name) > 80
           for code, name in locations.items()) or len(set(locations.values())) != len(locations):
        raise ValueError("invalid/duplicate location")
    routes = cfg.get("routes", {})
    if source == "vpn_e2e":
        if set(routes) != set(locations) or any(not isinstance(ids, list) or not ids for ids in routes.values()):
            raise ValueError("every VPN location needs at least one registered probe route")
        ids = [route for group in routes.values() for route in group]
        if any(not isinstance(route, str) or len(route) != 15 or not route.isascii()
               or not route.isalnum() or route.lower() != route for route in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid/duplicate probe route")
        if not cfg.get("probe_cache"):
            raise ValueError("VPN mode requires probe_cache")
    else:
        url = urlsplit(cfg["panel_url"])
        if (url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "::1", "localhost")
                or url.username is not None or url.query or url.fragment):
            raise ValueError("panel_url must be a local endpoint without credentials")
    for item in cfg.get("maintenance", []):
        start, end = item["start"], item["end"]
        if (item["location"] not in locations or type(start) is not int or type(end) is not int
                or start < 0 or end <= start or start % INTERVAL or end % INTERVAL):
            raise ValueError("maintenance UTC epochs must align with a slot")
    identity = [source, cfg.get("series_revision", 1), sorted(locations), routes if source == "vpn_e2e" else {}]
    cfg["series"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return cfg


def connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            series TEXT NOT NULL,
            slot INTEGER NOT NULL,
            observed INTEGER NOT NULL,
            states TEXT NOT NULL,
            has_observation INTEGER NOT NULL,
            PRIMARY KEY (series, slot)
        )
    """)
    return db


def normalize_states(reading, locations):
    states = {}
    for code in locations:
        state = reading.get(code)
        states[code] = state if state in STATES else "unknown"
    return states


def record(db, cfg, observed, states):
    slot = observed - observed % INTERVAL
    states = normalize_states(states, cfg["locations"])
    has_observation = any(state in ("up", "down", "degraded") for state in states.values())
    for window in cfg.get("maintenance", []):
        if window["start"] <= slot < window["end"]:
            states[window["location"]] = "maintenance"
    with db:
        # A retry can fill a wholly unobserved slot before it closes.
        db.execute("""
            INSERT INTO readings VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (series, slot) DO UPDATE SET
                observed = excluded.observed, states = excluded.states, has_observation = 1
            WHERE readings.has_observation = 0 AND excluded.has_observation = 1
        """, (cfg["series"], slot, observed, json.dumps(states), has_observation))
        db.execute("""
            DELETE FROM readings WHERE series = ? AND slot <= (
                SELECT slot FROM readings WHERE series = ? ORDER BY slot DESC LIMIT 1 OFFSET ?
            )
        """, (cfg["series"], cfg["series"], RETAIN_SLOTS))


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def read_states(raw, locations):
    try:
        reading = json.loads(raw)
    except (ValueError, RecursionError):
        reading = None
    if not isinstance(reading, dict):
        reading = {}
    return normalize_states(reading, locations)


def snapshot(db, cfg, now):
    current = now - now % INTERVAL
    first = current - WINDOW * INTERVAL
    rows = db.execute("""
        SELECT slot, observed, states FROM readings
        WHERE series = ? AND slot BETWEEN ? AND ? AND observed <= ?
        ORDER BY slot
    """, (cfg["series"], first, current, now)).fetchall()
    by_slot = {slot: read_states(states, cfg["locations"]) for slot, _, states in rows}
    history = []
    for slot in range(first, current, INTERVAL):
        states = by_slot.get(slot, {})
        state = aggregate([states.get(code, "unknown") for code in cfg["locations"]])
        label = datetime.fromtimestamp(slot, MSK).strftime("%d.%m %H:%M")
        history.append({"time": label, "state": state})
    counts = Counter(item["state"] for item in history)
    measured = counts["up"] + counts["down"] + counts["degraded"]
    # Integer rounding matches the page's two-decimal percentage, including half-cent ties.
    uptime = ((20_000 * counts["up"] + measured) // (2 * measured)) / 100 if measured else None
    _, observed, stored = rows[-1] if rows else (None, None, "{}")
    fresh = observed is not None and now - observed <= MAX_AGE
    latest = read_states(stored, cfg["locations"]) if fresh else {}
    return {
        "schema": 2,
        "source": cfg["source"],
        "generatedAt": stamp(now),
        "observedAt": stamp(observed) if observed is not None else None,
        "updated": datetime.fromtimestamp(observed, MSK).strftime("%H:%M") if observed is not None else "—",
        "locations": [[name, LOCATION_STATES[latest.get(code, "unknown")]]
                      for code, name in cfg["locations"].items()],
        "history": history,
        "uptime": uptime,
        "uptimeChecks": measured,
        "uptimeWindow": "24h",
        "coverage": {
            "measured": measured,
            "maintenance": counts["maintenance"],
            "unknown": counts["unknown"],
            "total": WINDOW,
        },
    }


def write_json(path, payload, mode):
    handle, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    tmp = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.chmod(tmp, mode)
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_panel_adapter(path):
    spec = importlib.util.spec_from_file_location("statusboard_panel_adapter", path)
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    return legacy


def collect(cfg, legacy=None):
    if cfg["source"] == "vpn_e2e":
        return probe_observation(Path(cfg["probe_cache"]), cfg["routes"], int(time.time()))
    try:
        data = legacy.http_json(
            cfg["panel_url"],
            headers={"Authorization": "Bearer " + legacy.panel_token(),
                     "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"},
            timeout=12,
        )
        return panel_observation(data["response"], cfg["locations"])
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, AttributeError) as exc:
        print("panel observation unavailable:", type(exc).__name__, file=sys.stderr)
        return dict.fromkeys(cfg["locations"], "unknown")


@contextlib.contextmanager
def process_lock(path):
    import fcntl
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "preview"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    cfg = load_settings(args.config)
    base = Path(cfg["private_dir"])
    guard = process_lock(base / "collect.lock") if args.command == "collect" else contextlib.nullcontext()
    with guard:
        database = base / "readings.sqlite"
        db = connect(database) if args.command == "collect" else sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        with contextlib.closing(db):
            if args.command == "collect":
                started = int(time.time())
                adapter = None
                if cfg["source"] == "control_plane":
                    adapter = load_panel_adapter(Path(cfg.get("panel_adapter") or cfg["legacy_board"]))
                states = collect(cfg, adapter)
                observed = int(time.time())
                if not 0 <= observed - started < INTERVAL:
                    raise RuntimeError("observation exceeded its time budget or clock moved backward")
                record(db, cfg, observed, states)
            now = int(time.time())
            payload = snapshot(db, cfg, now)
            if args.command == "collect":
                write_json(Path(cfg["public_json"]), payload, 0o644)
            else:
                print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
        # Transport exceptions may contain credential-bearing URLs.
        print("statusboard:", type(exc).__name__, file=sys.stderr)
        sys.exit(1)
