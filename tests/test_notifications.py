import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'log_notify', Path(__file__).resolve().parents[1] / 'collector/notify/monitor.py')
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class NotificationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / 'state.json'
        self.telegram = Mock()
        self.telegram.send.return_value = 17
        self.config = {
            'token': '123:synthetic-token',
            'chat': -123,
            'topic': 45,
            'language': 'en',
            'checks': ['delivery'],
            'nodes': ['demo-a', 'demo-b'],
        }

    def tick(self, key, bad, now, config=None):
        incidents = notify.Incidents(self.state, config or self.config, self.telegram)
        incidents.check(key, bad, 'demo <event>', now)
        notify.save_json(self.state, incidents.state)

    def test_debounce_then_edit_the_same_message_after_restart(self):
        for now in (0, 60, 120, 180):
            self.tick('delivery:demo-a', True, now)
        self.telegram.send.assert_called_once()
        self.assertIn('&lt;event&gt;', self.telegram.send.call_args.args[0])
        self.tick('delivery:demo-a', False, 240)
        self.tick('delivery:demo-a', False, 300)
        self.telegram.recover.assert_called_once()
        self.assertEqual(self.telegram.recover.call_args.args[0], 17)
        self.assertIn('Recovered', self.telegram.recover.call_args.args[1])

    def test_recipient_change_cannot_edit_an_old_chat(self):
        for now in (0, 60, 120):
            self.tick('disk', True, now)
        self.tick('disk', False, 180, dict(self.config, topic=99))
        self.telegram.recover.assert_not_called()

    def test_rate_limits_survive_restart(self):
        self.telegram.send.side_effect = [notify.TelegramError(429, retry_after=180), 17]
        for now in (0, 60, 120, 180, 240):
            self.tick('disk', True, now)
        self.assertEqual(self.telegram.send.call_count, 1)
        self.tick('disk', True, 300)
        self.assertEqual(self.telegram.send.call_count, 2)

    def test_loki_error_does_not_start_per_node_incidents(self):
        incidents = notify.Incidents(self.state, self.config, self.telegram)
        with patch.object(notify, 'loki', side_effect=OSError):
            for now in (0, 60, 120):
                notify.monitor(self.config, incidents, now)
        self.assertEqual(set(incidents.state['incidents']), {'query-loki'})
        with patch.object(notify, 'loki', return_value={'demo-a': 1}):
            for now in (180, 240, 300):
                notify.monitor(self.config, incidents, now)
        self.assertEqual(set(incidents.state['incidents']), {'delivery:demo-b'})
        self.assertIn('demo-b', self.telegram.send.call_args.args[0])

    def test_archive_freshness_uses_success_hook_independent_of_exit_result(self):
        config = dict(self.config, checks=['archive'], archive_hours=1)
        incidents = notify.Incidents(self.state, config, self.telegram)
        stamp = self.state.with_name('archive-success.json')
        successful = Mock(returncode=0, stdout='success\n')
        with patch.object(notify, 'ARCHIVE_SUCCESS', stamp), \
                patch.object(notify, 'command', return_value=successful):
            notify.monitor(config, incidents, 0)
            for now in (3601, 3661, 3721):
                notify.monitor(config, incidents, now)
            self.telegram.send.assert_called_once()
            self.assertEqual(set(incidents.state['incidents']), {'archive-stale'})
            notify.save_json(stamp, {'at': 3750})
            notify.monitor(config, incidents, 3781)
        self.telegram.recover.assert_called_once()

    def test_missing_error_series_is_unknown_and_zero_can_recover(self):
        config = dict(self.config, checks=['errors'])
        incidents = notify.Incidents(self.state, config, self.telegram)
        with patch.object(notify, 'loki', return_value={'demo-a': 22}):
            for now in range(0, 301, 60):
                notify.monitor(config, incidents, now)
        self.telegram.send.assert_called_once()
        with patch.object(notify, 'loki', return_value={}):
            notify.monitor(config, incidents, 360)
        self.telegram.recover.assert_not_called()
        with patch.object(notify, 'loki', return_value={'demo-a': 0}):
            notify.monitor(config, incidents, 420)
        self.telegram.recover.assert_called_once()

    def test_recovery_fallback_only_for_an_uneditable_message(self):
        sender = notify.Telegram(self.config)
        with patch.object(sender, 'request', side_effect=[
            notify.TelegramError(400, 'message to edit not found'), {'message_id': 21},
        ]) as request:
            sender.recover(17, 'Recovered')
        method, payload = request.call_args.args
        self.assertEqual(method, 'sendMessage')
        self.assertEqual(payload['message_thread_id'], 45)
        self.assertTrue(payload['disable_notification'])
        for error in (notify.TelegramError(429), notify.TelegramError(403),
                      notify.TelegramError(400, 'message is not modified')):
            with patch.object(sender, 'request', side_effect=error) as request:
                if error.code == 400:
                    sender.recover(17, 'Recovered')
                else:
                    with self.assertRaises(notify.TelegramError):
                        sender.recover(17, 'Recovered')
                request.assert_called_once()

    def test_setup_stores_opt_in_checks_and_rejects_invalid_input(self):
        path = self.state.with_name('config.json')
        with patch.object(notify.os, 'isatty', return_value=True), \
                patch.object(notify.getpass, 'getpass', return_value='123:synthetic-token'), \
                patch('builtins.input', side_effect=['-123', '45', '2,5', 'demo-b,demo-b,demo-a']):
            notify.configure(path, 'ru')
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['checks'], ['delivery', 'errors'])
        self.assertEqual(saved['nodes'], ['demo-a', 'demo-b'])
        with self.assertRaises(ValueError):
            notify.validate(dict(saved, nodes=[]))


if __name__ == '__main__':
    unittest.main()
