#!/usr/bin/env python3
"""Send collector health transitions to a configured Telegram chat."""

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

CONFIG = Path('/opt/logcollector/notify/config.json')
STATE = Path('/var/lib/log-service-notify/state.json')
ARCHIVE_SUCCESS = STATE.with_name('archive-success.json')
CHECKS = {
    'services': ('Collector services', 'Сервисы коллектора'),
    'delivery': ('Log delivery by node', 'Доставка логов по нодам'),
    'disk': ('Collector disk space', 'Место на диске коллектора'),
    'archive': ('Archive job and timer', 'Архиватор и его таймер'),
    'errors': ('Xray error.log volume', 'Число строк в error.log Xray'),
}
CANARIES = 'sum by (node) (count_over_time({service="log-service",stream="canary"}[5m]))'
ERRORS = (
    'sum by (node) (count_over_time({service="xray",stream="error"}[5m])) or on (node) '
    '(0 * sum by (node) (count_over_time({service="xray",stream="access"}[10m])))'
)
NODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


def localized(language, english, russian):
    return russian if language == 'ru' else english


def read_json(path, missing):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return missing


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def validate(config):
    if not isinstance(config, dict):
        raise ValueError('Expected a configuration object')
    token = config.get('token')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ValueError('Invalid bot token format')
    if type(config.get('chat')) is not int or config['chat'] == 0:
        raise ValueError('Enter a numeric Telegram chat ID')
    topic = config.get('topic')
    if topic is not None and (type(topic) is not int or topic <= 0):
        raise ValueError('Enter a positive Telegram topic ID')
    checks = config.get('checks', [])
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, str) or check not in CHECKS for check in checks
    ):
        raise ValueError('Select at least one supported check')
    nodes = config.get('nodes', [])
    if not isinstance(nodes, list) or any(not isinstance(node, str) or not NODE.fullmatch(node)
                                          for node in nodes):
        raise ValueError('Invalid node label')
    if 'delivery' in checks and not nodes:
        raise ValueError('Delivery checks require expected node labels')
    if config.get('language') not in ('en', 'ru'):
        raise ValueError('Language must be en or ru')
    hours = config.get('archive_hours', 192)
    if type(hours) is not int or not 1 <= hours <= 8760:
        raise ValueError('Archive delay must be 1 to 8760 hours')
    return config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = build_opener(ProxyHandler({}), NoRedirect())


def http(url, data=None, timeout=4):
    body = json.dumps(data).encode() if data is not None else None
    headers = {'Content-Type': 'application/json'} if body is not None else {}
    with HTTP.open(Request(url, data=body, headers=headers), timeout=timeout) as response:
        return response.read(1024 * 1024)


class TelegramError(Exception):
    def __init__(self, code, description='', retry_after=0):
        super().__init__(f'Telegram request failed ({code})')
        self.code = code
        self.retry_after = retry_after
        self.description = description.lower()

    @property
    def unavailable_message(self):
        return self.code == 400 and any(reason in self.description for reason in (
            'message to edit not found', "message can't be edited", 'message cannot be edited',
        ))


class Telegram:
    def __init__(self, config):
        self.config = config

    def request(self, method, params):
        try:
            payload = json.loads(http(f"https://api.telegram.org/bot{self.config['token']}/{method}",
                                      params, timeout=6))
        except HTTPError as error:
            try:
                payload = json.loads(error.read(8192))
            except (OSError, ValueError):
                payload = {'error_code': error.code}
        except (OSError, ValueError):
            raise TelegramError(0) from None
        if not isinstance(payload, dict):
            raise TelegramError(0)
        if payload.get('ok') is True and 'result' in payload:
            return payload['result']
        parameters = payload.get('parameters')
        parameters = parameters if isinstance(parameters, dict) else {}
        retry_after = parameters.get('retry_after', 0)
        raise TelegramError(payload.get('error_code', 0), str(payload.get('description', '')),
                            retry_after if type(retry_after) is int else 0)

    def send(self, text, silent=False):
        params = {'chat_id': self.config['chat'], 'text': text, 'parse_mode': 'HTML',
                  'disable_notification': silent}
        if self.config.get('topic'):
            params['message_thread_id'] = self.config['topic']
        return self.request('sendMessage', params)['message_id']

    def recover(self, message_id, text):
        try:
            self.request('editMessageText', {'chat_id': self.config['chat'],
                         'message_id': message_id, 'text': text, 'parse_mode': 'HTML'})
        except TelegramError as error:
            if error.code == 400 and 'message is not modified' in error.description:
                return
            if not error.unavailable_message:
                raise
            self.send(text, silent=True)


def card(language, title, opened, recovered=None):
    fmt = '%Y-%m-%d %H:%M UTC'
    date = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).strftime(fmt)
    status = localized(language, 'Alert', 'Тревога')
    if recovered is not None:
        status = localized(language, 'Recovered', 'Восстановлено')
    lines = [f'<b>{status} · {html.escape(title)}</b>',
             f'{localized(language, "Started", "Начало")} {date(opened)}']
    if recovered is not None:
        duration = max(0, int(recovered - opened) // 60)
        minutes = localized(language, 'min', 'мин')
        lines.append(f'{localized(language, "Ended", "Завершение")} {date(recovered)} · {duration} {minutes}')
    return '\n'.join(lines)


class Incidents:
    def __init__(self, path, config, telegram):
        self.path = path
        self.telegram = telegram
        self.remaining = 8
        self.language = config['language']
        recipient = json.dumps([config['token'], config['chat'], config.get('topic')])
        fingerprint = hashlib.sha256(recipient.encode()).hexdigest()
        state = read_json(path, {})
        self.state = state if state.get('recipient') == fingerprint else {
            'recipient': fingerprint, 'incidents': {},
        }

    def check(self, key, bad, title, now, pending=120):
        incidents = self.state['incidents']
        if bad:
            item = incidents.setdefault(key, {'opened': now, 'title': title})
            if not item.get('message_id') and now - item.get('observed', now) > 150:
                item['opened'] = now
            item['observed'] = now
            item.pop('recovered', None)
        elif key in incidents and incidents[key].get('message_id'):
            item = incidents[key]
            item.setdefault('recovered', now)
        else:
            incidents.pop(key, None)
            return
        if now < self.state.get('retry_at', 0) or self.remaining == 0:
            return
        try:
            if bad and not item.get('message_id') and now - item['opened'] >= pending:
                self.remaining -= 1
                save_json(self.path, self.state)
                item['message_id'] = self.telegram.send(card(self.language, item['title'], item['opened']))
                save_json(self.path, self.state)
            elif not bad:
                self.remaining -= 1
                save_json(self.path, self.state)
                self.telegram.recover(item['message_id'], card(self.language, item['title'],
                                      item['opened'], item['recovered']))
                del incidents[key]
                save_json(self.path, self.state)
        except TelegramError as error:
            delay = max(60, error.retry_after) if error.code != 401 else 300
            self.state['retry_at'] = now + delay
            save_json(self.path, self.state)
            print(error, flush=True)

    def unknown(self, key):
        item = self.state['incidents'].get(key)
        if item and not item.get('message_id'):
            del self.state['incidents'][key]


def command(*args):
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=4)


def local_check(probe):
    try:
        return bool(probe())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def loki(query, now):
    params = urlencode({'query': query, 'time': str(int(now * 1e9))})
    payload = json.loads(http('http://127.0.0.1:3100/loki/api/v1/query?' + params))
    data = payload['data']
    if payload['status'] != 'success' or data['resultType'] != 'vector':
        raise ValueError('Expected a Loki vector result')
    values = {}
    for item in data['result']:
        node = item['metric']['node']
        value = float(item['value'][1])
        if not NODE.fullmatch(node) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid Loki result')
        values[node] = value
    return values


def monitor(config, incidents, now):
    lang = config['language']
    enabled = config['checks']
    checked = set()

    def check(key, bad, english, russian, pending=120):
        checked.add(key)
        incidents.check(key, bad, localized(lang, english, russian), now, pending)

    if 'services' in enabled:
        for name, probe in (
            ('Loki', lambda: http('http://127.0.0.1:3100/ready').strip() == b'ready'),
            ('Grafana', lambda: json.loads(http('http://127.0.0.1:3000/api/health')).get('database') == 'ok'),
            ('Vector', lambda: command('docker', 'inspect', '--format', '{{.State.Running}}',
                                       'logcollector-vector').stdout.strip() == 'true'),
        ):
            check('service-' + name, not local_check(probe),
                  f'{name} unavailable', f'{name} недоступен')
    if 'disk' in enabled:
        try:
            disk = shutil.disk_usage('/opt/logcollector/spool')
        except OSError:
            checked.add('disk')
            incidents.unknown('disk')
            check('disk-query', True, 'Cannot check the spool disk', 'Не удалось проверить диск очереди архива')
        else:
            check('disk-query', False, '', '')
            check('disk', disk.used * 10 >= disk.total * 9,
                  'Collector spool disk at least 90% full', 'Диск очереди архива заполнен на 90% или больше')
    if 'archive' in enabled:
        timer = local_check(lambda: command('systemctl', 'is-active', '--quiet',
                                           'archive-upload.timer').returncode == 0)
        check('archive-timer', not timer, 'Archive timer inactive', 'Таймер архива не запущен')
        result = local_check(lambda: command('systemctl', 'show', '--value', '--property=Result',
                                            'archive-upload.service').stdout.strip() == 'success')
        check('archive-job', not result, 'Archive job failed or could not be checked',
              'Сбой архивации или ошибка проверки')
        baseline = incidents.state.setdefault('archive_since', now)
        last_success = read_json(ARCHIVE_SUCCESS, {}).get('at', baseline)
        hours = config.get('archive_hours', 192)
        check('archive-stale', now - last_success > hours * 3600,
              f'No successful archive run within {hours} hours',
              f'За {hours} ч. нет успешного запуска архивации')
    else:
        incidents.state.pop('archive_since', None)

    query_failed = False
    for kind, query in (('delivery', CANARIES), ('errors', ERRORS)):
        if kind not in enabled:
            continue
        try:
            values = loki(query, now)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            for key in tuple(incidents.state['incidents']):
                if key.startswith(kind + ':'):
                    checked.add(key)
                    incidents.unknown(key)
            query_failed = True
            continue
        if kind == 'delivery':
            for node in config['nodes']:
                check('delivery:' + node, values.get(node, 0) == 0,
                      f'{node} · No delivery marker in Loki for 5 minutes',
                      f'{node} · В Loki нет контрольной записи за 5 минут')
        else:
            for key in tuple(incidents.state['incidents']):
                if key.startswith('errors:') and key.split(':', 1)[1] not in values:
                    checked.add(key)
                    incidents.unknown(key)
            for node, value in values.items():
                check('errors:' + node, value > 20,
                      f'{node} · More than 20 error.log lines in 5 minutes',
                      f'{node} · Более 20 строк error.log за 5 минут', pending=300)
    if 'delivery' in enabled or 'errors' in enabled:
        check('query-loki', query_failed, 'Loki log query failed', 'Ошибка запроса к логам Loki')
    for key in incidents.state['incidents'].keys() - checked:
        del incidents.state['incidents'][key]
    save_json(incidents.path, incidents.state)


def configure(path, language):
    if not os.isatty(0):
        raise ValueError('Run interactive setup from a terminal')
    previous = read_json(path, {})
    tr = lambda en, ru: localized(language, en, ru)
    token_prompt = tr('Bot token', 'Токен бота')
    if previous.get('token'):
        token_prompt += tr(' (Enter to keep)', ' (Enter — сохранить текущий)')
    token = getpass.getpass(token_prompt + ': ').strip() or previous.get('token', '')

    def ask(prompt, default=''):
        suffix = f' [{default}]' if default else ''
        return input(prompt + suffix + ': ').strip() or str(default)

    chat = int(ask(tr('Numeric chat ID', 'Числовой ID чата'), previous.get('chat', '')))
    topic = int(ask(tr('Topic ID (0 for no topic)', 'ID темы (0 — без темы)'), previous.get('topic') or 0))
    choices = dict(enumerate(CHECKS, 1))
    for index, key in choices.items():
        print(f'{index}. {tr(*CHECKS[key])}')
    defaults = ','.join(str(index) for index, key in choices.items()
                        if key in previous.get('checks', ['services', 'disk', 'archive']))
    selected = ask(tr('Checks, separated by commas', 'Номера проверок через запятую'), defaults)
    checks = list(dict.fromkeys(choices[int(number)] for number in selected.split(',')))
    config = {'token': token, 'chat': chat, 'topic': topic or None, 'checks': checks,
              'language': language, 'nodes': []}
    if 'archive' in checks:
        config['archive_hours'] = int(ask(tr('Maximum hours since a successful archive run',
                                            'Допустимое время без успешной архивации, ч.'),
                                         previous.get('archive_hours', 192)))
    if 'delivery' in checks:
        nodes = ask(tr('Node labels with active delivery timers (comma separated)',
                       'Метки нод с включённым таймером доставки (через запятую)'),
                    ','.join(previous.get('nodes', [])))
        config['nodes'] = sorted({node.strip() for node in nodes.split(',') if node.strip()})
    validate(config)
    save_json(path, config)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'run', 'test', 'status', 'archive-success'])
    parser.add_argument('--lang', choices=['en', 'ru'], default='en')
    args = parser.parse_args()
    try:
        if args.action == 'configure':
            configure(CONFIG, args.lang)
            return
        if args.action == 'archive-success':
            save_json(ARCHIVE_SUCCESS, {'at': time.time()})
            return
        config = validate(read_json(CONFIG, {}))
        if args.action == 'status':
            for key in config['checks']:
                print(localized(args.lang, *CHECKS[key]))
            return
        telegram = Telegram(config)
        if args.action == 'test':
            telegram.send(localized(config['language'], 'Remnawave Log Service · Test notification',
                                    'Remnawave Log Service · Тестовое уведомление'))
            return
        monitor(config, Incidents(STATE, config, telegram), time.time())
    except (EOFError, KeyboardInterrupt, KeyError, OSError, TypeError, ValueError, TelegramError):
        parser.exit(1, 'Telegram monitor: configuration, local check, or delivery failed.\n')


if __name__ == '__main__':
    main()
