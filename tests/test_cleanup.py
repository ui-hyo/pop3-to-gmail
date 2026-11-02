import contextlib
import io
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

from email2gmail.cleanup import cleanup
from email2gmail.core import BridgeError, GmailError, Config, notify, sync
import test_bridge
from test_bridge import FakeGmail, Source, mail


class CleanupSource(Source):
    def __init__(self, messages):
        super().__init__(messages)
        self.deletions = []
        self.committed = False
        self.aborted = False
        self.quit_fail = False

    def uid_at(self, n): return list(self.messages)[n-1]
    def delete(self, n): self.deletions.append(n)
    def close(self): pass
    def abort_deletions(self): self.aborted = True
    def commit_deletions(self):
        if self.quit_fail: raise TimeoutError()
        self.committed = True


class CleanupGmail(FakeGmail):
    def __init__(self):
        super().__init__()
        self.remote_labels = ['CompanyLabel', 'INBOX']

    def current_labels(self, gid):
        return self.remote_labels

    def raw_with_labels(self, gid):
        return self.stored[gid], self.remote_labels


class CleanupTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown

    def setup_delivered(self):
        raw = mail()
        self.gmail = CleanupGmail()
        self.source = CleanupSource({'a': raw})
        sync(self.cfg, self.state, self.gmail, self.source, self.now)
        return self.now + 8*86400

    def test_preview_never_deletes_or_marks_attempt(self):
        later = self.setup_delivered()
        result = cleanup(self.cfg, self.state, gmail=self.gmail, source=self.source, now=later)
        self.assertEqual(result['eligible'], 1)
        self.assertEqual(self.source.deletions, [])
        self.assertIsNone(self.state.row('a')['cleanup_state'])

    def test_retention_prevents_early_delete(self):
        self.setup_delivered()
        result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=self.now+86400)
        self.assertEqual(result['too_recent'], 1)
        self.assertEqual(self.source.deletions, [])

    def test_verified_aged_mail_deletes_only_after_commit(self):
        later = self.setup_delivered()
        result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later)
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(self.source.committed)
        self.assertEqual(self.state.row('a')['cleanup_state'], 'confirmed')

    def test_spam_trash_are_never_deleted(self):
        later = self.setup_delivered()
        for label in ['SPAM', 'TRASH']:
            later += 3601
            self.gmail.remote_labels = ['CompanyLabel', label]
            result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later)
            self.assertEqual(result['held_spam_trash'], 1)
        self.assertEqual(self.source.deletions, [])

    def test_source_replacement_or_gmail_mismatch_prevents_delete(self):
        later = self.setup_delivered()
        self.source.messages['a'] = mail(body='replacement')
        result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later)
        self.assertEqual(result['mismatch'], 1)
        self.assertEqual(self.source.deletions, [])

    def test_lost_quit_reply_stays_pending_then_absence_reconciles(self):
        later = self.setup_delivered()
        self.source.quit_fail = True
        with self.assertRaisesRegex(BridgeError, 'cleanup_commit_unconfirmed'):
            cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later)
        self.assertEqual(self.state.row('a')['cleanup_state'], 'pending')
        self.assertIsNone(self.state.row('a')['source_deleted_at'])
        self.assertTrue(self.source.aborted)
        result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=CleanupSource({}), now=later+120)
        self.assertEqual(result['absent'], 1)
        self.assertEqual(self.state.row('a')['cleanup_state'], 'confirmed_absent')

    def test_interruption_after_dele_aborts_without_quit(self):
        later = self.setup_delivered()
        with patch.object(self.source, 'commit_deletions', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later)
        self.assertTrue(self.source.aborted)
        self.assertFalse(self.source.committed)

    def test_uidl_mapping_change_aborts(self):
        later = self.setup_delivered()
        with patch.object(self.source, 'uid_at', return_value='other'):
            with self.assertRaisesRegex(BridgeError, 'cleanup_uidl_changed'):
                cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later)
        self.assertEqual(self.source.deletions, [])

    def test_move_to_trash_after_content_check_prevents_delete(self):
        later = self.setup_delivered()
        with patch.object(self.gmail, 'current_labels', return_value=['TRASH']):
            result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later)
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['held_spam_trash'], 1)
        self.assertEqual(self.source.deletions, [])

    def test_cleanup_mismatch_is_visible_in_telegram_summary(self):
        from email2gmail.core import notify
        self.setup_delivered()
        self.cfg.source_cleanup_enabled = True
        self.state.set('last_cleanup_result', {'mismatch': 1, 'held_spam_trash': 0})
        messages = []
        class Telegram:
            def send(self, text): messages.append(text); return 1
        notify(self.cfg, self.state, Telegram(), self.now, force=True)
        self.assertIn('확인 필요', messages[0])
        self.assertIn('검증 불일치 1건', messages[0])

    def test_missing_gmail_copy_does_not_block_other_source_cleanup(self):
        self.gmail = CleanupGmail()
        self.source = CleanupSource({'bad': mail('bad@example.com'), 'good': mail('good@example.com')})
        sync(self.cfg, self.state, self.gmail, self.source, self.now)
        missing_id = self.state.row('bad')['gmail_id']
        original = self.gmail.current_labels
        def labels(gid):
            if gid == missing_id:
                raise GmailError('gmail_http_404', rejected=True)
            return original(gid)
        with patch.object(self.gmail, 'current_labels', side_effect=labels):
            result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=self.now+8*86400)
        self.assertEqual(result['gmail_missing'], 1)
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(self.source.deletions, [2])
        self.assertEqual(self.state.row('bad')['cleanup_issue'], 'gmail_missing')
        self.assertIsNone(self.state.row('bad')['source_deleted_at'])

    def test_gmail_outage_isolated_and_deferred(self):
        later = self.setup_delivered()
        with patch.object(self.gmail, 'current_labels', side_effect=GmailError('gmail_timeout')):
            result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later)
        self.assertEqual(result['gmail_unavailable'], 1)
        self.assertEqual(self.source.deletions, [])
        with patch.object(self.gmail, 'current_labels') as get:
            result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=later+120)
        get.assert_not_called()
        self.assertEqual(result['deferred'], 1)

    def test_held_items_count_toward_budget_without_raw_download(self):
        self.gmail = CleanupGmail()
        self.source = CleanupSource({'a': mail('a@example.com'), 'b': mail('b@example.com')})
        sync(self.cfg, self.state, self.gmail, self.source, self.now)
        self.cfg.max_messages = 1
        self.gmail.remote_labels = ['SPAM']
        with patch.object(self.gmail, 'raw_with_labels') as raw:
            result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=self.now+8*86400)
        raw.assert_not_called()
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['held_spam_trash'], 1)

    def test_config_quoted_false_cannot_enable_deletion(self):
        path = self.cfg.state/'config.toml'
        base = ('gmail_account="g@example.com"\npop_account="p@example.com"\n'
                'pop_host="pop.example.com"\ntelegram_chat_id="123456789"\n')
        path.write_text(base)
        self.assertFalse(Config.load(path).source_cleanup_enabled)
        path.write_text(base+'source_cleanup_enabled="false"\n')
        with self.assertRaisesRegex(BridgeError, 'invalid_config'):
            Config.load(path)

    def test_config_requires_host_and_quoted_numeric_chat_id(self):
        path = self.cfg.state/'config.toml'
        accounts = 'gmail_account="g@example.com"\npop_account="p@example.com"\n'
        host = 'pop_host="pop.example.com"\n'
        for body in (accounts+host, accounts+'telegram_chat_id="123456789"\n',
                     accounts+host+'telegram_chat_id=123456789\n', accounts+host+'telegram_chat_id="@channel"\n',
                     accounts+'pop_host=""\ntelegram_chat_id="123456789"\n'):
            path.write_text(body)
            with self.assertRaisesRegex(BridgeError, 'invalid_config'):
                Config.load(path)
        path.write_text(accounts+host+'telegram_chat_id="-100123"\n')
        self.assertEqual(Config.load(path).telegram_chat_id, '-100123')

    def test_disabling_cleanup_clears_active_error_and_skips_it(self):
        from email2gmail.cli import once
        from contextlib import redirect_stdout
        from io import StringIO
        self.state.set('cleanup_error', 'cleanup_commit_unconfirmed')
        self.cfg.source_cleanup_enabled = False
        with patch('email2gmail.cli.PopSource', return_value=CleanupSource({})), \
             patch('email2gmail.cli.Gmail', return_value=CleanupGmail()), \
             patch('email2gmail.cli.notify'), patch('email2gmail.cleanup.cleanup') as clean, redirect_stdout(StringIO()):
            code = once(self.cfg)
        clean.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIsNone(self.state.get('cleanup_error'))

    def test_automatic_cleanup_obeys_hourly_interval(self):
        from email2gmail.cli import once
        from contextlib import redirect_stdout
        from io import StringIO
        self.cfg.source_cleanup_enabled = True
        self.state.set('last_cleanup_attempt', time.time())
        with patch('email2gmail.cli.PopSource', return_value=CleanupSource({})), \
             patch('email2gmail.cli.Gmail', return_value=CleanupGmail()), \
             patch('email2gmail.cli.notify'), patch('email2gmail.cleanup.cleanup') as clean, redirect_stdout(StringIO()):
            once(self.cfg)
        clean.assert_not_called()


class UnconfirmedDeleteTests(unittest.TestCase):
    """A fresh listing that still shows the source resolves a stale pending delete."""
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown

    def unconfirmed(self):
        self.cfg.source_cleanup_enabled = True
        self.gmail = CleanupGmail()
        self.source = CleanupSource({'a': mail()})
        sync(self.cfg, self.state, self.gmail, self.source, self.now)
        self.later = self.now + 8*86400
        self.source.quit_fail = True                     # QUIT failed: DELE never committed
        with self.assertRaisesRegex(BridgeError, 'cleanup_commit_unconfirmed'):
            cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=self.later)
        self.assertEqual(self.state.row('a')['cleanup_state'], 'pending')
        self.source = CleanupSource(dict(self.source.messages))   # new session, source still there

    def test_present_source_clears_pending_and_keeps_history(self):
        self.unconfirmed()
        self.gmail.remote_labels = ['TRASH']                 # re-verification holds the source
        self.later += 3601
        result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=self.later)
        row = self.state.row('a')
        self.assertEqual((row['cleanup_state'], row['cleanup_issue'], result['pending_resolved']), (None, 'spam_trash', 1))
        self.assertIsNone(row['source_deleted_at'])
        self.assertEqual(self.source.deletions, [])
        history = self.state.db.execute('SELECT * FROM cleanup_unconfirmed_resolutions').fetchall()
        self.assertEqual([(h['uid'], h['resolution']) for h in history], [('a', 'source_present_after_unconfirmed_delete')])
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.db.execute('DELETE FROM cleanup_unconfirmed_resolutions')
        self.state.db.rollback()
        sent = []
        class Telegram:
            def send(self, text): sent.append(text); return 1
        notify(self.cfg, self.state, Telegram(), self.later, force=True)
        self.assertIn('정상 동작', sent[0])                 # no lasting "delete unconfirmed" alert

    def test_present_source_can_be_restaged_and_confirmed(self):
        self.unconfirmed()
        self.later += 3601
        result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=self.later)
        self.assertEqual((result['pending_resolved'], result['deleted']), (1, 1))
        self.assertEqual(self.state.row('a')['cleanup_state'], 'confirmed')

    def test_reused_or_resized_uidl_keeps_pending_alert(self):
        self.unconfirmed()
        self.source = CleanupSource({'a': mail(body='a different, longer reused message')})
        self.later += 3601
        result = cleanup(self.cfg, self.state, apply=True, gmail=self.gmail, source=self.source, now=self.later)
        self.assertEqual((self.state.row('a')['cleanup_state'], result['pending_unresolved']), ('pending', 1))
        self.assertEqual(self.source.deletions, [])

    def test_preview_never_resolves_pending(self):
        self.unconfirmed()
        cleanup(self.cfg, self.state, gmail=self.gmail, source=self.source, now=self.later+3601)
        self.assertEqual(self.state.row('a')['cleanup_state'], 'pending')


class DeliveredSpoolPruneTests(unittest.TestCase):
    """once() removes only leftover spool of cleanly delivered rows, under the sync lock."""
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown

    def once_offline(self):
        from email2gmail.cli import once
        with patch('email2gmail.cli.Gmail'), patch('email2gmail.cli.PopSource'), \
             patch('email2gmail.cli.recover'), patch('email2gmail.cli.sync'), \
             patch('email2gmail.cli.notify'), contextlib.redirect_stdout(io.StringIO()):
            return once(self.cfg)

    def crash_before_unlink(self, uid, gmail, source):
        real = Path.unlink
        target = self.state.spool(uid)
        def crash(path, *args, **kwargs):
            if path == target:
                raise SystemExit('simulated crash before spool unlink')
            return real(path, *args, **kwargs)
        with patch.object(Path, 'unlink', crash), self.assertRaises(SystemExit):
            sync(self.cfg, self.state, gmail, source, self.now)
        self.assertEqual(self.state.row(uid)['status'], 'imported')
        self.assertTrue(target.is_file())

    def test_crash_before_unlink_leaves_spool_that_next_run_removes(self):
        self.crash_before_unlink('a', self.gmail, Source({'a': mail()}))
        self.once_offline()
        self.assertFalse(self.state.spool('a').exists())

    def test_prune_keeps_spool_for_known_missing_copy_mismatch_and_pending_delete(self):
        gmail = CleanupGmail()
        for uid in ['missing', 'mismatch', 'pending']:
            source = CleanupSource({uid: mail(mid=uid+'@example.com')})
            self.crash_before_unlink(uid, gmail, source)
            later = self.now+8*86400
            if uid == 'missing':
                with patch.object(gmail, 'current_labels', side_effect=GmailError('gmail_http_404')):
                    cleanup(self.cfg, self.state, apply=True, gmail=gmail, source=source, now=later)
                self.assertEqual(self.state.row(uid)['cleanup_issue'], 'gmail_missing')
                self.state.snapshot([], self.cfg, later+1)     # source vanishes independently
            elif uid == 'mismatch':
                gmail.stored[self.state.row(uid)['gmail_id']] += b'changed body\r\n'
                cleanup(self.cfg, self.state, apply=True, gmail=gmail, source=source, now=later)
                self.assertEqual(self.state.row(uid)['cleanup_issue'], 'fingerprint_mismatch')
            else:
                source.quit_fail = True
                with self.assertRaisesRegex(BridgeError, 'cleanup_commit_unconfirmed'):
                    cleanup(self.cfg, self.state, apply=True, gmail=gmail, source=source, now=later)
                self.assertEqual(self.state.row(uid)['cleanup_state'], 'pending')
        self.assertEqual(self.state.prune_delivered_spool(), 0,
                         'cleanup incidents and pending deletes are not clean completion')
        for uid in ['missing', 'mismatch', 'pending']:
            self.assertTrue(self.state.spool(uid).is_file())

    def test_prune_exclusions_are_enumerated(self):
        """Every recovery-relevant state keeps its spool; only clean completion is pruned."""
        keep = {
            'issue_src': dict(status='imported', source_issue='uidl_reappeared_or_changed'),
            'error': dict(status='imported', error='x'),
            'no_gmail_id': dict(status='imported', gmail_id=None),
            'c_missing': dict(status='imported', cleanup_issue='gmail_missing'),
            'c_mismatch': dict(status='imported', cleanup_issue='fingerprint_mismatch'),
            'c_unavail': dict(status='imported', cleanup_issue='gmail_unavailable'),
            'c_spam': dict(status='imported', cleanup_issue='spam_trash'),
            'pending': dict(status='imported', cleanup_state='pending'),
            'conf_absent': dict(status='imported', cleanup_state='confirmed_absent', source_deleted_at=self.now),
            'not_present': dict(status='imported', present=0),
            'resolved': dict(status='imported'),
            'blocked': dict(status='blocked'), 'ignored': dict(status='ignored'),
            'prepared': dict(status='prepared'), 'importing': dict(status='importing'),
            'skipped': dict(status='skipped'), 'new': dict(status='new'),
        }
        prune = {'clean_imported': dict(status='imported'), 'clean_existing': dict(status='existing'),
                 'confirmed': dict(status='imported', cleanup_state='confirmed', present=0,
                                   source_deleted_at=self.now)}
        rows = {**keep, **prune}
        self.state.snapshot([(i, uid, 10) for i, uid in enumerate(rows, 1)], self.cfg, self.now)
        for uid, values in rows.items():
            self.state.update(uid, **{'gmail_id': 'g-'+uid, 'fingerprint': 'f', **values})
            self.state.spool(uid).write_bytes(b'payload')
        with self.state.db:
            self.state.db.execute('INSERT INTO cleanup_unconfirmed_resolutions VALUES(?,?,?,?,?,?)',
                                  ('resolved', 'g-resolved', 'f', 10, 'source_present_after_unconfirmed_delete', self.now))
        stray = self.cfg.state/'spool'/('0'*64+'.eml')
        stray.write_bytes(b'no row')
        temp = self.cfg.state/'spool'/'.tmp-partial'
        temp.write_bytes(b'partial')
        self.assertEqual(self.state.prune_delivered_spool(), len(prune))
        self.assertEqual({u for u in rows if self.state.spool(u).exists()}, set(keep))
        self.assertTrue(stray.exists() and temp.exists())
