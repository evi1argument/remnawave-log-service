"""Measure dedicated SOCKS test routes and publish a private, timestamped cache."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.parse import urlsplit

from statusboard import process_lock, write_json


def request(url, proxy=None):
    command = ["curl", "-q", "--silent", "--fail", "--proto", "=https",
               "--connect-timeout", "4", "--max-time", "9", "--max-filesize", "4096"]
    if proxy is None:
        command.extend(("--noproxy", "*"))
    else:
        command.extend(("--noproxy", "", "--proxy", f"socks5h://127.0.0.1:{proxy}"))
    try:
        result = subprocess.run([*command, "--", url], capture_output=True, timeout=11, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or len(result.stdout) > 4096:
        return None
    try:
        matches = [line[3:].strip() for line in result.stdout.decode("utf-8").splitlines() if line.startswith("ip=")]
        return ipaddress.ip_address(matches[0]) if len(matches) == 1 else None
    except ValueError:
        return None


def route_state(url, port, expected, control):
    if control is None or control in expected:
        return "unknown"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError:
        return "unknown"
    answer = request(url, port)
    if answer in expected:
        return "ok"
    retry = request(url, port)
    if retry in expected:
        return "ok"
    if answer == retry:
        return "needs_control"
    return "unknown"


def run(settings):
    url = settings["verifier"]
    verifier = urlsplit(url)
    if (verifier.scheme != "https" or not verifier.hostname or verifier.username is not None
            or verifier.query or verifier.fragment):
        raise ValueError("verifier must be a credential-free HTTPS URL returning an ip= line")
    routes = settings["routes"]
    if not isinstance(routes, dict) or not 1 <= len(routes) <= 20:
        raise ValueError("expected 1–20 routes")
    targets = []
    ports = set()
    for route_id, route in routes.items():
        port = route["port"]
        ips = route["expected_egress"]
        if (not re.fullmatch(r"[a-z0-9]{15}", route_id) or type(port) is not int
                or not 1024 <= port <= 65535 or port in ports or not isinstance(ips, list) or not ips):
            raise ValueError("invalid route, duplicated port or missing expected egress")
        ports.add(port)
        targets.append((route_id, port, {ipaddress.ip_address(ip) for ip in ips}))

    control = request(url) or request(url)

    def check(target):
        route_id, port, expected = target
        state = route_state(url, port, expected, control)
        return route_id, {"kind": "vpn_e2e", "state": state, "checked_at": int(time.time())}

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = dict(pool.map(check, targets))
    pending = [entry for entry in results.values() if entry["state"] == "needs_control"]
    if pending:
        state = "failed" if request(url) == control else "unknown"
        for entry in pending:
            entry["state"] = state
    return {"version": 1, "routes": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    settings = json.loads(args.config.read_text(encoding="utf-8"))
    cache = Path(settings["probe_cache"])
    with process_lock(cache.with_name(cache.name + ".lock")):
        write_json(cache, run(settings), 0o600)
