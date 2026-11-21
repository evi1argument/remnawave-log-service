from http.client import HTTPConnection
from http.server import HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / 'collector/notify'
sys.path.insert(0, str(SOURCE))
try:
    import beszel
    import monitor
    import watchdog
finally:
    sys.path.remove(str(SOURCE))


class MonitoringIntegrationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.state = self.work / 'state.json'
        self.config = {'token': '123:synthetic-token', 'chat': -123, 'language': 'en'}
        self.telegram = Mock()
        self.telegram.send.return_value = 41

    def incidents(self):
        return monitor.Incidents(self.state, self.config, self.telegram)

    def test_watchdog_uses_receiver_time_then_confirms_recovery(self):
        heartbeat = self.work / 'received'
        watchdog.check_heartbeat(heartbeat, self.incidents(), 100)
        watchdog.check_heartbeat(heartbeat, self.incidents(), 280)
        self.telegram.send.assert_not_called()
        watchdog.check_heartbeat(heartbeat, self.incidents(), 281)
        watchdog.check_heartbeat(heartbeat, self.incidents(), 310)
        self.telegram.send.assert_called_once()
        heartbeat.write_text('320\n')
        watchdog.check_heartbeat(heartbeat, self.incidents(), 321)
        watchdog.check_heartbeat(heartbeat, self.incidents(), 381)
        self.telegram.recover.assert_not_called()
        heartbeat.write_text('382\n')
        watchdog.check_heartbeat(heartbeat, self.incidents(), 383)
        self.telegram.recover.assert_called_once()
        self.assertEqual(self.telegram.recover.call_args.args[0], 41)

    @unittest.skipUnless(os.name == 'posix', 'requires the Linux SSH receiver')
    def test_ssh_receiver_ignores_original_command_and_input(self):
        script = (SOURCE / 'receive-heartbeat.sh').read_text()
        script = script.replace('inbox=/var/lib/log-service-heartbeat/inbox',
                                f'inbox="{self.work}"')
        marker = self.work / 'unexpected'
        original = f'touch "{marker}"'
        subprocess.run(['sh', '-c', script, 'receiver', original],
                       input=original.encode(),
                       env=dict(os.environ, SSH_ORIGINAL_COMMAND=original), check=True)
        self.assertFalse(marker.exists())
        self.assertGreater(int((self.work / 'received').read_text()), 0)

    @unittest.skipUnless(os.name == 'posix', 'requires durable Linux directory sync')
    def test_webhook_auth_size_and_ordered_drain_with_retry(self):
        queue = self.work / 'queue'
        queue.mkdir()
        with HTTPServer(('127.0.0.1', 0), beszel.Handler) as server:
            server.secret_path = b'/synthetic-secret'
            server.queue = queue
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=4)

            def post(path, content):
                connection.request('POST', path, content)
                response = connection.getresponse()
                response.read()
                return response.status

            try:
                down = json.dumps({'title': 'Connection to demo-a is down 🔴',
                                   'message': 'secret URL: https://example.test/?token=secret',
                                   'chat_id': 443})
                up = json.dumps({'title': 'Connection to demo-a is up ✅'})
                self.assertEqual(post('/wrong', down), 404)
                self.assertEqual(post('/synthetic-secret', 'x' * (beszel.MAX_BODY + 1)), 413)
                self.assertEqual(beszel.queue_files(queue), [])
                self.assertEqual(post('/synthetic-secret', down), 204)
                self.assertEqual(post('/synthetic-secret', down), 204)
                self.assertEqual(post('/synthetic-secret', up), 204)
            finally:
                connection.close()
                server.shutdown()
                worker.join(timeout=4)

        files = beszel.queue_files(queue)
        self.assertEqual(len(files), 3)
        self.assertTrue(all('secret' not in file.read_text() for file in files))
        self.assertTrue(all('chat_id' not in file.read_text() for file in files))
        self.telegram.send.side_effect = [monitor.TelegramError(429, retry_after=90), 41]
        now = monitor.time.time()
        beszel.drain(queue, self.incidents(), now)
        self.assertEqual(beszel.queue_files(queue), files)
        beszel.drain(queue, self.incidents(), now + 89)
        self.assertEqual(self.telegram.send.call_count, 1)
        beszel.drain(queue, self.incidents(), now + 90)
        self.assertEqual(self.telegram.send.call_count, 2)
        self.telegram.recover.assert_called_once()
        self.assertEqual(beszel.queue_files(queue), [])
        self.assertEqual(self.telegram.recover.call_args.args[0], 41)

    def test_accepts_only_private_bind_addresses_and_supported_titles(self):
        self.assertEqual(beszel.bind_address('172.20.0.1'), '172.20.0.1')
        for address in ('0.0.0.0', '8.8.8.8', '::'):
            with self.assertRaises(ValueError):
                beszel.bind_address(address)
        self.assertIsNone(beszel.parse_event({'title': 'demo-a CPU above threshold'}, 500))
        self.assertFalse(beszel.parse_event({'title': 'Connection to demo-a is up ✅'}, 500)['bad'])
        with self.assertRaises(ValueError):
            beszel.parse_event({'title': 'Connection to https://example.test is down'}, 500)
        disk = beszel.parse_event({'title': 'demo-a disk usage above threshold',
                                   'message': 'Disk averaged 92.21% for the previous 5 minutes.'}, 500)
        self.assertEqual((disk['name'], disk['average'], disk['minutes']), ('demo-a', '92.21', 5))

    def test_vpn_context_requires_fresh_end_to_end_observation(self):
        cache = self.work / 'probes.json'
        sample = {'kind': 'vpn_e2e', 'checked_at': 500, 'state': 'ok'}
        monitor.save_json(cache, {'version': 1, 'routes': {'demo': sample}})
        self.assertEqual(beszel.probe_state('demo', 600, cache), 'ok')
        self.assertEqual(beszel.probe_state('demo', 681, cache), 'unknown')
        self.assertEqual(beszel.probe_state('demo', 499, cache), 'unknown')
        self.assertEqual(beszel.probe_state('missing', 600, cache), 'unknown')


if __name__ == '__main__':
    unittest.main()
