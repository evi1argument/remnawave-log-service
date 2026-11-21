"""Create a private initial statusboard configuration from a local panel."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import adapter
from statusboard import panel_observation


def configure(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username is not None or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") != "/api/nodes"):
        raise ValueError("Use the local Remnawave /api/nodes URL.")
    token = adapter.panel_token()
    result = adapter.http_json(url, headers={
        "Authorization": "Bearer " + token,
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "https",
    }, timeout=8)
    records = result["response"]
    if not isinstance(records, list):
        raise ValueError("Invalid nodes response.")
    locations = {
        node["countryCode"]: node["countryCode"]
        for node in records
        if isinstance(node, dict) and isinstance(node.get("countryCode"), str)
        and re.fullmatch(r"[A-Z]{2}", node["countryCode"]) and node.get("isDisabled") is False
    }
    if not 1 <= len(locations) <= 20:
        raise ValueError("The panel must have 1 to 20 enabled countries for guided setup.")
    if not any(state != "unknown" for state in panel_observation(records, locations).values()):
        raise ValueError("The API returned no usable observations.")
    config = json.loads(Path(__file__).with_name("config.example.json").read_text(encoding="utf-8"))
    config.update(panel_url=url, locations=dict(sorted(locations.items())))
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args()
    try:
        result = configure(args.url)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        # Network exception strings can include credential-bearing URLs.
        label = type(exc).__name__
        if os.environ.get("INSTALL_LANG") == "ru":
            print(f"Проверка API панели не прошла ({label}). Проверьте локальный URL, токен и включённые ноды.",
                  file=sys.stderr)
        else:
            print(f"Panel preflight failed ({label}). Check the local URL, token and enabled nodes.", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))
