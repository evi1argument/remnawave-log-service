#!/usr/bin/env python3
"""Check a collector heartbeat received over restricted SSH."""

import argparse
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from monitor import (Incidents, Telegram, http, local_check, localized, read_json,
                     recipient_from_terminal, save_json, validate_recipient)

CONFIG = Path('/etc/log-service-watchdog.json')
STATE = Path('/var/lib/log-service-watchdog/state.json')
HEARTBEAT = Path('/var/lib/log-service-heartbeat/inbox/received')


def beszel_origin(value):
    if not value:
        return ''
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or
            url.query or url.fragment or url.path not in ('', '/')):
        raise ValueError('Expected an HTTPS origin without credentials or a path')
    return f'https://{url.netloc}'


def check_heartbeat(path, incidents, now, hub=''):
    started = incidents.state.setdefault('watchdog_since', now)
    try:
        received = int(path.read_text(encoding='ascii'))
        fresh = 0 <= now - received <= 180
        late = not fresh
    except (FileNotFoundError, UnicodeError, ValueError):
        received = None
        late = now - started > 180
    title = localized(incidents.language, 'Collector heartbeat absent for more than 3 minutes',
                      'Сигнал коллектора отсутствует более 3 минут')
    # A cached heartbeat can be read many times between SSH connections.
    # Only a new reception can confirm recovery.
    key = 'collector-heartbeat'
    pending_recovery = bool(incidents.state['incidents'].get(key))
    if late or not pending_recovery or received != incidents.state.get('watchdog_last_received'):
        settled = incidents.check(key, late, title, now, pending=0, recovery_pending=60)
        if not late and (settled or not pending_recovery or
                         not incidents.state['incidents'][key].get('recovered') or
                         now - incidents.state['incidents'][key]['recovered'] < 60):
            incidents.state['watchdog_last_received'] = received
    if hub and not late and received is not None:
        available = local_check(lambda: http(hub + '/api/health') is not None)
        title = localized(incidents.language, 'External Beszel health check failed',
                          'Внешняя проверка API Beszel не прошла')
        incidents.check('beszel-api', not available, title, now, recovery_pending=60)
    else:
        incidents.unknown('beszel-api')
    save_json(incidents.path, incidents.state)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'run'])
    parser.add_argument('--lang', choices=['en', 'ru'], default='en')
    args = parser.parse_args()
    try:
        if args.action == 'configure':
            config, old, ask = recipient_from_terminal(CONFIG, args.lang)
            prompt = localized(args.lang, 'Beszel HTTPS URL or - to skip', 'HTTPS-адрес Beszel или - для пропуска')
            hub = ask(prompt, old.get('beszel') or '-')
            config['beszel'] = beszel_origin('' if hub == '-' else hub)
            save_json(CONFIG, config)
        else:
            config = validate_recipient(read_json(CONFIG, {}))
            check_heartbeat(HEARTBEAT, Incidents(STATE, config, Telegram(config)), time.time(),
                            beszel_origin(config.get('beszel')))
    except (OSError, ValueError, KeyError, TypeError, EOFError, KeyboardInterrupt):
        parser.exit(1, 'Watchdog could not read its configuration or heartbeat state.\n')


if __name__ == '__main__':
    main()
