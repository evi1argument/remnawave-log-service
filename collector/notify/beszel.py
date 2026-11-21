#!/usr/bin/env python3
"""Queue supported Beszel events and deliver them as Telegram incidents."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

from monitor import (CONFIG, STATE, Incidents, Telegram, localized, read_json,
                     save_json, validate_recipient)

CONFIG_PATH = CONFIG.with_name('beszel.json')
QUEUE = STATE.parent / 'beszel-queue'
RELAY_STATE = STATE.with_name('beszel-state.json')
PROBE_CACHE = Path('/var/lib/remnawave-statusboard-v2/vpn-probes.json')
MAX_BODY = 8192
MAX_QUEUE = 1024
PORT = 7077
PRIVATE_NETWORKS = tuple(map(ipaddress.IPv4Network, (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
)))


def bind_address(value):
    address = ipaddress.IPv4Address(value)
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError('Use a loopback or private IPv4 address')
    return str(address)


def parse_event(payload, now):
    if not isinstance(payload, dict) or not isinstance(payload.get('title'), str):
        raise ValueError('Expected a title')
    title = payload['title']
    match = re.fullmatch(r'Connection to (.{1,80}) is (up|down)(?: [✅🔴])?', title)
    if match:
        kind, bad = 'connection', match[2] == 'down'
    else:
        match = re.fullmatch(r'(.{1,80}) disk usage (above|below) threshold', title)
        if not match:
            return None
        kind, bad = 'disk', match[2] == 'above'
    name = match[1].strip()
    if not name or not all(c.isalnum() or c in ' ._-()' for c in name):
        raise ValueError('Unsupported system name')
    event = {'name': name, 'kind': kind, 'bad': bad, 'at': now}
    if kind == 'disk' and bad and isinstance(payload.get('message'), str):
        match = re.search(r'averaged ([0-9]{1,3}(?:\.[0-9]{1,2})?)%? for the previous ([0-9]{1,4}) minutes?\b',
                          payload['message'])
        if match and float(match[1]) <= 100:
            event.update(average=match[1], minutes=int(match[2]))
    return event


def queue_files(path):
    return sorted(path.glob('event-*.json'))


def enqueue(path, event):
    files = queue_files(path)
    if len(files) >= MAX_QUEUE:
        raise OverflowError('Beszel queue is full')
    sequence = int(files[-1].stem.split('-')[1]) + 1 if files else 1
    save_json(path / f'event-{sequence:020d}.json', event)
    # Confirm the directory entry is durable before acknowledging the webhook.
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(4)
        super().setup()

    def log_message(self, *args):
        # The request path contains the webhook credential.
        pass

    def reply(self, code):
        self.send_response(code)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        self.reply(204 if self.path == '/health' else 404)

    def do_POST(self):
        if not hmac.compare_digest(self.path.encode(), self.server.secret_path):
            self.reply(404)
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
            if not 0 < length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                self.reply(413)
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete body')
            event = parse_event(json.loads(raw), time.time())
            if event:
                enqueue(self.server.queue, event)
        except (ValueError, RecursionError):
            self.reply(400)
            return
        except (OSError, OverflowError):
            self.reply(503)
            return
        self.reply(204)


def probe_state(route, now, cache_path=PROBE_CACHE):
    try:
        with cache_path.open('rb') as stream:
            raw = stream.read(65537)
        doc = json.loads(raw) if len(raw) <= 65536 else {}
        if doc['version'] != 1:
            return 'unknown'
        sample = doc['routes'][route]
        stamp = sample['checked_at']
        if sample['kind'] == 'vpn_e2e' and type(stamp) in (int, float) and 0 <= now - stamp <= 180:
            if sample['state'] in ('ok', 'failed'):
                return sample['state']
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        pass
    return 'unknown'


def drain(path, incidents, now, routes=None):
    lang = incidents.language
    for file in queue_files(path)[:32]:
        item = read_json(file, None)
        if item is None:
            continue
        name = item['name']
        if item['kind'] == 'connection':
            title = localized(lang, f'Beszel · {name} · Agent disconnected',
                              f'Beszel · {name} · Агент потерял связь')
            if item['bad'] and routes and name in routes:
                state = probe_state(routes[name], item['at'])
                message = {
                    'ok': ('VPN request succeeded', 'Проверочный запрос через VPN прошёл'),
                    'failed': ('VPN request failed', 'Проверочный запрос через VPN не прошёл'),
                    'unknown': ('No fresh VPN probe result', 'Нет свежего результата проверки VPN'),
                }
                title += '\n' + localized(lang, *message[state])
        else:
            title = localized(lang, f'Beszel · {name} · Disk usage above threshold',
                              f'Beszel · {name} · Диск заполнен выше порога')
            if 'average' in item:
                title += localized(lang, f"\nAverage {item['average']}% over {item['minutes']} min",
                                   f"\nСреднее {item['average']}% за {item['minutes']} мин")
        key = item['kind'] + ':' + name
        if not incidents.check(key, item['bad'], title, item['at'], pending=0, delivery_now=now):
            break
        save_json(incidents.path, incidents.state)
        file.unlink()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'serve', 'drain', 'url'])
    parser.add_argument('--lang', choices=['en', 'ru'], default='en')
    args = parser.parse_args()
    try:
        if args.action == 'drain':
            config = validate_recipient(read_json(CONFIG, {}))
            routes = read_json(CONFIG_PATH, {}).get('probe_routes', {})
            drain(QUEUE, Incidents(RELAY_STATE, config, Telegram(config)), time.time(), routes)
            return
        config = read_json(CONFIG_PATH, {})
        if args.action == 'configure':
            if not sys.stdin.isatty():
                raise ValueError('Run from a terminal')
            old = config.get('bind', '127.0.0.1')
            prompt = localized(args.lang, 'Private bind IPv4 visible to Beszel',
                               'Закрытый IPv4-адрес, доступный Beszel')
            address = bind_address(input(f'{prompt} [{old}]: ').strip() or old)
            config.update(bind=address, secret=config.get('secret') or secrets.token_urlsafe(32))
            save_json(CONFIG_PATH, config)
            return
        address = bind_address(config['bind'])
        secret = config['secret']
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', secret):
            raise ValueError('Invalid webhook credential')
        if args.action == 'url':
            if not sys.stdout.isatty():
                raise ValueError('Display webhook URL from a terminal')
            print(f'generic://{address}:{PORT}/{secret}?template=json&disabletls=yes')
            return
        with HTTPServer((address, PORT), Handler) as server:
            server.secret_path = f'/{secret}'.encode()
            server.queue = QUEUE
            server.serve_forever()
    except (OSError, ValueError, KeyError, TypeError, EOFError, KeyboardInterrupt):
        parser.exit(1, 'Beszel relay configuration or operation failed.\n')


if __name__ == '__main__':
    main()
