import base64
import dataclasses
import datetime
from email.message import EmailMessage
from email import policy
from email.utils import format_datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from email2gmail.core import (POP_PASSWORD_KEY, POP_USER_KEY, BridgeError, Config, Gmail, GmailError, PopSource, State,
                             Telegram, atomic_write, digest, fingerprint, headers, lock, message_id, notify, prepare,
                             read_secrets, sync)

# Inside the default 30-day initial window whenever the suite runs.
RECENT = format_datetime(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))


def mail(mid='one@example.com', body='hello', date=RECENT):
    m = EmailMessage(policy=policy.SMTP)
    m['From'] = 'sender@example.com'
    m['To'] = 'company@example.com'
    m['Subject'] = '한글 제목'
    if mid:
        m['Message-ID'] = '<'+mid+'>'
    if date:
        m['Date'] = date
    m.set_content(body)
    m.add_attachment(b'\x00\xff\x01binary\r\n..attachment', maintype='application', subtype='octet-stream', filename='첨부.bin')
    return m.as_bytes()


class Source:
    def __init__(self, messages):
        self.messages = messages
        self.gets = []

    def listing(self):
        return [(i, uid, len(raw)) for i, (uid, raw) in enumerate(self.messages.items(), 1)]

    def retrieve(self, n):
        self.gets.append(n)
        return list(self.messages.values())[n-1]


class FakeGmail:
    def __init__(self):
        self.imports = []
        self.stored = {}
        self.fail = None
        self.visible = True
        self.labels = []

    def verify_account(self): pass
    def label(self): return 'CompanyLabel'
    def add_label(self, gid, label): self.labels.append((gid, label))

    def find_match(self, row, label):
        if not self.visible:
            return None
        for gid, raw in self.stored.items():
            if row['marker'] == str(headers(raw).get('X-Email2Gmail-Key', '')) or (
                    row['mid'] and message_id(raw) == row['mid'] and fingerprint(raw) == row['fingerprint']):
                return gid
        return None

    def import_mail(self, path, label, dated):
        data = path.read_bytes()
        self.imports.append(data)
        gid = str(len(self.imports))
        if not self.fail or not self.fail.rejected:
            self.stored[gid] = data
        if self.fail:
            raise self.fail
        return gid


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config('gmail@example.com', 'company@example.com', 'pop.example.com', '123456789', state_dir=self.tmp.name)
        self.state = State(self.cfg)
        self.gmail = FakeGmail()
        self.now = time.time()

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def run_sync(self, messages, advance=0, maximum=None):
        sync(self.cfg, self.state, self.gmail, Source(messages), self.now+advance, maximum)

    def test_success_rerun_and_sequence_reorder(self):
        messages = {'a': mail(), 'b': mail('two@example.com')}
        self.run_sync(messages)
        self.run_sync(dict(reversed(list(messages.items()))), 120)
        self.assertEqual(len(self.gmail.imports), 2)
        self.assertEqual(self.state.counts(), {'imported': 2})
        self.assertEqual(list((self.cfg.state/'spool').glob('*.eml')), [])

    def test_ambiguous_acceptance_delayed_search_no_duplicate_and_other_mail(self):
        self.gmail.fail = GmailError('gmail_timeout')
        self.run_sync({'a': mail()})
        self.assertEqual(self.state.row('a')['status'], 'importing')
        self.gmail.fail = None
        self.gmail.visible = False
        self.run_sync({'a': mail(), 'b': mail('two@example.com')}, 120)
        self.assertEqual(len(self.gmail.imports), 2)
        self.gmail.visible = True
        self.run_sync({'a': mail(), 'b': mail('two@example.com')}, 240)
        self.assertEqual(len(self.gmail.imports), 2)
        self.assertEqual(self.state.row('a')['status'], 'imported')

    def test_crash_after_acceptance_before_database_commit(self):
        real = self.state.update
        def crash(uid, **kw):
            if kw.get('status') == 'imported':
                raise SystemExit('simulated crash')
            return real(uid, **kw)
        with patch.object(self.state, 'update', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.run_sync({'a': mail()})
        self.run_sync({}, 120)  # Original can disappear while payload remains durable.
        self.assertEqual(len(self.gmail.imports), 1)
        self.assertEqual(self.state.row('a')['status'], 'imported')

    def test_explicit_quota_rejection_backoff_then_retry(self):
        self.gmail.fail = GmailError('gmail_http_429', rejected=True)
        self.run_sync({'a': mail()})
        self.assertEqual(self.state.row('a')['status'], 'prepared')
        self.gmail.fail = None
        self.run_sync({'a': mail()}, 30)
        self.assertEqual(len(self.gmail.imports), 1)
        self.run_sync({'a': mail()}, 121)
        self.assertEqual(self.state.row('a')['status'], 'imported')

    def test_same_message_id_different_content_is_not_dropped(self):
        self.run_sync({'a': mail(body='first'), 'b': mail(body='second')})
        self.assertEqual(len(self.gmail.imports), 2)

    def test_existing_gmail_is_labeled_without_reimport(self):
        raw = mail()
        self.gmail.stored['old'] = raw
        self.run_sync({'a': raw})
        self.assertEqual(len(self.gmail.imports), 0)
        self.assertEqual(self.state.row('a')['status'], 'existing')
        self.assertEqual(self.gmail.labels, [('old', 'CompanyLabel')])

    def test_initial_boundary_does_not_filter_new_arrivals_with_old_date(self):
        old = mail('old@example.com', date='Mon, 01 Jun 2020 10:00:00 +0900')
        self.run_sync({'a': old})
        self.run_sync({'a': old, 'b': old}, 120)
        self.assertEqual(self.state.row('a')['status'], 'skipped')
        self.assertEqual(self.state.row('b')['status'], 'imported')

    def test_future_only_snapshot_and_restart(self):
        self.cfg.initial_days = 0
        self.run_sync({'a': mail()})
        self.cfg.initial_days = 30  # Initial scope already sealed.
        self.run_sync({'a': mail(), 'b': mail('two@example.com')}, 120)
        self.assertEqual(self.state.row('a')['status'], 'skipped')
        self.assertEqual(self.state.row('b')['status'], 'imported')

    def test_undated_is_included(self):
        self.run_sync({'a': mail(date=None)})
        self.assertEqual(self.state.row('a')['status'], 'imported')

    def test_mime_and_original_headers_preserved(self):
        raw = mail()
        prepared, mid, marker = prepare(raw, self.cfg.identity, 'a')
        self.assertTrue(prepared.endswith(raw))
        self.assertEqual(prepared.split(b'\r\n', 1)[1], raw)
        self.assertEqual(mid, 'one@example.com')
        self.assertEqual(fingerprint(prepared), fingerprint(raw))
        self.assertEqual(fingerprint(prepared), fingerprint(b'X-Received: gmail-added\r\nX-Forwarded-Encrypted: gmail-added\r\n'+prepared))
        self.assertEqual(fingerprint(prepared), fingerprint(prepared.replace(b'\r\n', b'\n')))

    def test_missing_message_id_deterministic_and_invalid_id_preserved(self):
        raw = mail(mid=None)
        a, mid, _ = prepare(raw, self.cfg.identity, 'a')
        b, mid2, _ = prepare(raw, self.cfg.identity, 'b')
        self.assertEqual(mid, mid2)
        self.assertTrue(a.endswith(raw))
        bad = b'Message-ID: invalid value\r\n'+raw
        payload, mid, _ = prepare(bad, self.cfg.identity, 'c')
        self.assertIsNone(mid)
        self.assertTrue(payload.endswith(bad))

    def test_account_state_cannot_be_reused(self):
        other = dataclasses.replace(self.cfg, gmail_account='other@example.com')
        with self.assertRaisesRegex(BridgeError, 'state_account_mismatch'):
            State(other)

    def test_uidl_reappearance_or_size_change_blocks(self):
        self.run_sync({'a': mail()})
        self.run_sync({}, 120)
        self.run_sync({'a': mail()}, 240)
        self.assertEqual(self.state.row('a')['status'], 'imported')
        self.assertEqual(self.state.row('a')['source_issue'], 'uidl_reappeared_or_changed')
        self.assertEqual(len(self.gmail.imports), 1)

    def test_missing_source_before_download_is_visible(self):
        self.run_sync({'a': mail(), 'b': mail('two@example.com')}, maximum=1)
        self.run_sync({'b': mail('two@example.com')}, 120)
        self.assertEqual(self.state.row('a')['error'], 'source_removed_before_download')

    def test_oversized_mail_does_not_block_small_mail(self):
        self.cfg.max_message_bytes = 1000
        self.run_sync({'a': b'x'*1001, 'b': b'From: a@example.com\r\n\r\nsmall\r\n'})
        self.assertEqual(self.state.row('a')['status'], 'blocked')
        self.assertEqual(self.state.row('b')['status'], 'imported')

    def test_lock_prevents_overlap(self):
        with lock(self.cfg.state/'sync.lock'):
            with self.assertRaisesRegex(BridgeError, 'already_running'):
                with lock(self.cfg.state/'sync.lock'):
                    pass

    def test_spool_tampering_blocks_upload(self):
        self.gmail.fail = GmailError('gmail_http_429', rejected=True)
        self.run_sync({'a': mail()})
        self.state.spool('a').write_bytes(b'tampered')
        self.gmail.fail = None
        self.run_sync({'a': mail()}, 121)
        self.assertEqual(len(self.gmail.imports), 1)
        self.assertEqual(self.state.row('a')['error'], 'spool_hash_mismatch')

    def test_telegram_failure_does_not_undo_import(self):
        self.run_sync({'a': mail()})
        class FailTelegram:
            def send(self, text): raise BridgeError('telegram_http_403')
        with self.assertRaises(BridgeError):
            notify(self.cfg, self.state, FailTelegram(), self.now, force=True)
        self.run_sync({'a': mail()}, 120)
        self.assertEqual(len(self.gmail.imports), 1)
        self.assertIsNone(self.state.get('last_notice'))

    def test_notification_only_counts_no_subject_body(self):
        self.run_sync({'a': mail()})
        output = []
        class FakeTelegram:
            def send(self, text): output.append(text); return 42
        notify(self.cfg, self.state, FakeTelegram(), self.now, force=True)
        notify(self.cfg, self.state, FakeTelegram(), self.now+120)
        self.assertEqual(len(output), 1)
        self.assertNotIn('한글 제목', output[0])
        self.assertNotIn('hello', output[0])
        self.assertEqual(self.state.get('last_telegram_id'), 42)

    def test_secret_parser_colon_and_shell_syntax_never_evaluated(self):
        path = self.cfg.state/'input.env'
        path.write_text('EMAIL2GMAIL_POP_USER: company@example.com\nEMAIL2GMAIL_POP_PASSWORD="$(touch /tmp/nope) # !"\nIGNORED=ignore\n')
        values = read_secrets(path, {POP_USER_KEY, POP_PASSWORD_KEY})
        self.assertEqual(values[POP_PASSWORD_KEY], '$(touch /tmp/nope) # !')
        self.assertNotIn('IGNORED', values)

    def test_telegram_uses_dedicated_token_from_shared_file(self):
        from io import BytesIO
        path = self.cfg.state/'input.env'
        path.write_text('EMAIL2GMAIL_POP_USER=company@example.com\nEMAIL2GMAIL_POP_PASSWORD="password # !"\n'
                        'EMAIL2GMAIL_TELEGRAM_TOKEN="123:project-test-token"\n'
                        'TELEGRAM_NOTIFY_BOT_TOKEN=other-notify-token\nTELEGRAM_BOT_TOKEN=other-main-token\n')
        self.cfg.credentials_file = self.cfg.telegram_file = str(path)
        with patch.dict(os.environ, {}, clear=True), patch('email2gmail.core.urllib.request.urlopen') as request:
            request.return_value = BytesIO(b'{"ok":true,"result":{"message_id":42}}')
            values = read_secrets(path, {POP_USER_KEY, POP_PASSWORD_KEY})
            self.assertEqual(values[POP_USER_KEY], self.cfg.pop_account)
            self.assertEqual(values[POP_PASSWORD_KEY], 'password # !')
            self.assertEqual(Telegram(self.cfg).send('status'), 42)
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, 'https://api.telegram.org/bot123:project-test-token/sendMessage')
            self.assertEqual(json.loads(sent.data)['chat_id'], '123456789')

    def test_telegram_missing_dedicated_token_does_not_use_other_bots(self):
        path = self.cfg.state/'input.env'
        path.write_text('TELEGRAM_NOTIFY_BOT_TOKEN=other-notify-token\nTELEGRAM_BOT_TOKEN=other-main-token\n')
        self.cfg.telegram_file = str(path)
        with patch.dict(os.environ, {}, clear=True), patch('email2gmail.core.urllib.request.urlopen') as request:
            with self.assertRaisesRegex(BridgeError, '^missing_credentials$'):
                Telegram(self.cfg).send('status')
            request.assert_not_called()

    def test_pop_long_lines_dot_unstuffing_and_size_bound(self):
        from io import BytesIO
        from types import SimpleNamespace
        wire = b'Subject: test\r\n\r\n'+b'x'*10000+b'\r\n..dot\r\n.\r\n'
        src = object.__new__(PopSource)
        src.max_bytes = 20000
        src.p = SimpleNamespace(file=BytesIO(wire), _putcmd=lambda c: None, _getresp=lambda: b'+OK')
        result = src.retrieve(1)
        self.assertIn(b'x'*10000, result)
        self.assertTrue(result.endswith(b'\r\n.dot\r\n'))
        src.max_bytes = 100
        src.p.file = BytesIO(wire)
        with self.assertRaisesRegex(BridgeError, 'message_too_large'):
            src.retrieve(1)

    def test_reply_and_thread_headers_distinguish_reused_ids(self):
        raw = mail()
        variants = {str(i): (f'{header}: value{i}@example.com\r\n'.encode()+raw)
                    for i, header in enumerate(['Reply-To', 'References', 'In-Reply-To'])}
        self.run_sync(variants)
        self.assertEqual(len(self.gmail.imports), 3)

    def test_poisoned_download_rotates_behind_unattempted_mail(self):
        source = Source({'good': mail(), 'bad': mail('bad@example.com')})
        real = source.retrieve
        def broken(n):
            if n == 2:
                raise TimeoutError()
            return real(n)
        source.retrieve = broken
        sync(self.cfg, self.state, self.gmail, source, self.now, 1)
        sync(self.cfg, self.state, self.gmail, source, self.now+120, 1)
        self.assertEqual(self.state.row('good')['status'], 'imported')

    def test_ambiguous_recovery_rotates_to_later_prepared(self):
        self.gmail.fail = GmailError('gmail_timeout')
        self.run_sync({'a': mail()})
        self.gmail.visible = False
        self.gmail.fail = GmailError('gmail_http_429', rejected=True)
        self.run_sync({'a': mail(), 'b': mail('two@example.com')}, 120)
        self.gmail.fail = None
        self.run_sync({'a': mail(), 'b': mail('two@example.com')}, 240, 1)
        self.run_sync({'a': mail(), 'b': mail('two@example.com')}, 360, 1)
        self.assertEqual(self.state.row('b')['status'], 'imported')

    def test_spooled_mail_recovers_before_pop_listing_failure(self):
        self.gmail.fail = GmailError('gmail_http_429', rejected=True)
        self.run_sync({'a': mail()})
        self.gmail.fail = None
        class BrokenSource:
            def listing(self): raise BridgeError('pop_failed')
        with self.assertRaisesRegex(BridgeError, 'pop_failed'):
            sync(self.cfg, self.state, self.gmail, BrokenSource(), self.now+121)
        self.assertEqual(self.state.row('a')['status'], 'imported')

    def test_gws_upload_uses_path_not_message_argv_and_scan_flags(self):
        raw = mail()
        path = self.cfg.state/'payload.eml'
        path.write_bytes(raw)
        from types import SimpleNamespace
        with patch('email2gmail.core.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=b'{"id":"abc"}')) as run:
            Gmail(self.cfg).import_mail(path, 'CompanyLabel', True)
        argv = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs['cwd'], str(path.parent))
        self.assertIn('--upload-content-type', argv)
        self.assertIn('message/rfc822', argv)
        params = json.loads(argv[argv.index('--params')+1])
        self.assertFalse(params['neverMarkSpam'])
        self.assertFalse(params['processForCalendar'])
        self.assertNotIn(POP_PASSWORD_KEY, run.call_args.kwargs['env'])

    def test_invalid_message_id_first_attempt_skips_remote_search(self):
        g = Gmail(self.cfg)
        with patch.object(g, 'call') as call:
            self.assertIsNone(g.find_match({'mid': None, 'attempts': 0}, 'CompanyLabel'))
        call.assert_not_called()

    def test_invalid_id_recovery_is_metadata_only_and_bounded(self):
        g = Gmail(self.cfg)
        row = {'mid': None, 'attempts': 1, 'marker': 'expected', 'fingerprint': 'unused'}
        listing = {'messages': [{'id': str(i)} for i in range(25)], 'nextPageToken': 'more'}
        with patch.object(g, 'call', side_effect=[listing]+[{'payload': {'headers': []}}]*25) as call, \
             patch.object(g, 'raw') as raw:
            with self.assertRaisesRegex(BridgeError, 'gmail_search_limit'):
                g.find_match(row, 'CompanyLabel')
        self.assertEqual(call.call_count, 26)
        raw.assert_not_called()

    def test_search_limit_preserves_payload_and_stops_repeated_scan(self):
        with patch.object(self.gmail, 'find_match', side_effect=BridgeError('gmail_search_limit')):
            self.run_sync({'a': mail()})
        self.assertEqual(self.state.row('a')['status'], 'blocked')
        self.assertTrue(self.state.spool('a').exists())
        self.run_sync({'a': mail(), 'b': mail('two@example.com')}, 120)
        self.assertEqual(self.state.row('b')['status'], 'imported')

    def test_uidl_change_does_not_strand_prepared_payload(self):
        original = mail()
        self.gmail.fail = GmailError('gmail_http_429', rejected=True)
        self.run_sync({'a': original})
        self.gmail.fail = None
        replacement = mail(body='different-sized source replacement')
        self.run_sync({'a': replacement}, 30)
        self.assertEqual(self.state.row('a')['status'], 'prepared')
        self.run_sync({'a': replacement}, 121)
        self.assertEqual(self.state.row('a')['status'], 'imported')
        self.assertTrue(self.gmail.imports[-1].endswith(original))
        self.assertIsNotNone(self.state.row('a')['source_issue'])

    def test_uidl_change_keeps_ambiguous_reconciliation_alive(self):
        original = mail()
        self.gmail.fail = GmailError('gmail_timeout')
        self.run_sync({'a': original})
        self.gmail.fail = None
        self.gmail.visible = False
        self.run_sync({'a': mail(body='replacement')}, 30)
        self.assertEqual(self.state.row('a')['status'], 'importing')
        self.gmail.visible = True
        self.run_sync({'a': mail(body='replacement')}, 121)
        self.assertEqual(self.state.row('a')['status'], 'imported')
        self.assertEqual(len(self.gmail.imports), 1)

    def test_readonly_state_does_not_write_identity(self):
        read = State(self.cfg, readonly=True)
        try:
            self.assertEqual(read.get('identity'), self.cfg.identity)
            import sqlite3
            with self.assertRaises(sqlite3.OperationalError):
                read.set('probe', True)
        finally:
            read.close()

    def test_watchdog_alerts_when_state_database_cannot_open(self):
        import sqlite3
        from io import StringIO
        from contextlib import redirect_stdout
        from email2gmail.cli import watchdog
        with patch.dict('os.environ', {'XDG_RUNTIME_DIR': self.tmp.name}), \
             patch('email2gmail.cli.State', side_effect=sqlite3.OperationalError('simulated unavailable DB')), \
             patch('email2gmail.cli.Telegram') as telegram, redirect_stdout(StringIO()):
            telegram.return_value.send.return_value = 99
            self.assertEqual(watchdog(self.cfg), 1)
            self.assertEqual(watchdog(self.cfg), 1)
        telegram.return_value.send.assert_called_once()
        self.assertIn('상태 DB 읽기 실패', telegram.return_value.send.call_args.args[0])

    def test_watchdog_alerts_from_readonly_database(self):
        from io import StringIO
        from contextlib import redirect_stdout
        from email2gmail.cli import watchdog
        self.state.set('last_scan_ok', self.now-7200)
        with patch.dict('os.environ', {'XDG_RUNTIME_DIR': self.tmp.name}), \
             patch('email2gmail.cli.Telegram') as telegram, redirect_stdout(StringIO()):
            telegram.return_value.send.return_value = 99
            self.assertEqual(watchdog(self.cfg), 1)
        telegram.return_value.send.assert_called_once()

    def test_gmail_cycle_deadline_prevents_new_remote_call(self):
        g = Gmail(self.cfg)
        g.deadline = time.monotonic()-1
        with patch('email2gmail.core.subprocess.run') as call:
            with self.assertRaisesRegex(GmailError, 'cycle_time_budget'):
                g.verify_account()
        call.assert_not_called()

    def test_out_of_range_date_cannot_crash_fingerprint(self):
        raw = b'Date: Fri, 11 Sep 999999999999999999 10:00:00 +0900\r\nFrom: a@example.com\r\n\r\nbody\r\n'
        self.assertTrue(fingerprint(raw))


if __name__ == '__main__':
    unittest.main()
