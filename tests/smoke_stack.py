"""Linux Docker smoke test. Only synthetic logs; no published ports or real volumes."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[1]
ENV = dict(os.environ, GF_ADMIN_PASSWORD='smoke-only-password')


def docker(*args):
    return subprocess.check_output(['docker', *args], env=ENV, text=True).strip()


def wait_for(callback, description, timeout=90):
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except Exception as exc:
            error = exc
        time.sleep(1)
    raise RuntimeError(f'Timeout waiting for {description}: {error}')


def get(url, authenticated=False):
    headers = {'Cache-Control': 'no-cache'}
    if authenticated:
        headers['Authorization'] = 'Basic ' + base64.b64encode(
            b'admin:smoke-only-password').decode()
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3) as response:
        return response.read()


def main():
    config = json.loads(docker('compose', '-f', str(REPO / 'collector/docker-compose.yml'),
                               'config', '--format', 'json'))['services']
    prefix = 'log-smoke-' + uuid.uuid4().hex[:10]
    network = prefix + '-net'
    containers = []
    docker('network', 'create', '--internal', network)
    try:
        # /tmp is often a small tmpfs; the configured buffers require >1 GiB capacity.
        with tempfile.TemporaryDirectory(prefix='log-service-smoke-', dir='/var/tmp') as tmp:
            work = Path(tmp)
            work.chmod(0o755)
            for name in ['tls', 'loki', 'grafana', 'aggregator', 'agent', 'spool', 'self', 'logs']:
                (work / name).mkdir(mode=0o777)
                (work / name).chmod(0o777)
            subprocess.run(['bash', str(REPO / 'scripts/gen-tls.sh'), str(work / 'tls')],
                           check=True, capture_output=True)
            (work / 'tls/key.pem').chmod(0o644)  # Disposable certificate only.
            marker = prefix
            aggregator = (REPO / 'collector/vector/vector.aggregator.yaml').read_text()
            (work / 'aggregator.yaml').write_text(aggregator.replace('__COLLECTOR_NODE__', 'smoke-self'))
            agent = (REPO / 'node/vector/vector.agent.yaml').read_text()
            (work / 'agent.yaml').write_text(agent.replace('__NODE_NAME__', 'smoke-agent')
                                           .replace('__COLLECTOR_IP__', 'collector'))

            def start(role, image, options=(), command=()):
                name = prefix + '-' + role
                containers.append(name)
                docker('run', '-d', '--name', name, '--network', network,
                       '--network-alias', role, *options, image, *command)
                info = json.loads(docker('inspect', name))[0]
                return info['NetworkSettings']['Networks'][network]['IPAddress']

            def mount(source, target, readonly=False):
                return ['-v', f'{source}:{target}' + (':ro' if readonly else '')]

            loki_ip = start('loki', config['loki']['image'],
                            mount(REPO / 'collector/loki/loki-config.yaml', '/etc/loki/config.yaml', True)
                            + mount(work / 'loki', '/loki'), ['-config.file=/etc/loki/config.yaml'])
            wait_for(lambda: get(f'http://{loki_ip}:3100/ready') == b'ready\n', 'Loki readiness')
            start('collector', config['vector']['image'], ['--user', '0']
                  + mount(work / 'aggregator.yaml', '/etc/vector/vector.yaml', True)
                  + mount(work / 'tls', '/tls', True) + mount(work / 'aggregator', '/vector-data')
                  + mount(work / 'spool', '/spool') + mount(work / 'self', '/self-logs', True))
            start('agent', config['vector']['image'], ['--user', '0']
                  + mount(work / 'agent.yaml', '/etc/vector/vector.yaml', True)
                  + mount(work / 'tls/cert.pem', '/tls/collector.crt', True)
                  + mount(work / 'agent', '/vector-data')
                  + mount(work / 'logs', '/var/log/remnanode', True))
            start_ns = time.time_ns() - 300 * 10**9
            (work / 'self/access.log').write_text(f'{marker} self\n')
            (work / 'logs/access.log').write_text(f'{marker} agent\n')

            def loki_streams(query):
                params = urllib.parse.urlencode({
                    'query': query, 'limit': 10, 'start': start_ns, 'end': time.time_ns(),
                })
                response = get(f'http://{loki_ip}:3100/loki/api/v1/query_range?{params}')
                result = json.loads(response)
                assert result['status'] == 'success', result
                return result['data']['result']

            def delivered():
                nodes = {s['stream']['node'] for s in loki_streams(f'{{service="xray"}} |= "{marker}"')}
                return {'smoke-self', 'smoke-agent'} <= nodes

            try:
                wait_for(delivered, 'both self and TLS agent logs in Loki')
            except RuntimeError:
                print('Synthetic Xray streams:', loki_streams('{service="xray"}'), flush=True)
                raise
            now = int(time.time())
            expected = {}
            for number, (directory, node) in enumerate([('self', 'smoke-self'), ('logs', 'smoke-agent')], 1):
                marker_epoch = now - number
                canary = f'log-service-canary {marker_epoch}'
                expected[node] = {(str(marker_epoch * 10**9), canary)}
                lines = canary + '\n'
                if directory == 'logs':
                    lines += 'invalid marker\n'
                (work / directory / 'log-service-canary.log').write_text(lines)

            canary_selector = '{service="log-service",stream="canary"}'
            def canaries_delivered():
                entries = {
                    stream['stream']['node']: set(map(tuple, stream['values']))
                    for stream in loki_streams(canary_selector)
                }
                return entries == expected

            try:
                wait_for(canaries_delivered, 'local and TLS canaries in Loki with original timestamps')
            except RuntimeError:
                print('Synthetic control records:', loki_streams(canary_selector), flush=True)
                print('Expected control records:', expected, flush=True)
                raise
            # Archives consume closed segments. Stop the writers in upstream
            # order so Vector drains its disk buffers and closes the files.
            for role in ('agent', 'collector'):
                name = prefix + '-' + role
                docker('stop', '--time', '70', name)
                state = json.loads(docker('inspect', name))[0]['State']
                assert state['ExitCode'] == 0 and not state['OOMKilled'], (name, state)

            spool = work / 'spool'
            records = {}
            for path in sorted(spool.rglob('*.log')):
                node = path.relative_to(spool).parts[0]
                records.setdefault(node, []).extend(path.read_text().splitlines())
            expected_spool = {'smoke-self': {f'{marker} self'}, 'smoke-agent': {f'{marker} agent'}}
            assert {node: set(lines) for node, lines in records.items()} == expected_spool, (
                'Unexpected closed raw spool segments', records)
            environment = [arg for key, value in config['grafana']['environment'].items()
                           for arg in ['-e', f'{key}={value}']]
            grafana_ip = start('grafana', config['grafana']['image'], environment
                               + mount(REPO / 'collector/grafana/provisioning', '/etc/grafana/provisioning', True)
                               + mount(work / 'grafana', '/var/lib/grafana'))
            base = f'http://{grafana_ip}:3000'
            wait_for(lambda: json.loads(get(base + '/api/health'))['database'] == 'ok', 'Grafana health')
            wait_for(lambda: len([d for d in json.loads(get(base + '/api/search', True))
                                 if d.get('type') == 'dash-db']) == 2, 'two provisioned dashboards')
            datasource = json.loads(get(base + '/api/datasources/uid/loki', True))
            assert datasource['url'] == 'http://loki:3100', datasource
            print('SMOKE OK: Xray logs reach Loki and spool; canaries reach only Loki; Grafana healthy')
    except Exception:
        for name in containers:
            subprocess.run(['docker', 'logs', '--tail', '35', name], check=False)
        raise
    finally:
        for name in reversed(containers):
            subprocess.run(['docker', 'rm', '-f', '-v', name], check=False, stdout=subprocess.DEVNULL)
        subprocess.run(['docker', 'network', 'rm', network], check=False, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
