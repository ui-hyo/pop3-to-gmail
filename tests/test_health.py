import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from email2gmail.cli import health, ignore_message, main, status
from email2gmail.core import BridgeError, Config, State, digest, lock


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config('gmail@example.com', 'company@example.com', 'pop.example.com', '123456789', state_dir=self.tmp.name)
        self.state = State(self.cfg)
        self.addCleanup(self.state.close)
        self.now = time.time()
        self.state.set('last_scan_ok', self.now - 60)
        self.run = self.enterContext(patch('email2gmail.cli.subprocess.run'))
        self.run.side_effect = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1 if 'is-failed' in argv else 0)
        self.enterContext(patch('email2gmail.cli.time.time', return_value=self.now))

    def message(self, **values):
        self.state.snapshot([(1, 'private-uid', 100)], self.cfg, self.now)
        self.state.update('private-uid', **values)

    def test_fresh_empty_queue_is_healthy_and_does_not_write(self):
        before = (Path(self.tmp.name) / 'state.sqlite3').read_bytes()
        self.assertEqual(health(self.cfg), {'healthy': True, 'issues': [], 'scan_age_seconds': 60,
                                          'liveness_issues': [], 'queue_issues': []})
        self.assertEqual((Path(self.tmp.name) / 'state.sqlite3').read_bytes(), before)
        calls = self.run.call_args_list
        self.assertEqual([c.args[0][-1] for c in calls],
                         ['email2gmail.timer', 'email2gmail-watchdog.timer', 'email2gmail-watchdog.service'])
        self.assertEqual(calls[0].kwargs['env']['DBUS_SESSION_BUS_ADDRESS'], f'unix:path=/run/user/{os.getuid()}/bus')

    def test_scan_freshness_and_clock(self):
        for stamp, expected in [(None, 'scan_never_succeeded'),
                                (self.now - 901, 'scan_stale'),
                                (self.now + 60, 'scan_clock_invalid')]:
            with self.subTest(stamp=stamp):
                self.state.set('last_scan_ok', stamp)
                self.assertIn(expected, health(self.cfg)['issues'])

    def test_blocked_mail_is_unhealthy_without_exposing_error_or_identity(self):
        self.message(status='blocked', error='secret subject / token / API body')
        result = health(self.cfg)
        self.assertFalse(result['healthy'])
        self.assertIn('blocked=1', result['issues'])
        self.assertIn('message_errors=1', result['issues'])
        self.assertEqual(result['liveness_issues'], [])
        self.assertEqual(result['queue_issues'], ['blocked=1', 'message_errors=1'])
        for private in ('secret', 'token', 'private-uid', '@example.com'):
            self.assertNotIn(private, json.dumps(result))

    def test_ambiguous_import_and_source_issue_are_unhealthy(self):
        self.message(status='importing', source_issue='uidl_reappeared_or_changed')
        self.assertIn('importing=1', health(self.cfg)['issues'])
        self.assertIn('message_errors=1', health(self.cfg)['issues'])

    def test_sync_and_notification_errors_are_reported_as_fixed_codes(self):
        self.state.set('last_error', 'private API response')
        self.state.set('telegram_error', 'private token')
        self.assertEqual(health(self.cfg)['issues'], ['sync_error', 'telegram_error'])

    def test_cleanup_holds_and_pending_results(self):
        self.cfg.source_cleanup_enabled = True
        self.message(status='imported', cleanup_issue='spam_trash')
        self.assertTrue(health(self.cfg)['healthy'])
        self.state.update('private-uid', cleanup_issue='gmail_missing')
        self.assertIn('cleanup_issues=1', health(self.cfg)['issues'])
        self.state.update('private-uid', present=0)
        self.assertTrue(health(self.cfg)['healthy'])
        self.state.update('private-uid', cleanup_state='pending')
        self.state.set('cleanup_error', 'private error')
        self.assertEqual(health(self.cfg)['issues'], ['cleanup_error', 'cleanup_pending=1'])
        self.cfg.source_cleanup_enabled = False
        self.assertTrue(health(self.cfg)['healthy'])

    def test_missing_database_is_unhealthy_and_not_created(self):
        self.cfg.state_dir = str(Path(self.tmp.name) / 'missing')
        self.assertIn('state_unreadable', health(self.cfg)['issues'])
        self.assertFalse(Path(self.cfg.state_dir).exists())

    def test_each_timer_and_user_bus_failure(self):
        for index, unit in enumerate(('email2gmail.timer', 'email2gmail-watchdog.timer')):
            with self.subTest(unit=unit):
                results = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                           subprocess.CompletedProcess([], 1)]
                results[index].returncode = 3
                self.run.side_effect = results
                self.assertEqual(health(self.cfg)['issues'], [f'{unit}_inactive'])
        for error in (OSError('no bus'), subprocess.TimeoutExpired('systemctl', 5)):
            with self.subTest(error=type(error).__name__):
                self.run.side_effect = error
                self.assertFalse(health(self.cfg)['healthy'])

    def test_stalled_sync_is_independent_of_existing_queue_failure(self):
        self.message(status='blocked', error='gmail_http_400')
        self.state.set('last_scan_ok', self.now - 901)
        result = health(self.cfg)
        self.assertEqual(result['liveness_issues'], ['scan_stale'])
        self.assertEqual(result['queue_issues'], ['blocked=1', 'message_errors=1'])

    def test_failed_watchdog_service_with_active_timers(self):
        self.run.side_effect = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0)
        result = health(self.cfg)
        self.assertEqual(result['liveness_issues'], ['watchdog_failed'])
        self.assertEqual(result['queue_issues'], [])

    def test_cli_exit_status_matches_verdict(self):
        for error, expected in [(None, 0), ('gmail_timeout', 1)]:
            with self.subTest(error=error):
                self.state.set('last_error', error)
                out = io.StringIO()
                with patch('sys.argv', ['email2gmail', 'health']), patch.object(Config, 'load', return_value=self.cfg), contextlib.redirect_stdout(out):
                    self.assertEqual(main(), expected)
                self.assertEqual(json.loads(out.getvalue())['healthy'], expected == 0)

    def test_operator_can_ignore_one_reviewed_blocked_message(self):
        self.message(status='blocked', error='gmail_http_400', source_issue='reviewed')
        spool = self.state.spool('private-uid')
        spool.write_bytes(b'preserved evidence')
        ref = digest(b'private-uid')[:16]

        result = ignore_message(self.cfg, ref)

        row = self.state.row('private-uid')
        self.assertEqual(row['status'], 'ignored')
        self.assertEqual(row['error'], 'gmail_http_400')
        self.assertEqual(row['source_issue'], 'reviewed')
        self.assertTrue(spool.is_file())
        self.assertEqual(result, {'ignored': True, 'ref': ref, 'source_present': True,
                                  'spool_retained': True, 'prior_error': 'gmail_http_400',
                                  'prior_source_issue': 'reviewed'})
        self.assertTrue(health(self.cfg)['healthy'])
        self.assertEqual(status(self.cfg)['message_errors'][0]['status'], 'ignored')

    def test_ignore_is_fail_closed_for_bad_ref_and_nonblocked_mail(self):
        self.message(status='importing', error='gmail_timeout')
        ref = digest(b'private-uid')[:16]
        for value, error in [('not-a-ref', 'invalid_message_ref'),
                             ('0' * 16, 'message_ref_not_found'),
                             (ref, 'message_not_blocked')]:
            with self.subTest(value=value), self.assertRaisesRegex(BridgeError, error):
                ignore_message(self.cfg, value)
        self.assertEqual(self.state.row('private-uid')['status'], 'importing')
        self.state.update('private-uid', status='blocked', error='gmail_search_limit')
        with self.assertRaisesRegex(BridgeError, 'message_import_unresolved'):
            ignore_message(self.cfg, ref)
        self.assertEqual(self.state.row('private-uid')['status'], 'blocked')

    def test_ignored_message_stays_quiet_after_source_reappearance(self):
        self.message(status='blocked', error='gmail_http_400')
        ref = digest(b'private-uid')[:16]
        ignore_message(self.cfg, ref)

        self.state.snapshot([], self.cfg, self.now + 1)
        self.state.snapshot([(1, 'private-uid', 100)], self.cfg, self.now + 2)

        row = self.state.row('private-uid')
        self.assertEqual(row['status'], 'ignored')
        self.assertEqual(row['error'], 'gmail_http_400')
        self.assertEqual(row['source_issue'], 'uidl_reappeared_or_changed')
        self.assertTrue(health(self.cfg)['healthy'])

        self.state.snapshot([], self.cfg, self.now + 3)
        self.state.snapshot([(1, 'private-uid', 101)], self.cfg, self.now + 4)
        row = self.state.row('private-uid')
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['size'], 101)
        self.assertEqual(health(self.cfg)['queue_issues'], ['blocked=1', 'message_errors=1'])

    def test_ignore_reports_busy_and_does_not_mutate(self):
        self.message(status='blocked', error='gmail_http_400')
        ref = digest(b'private-uid')[:16]
        out = io.StringIO()
        with lock(self.cfg.state/'sync.lock'), \
             patch('sys.argv', ['email2gmail', 'ignore', ref]), \
             patch.object(Config, 'load', return_value=self.cfg), \
             contextlib.redirect_stdout(out):
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(out.getvalue()), {'error': 'message_state_busy'})
        self.assertEqual(self.state.row('private-uid')['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
