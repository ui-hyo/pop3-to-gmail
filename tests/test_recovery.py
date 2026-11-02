"""Gmail reconciliation and recovery against the real ``Gmail.call()``.

Only the gws subprocess and the monotonic clock are fake, and every call stays
under the real per-call timeout (``gmail_timeout``, 120 s).
"""
import base64
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from email2gmail.cli import requeue_blocked
from email2gmail.core import BridgeError, Gmail, GmailError, digest, headers, message_id, recover, sync
import test_bridge
from test_bridge import Source, mail


class GwsBackend:
    """Fake gws CLI: a tiny Gmail mailbox behind ``subprocess.run``."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.mono = 0.0
        self.messages = {}            # gid -> [raw, set(labels)]
        self.durations = {}           # op -> [seconds, ...] consumed in order
        self.imports = 0
        self.lose_import_reply = False
        self.drop_import = False      # timeout before Gmail accepted anything

    def clock(self):
        return self.mono

    def add(self, gid, raw, labels=('INBOX',)):
        self.messages[gid] = [raw, set(labels)]

    def run(self, cmd, timeout, **kwargs):
        i = cmd.index('--params')
        op = '.'.join(cmd[3:i])
        params = json.loads(cmd[i+1])
        body = json.loads(cmd[cmd.index('--json')+1]) if '--json' in cmd else None
        spent = self.durations[op].pop(0) if self.durations.get(op) else 1
        assert spent <= self.cfg.gmail_timeout
        if spent > timeout:
            self.mono += timeout
            raise subprocess.TimeoutExpired(cmd, timeout)
        self.mono += spent
        if op == 'messages.import' and self.drop_import:
            self.drop_import = False
            raise subprocess.TimeoutExpired(cmd, timeout)
        result = self.respond(op, params, body, cmd)
        if op == 'messages.import' and self.lose_import_reply:
            self.lose_import_reply = False
            raise subprocess.TimeoutExpired(cmd, timeout)   # accepted, reply lost
        return SimpleNamespace(returncode=0 if 'error' not in result else 1, stdout=json.dumps(result).encode())

    def respond(self, op, params, body, cmd):
        if op == 'getProfile':
            return {'emailAddress': self.cfg.gmail_account}
        if op == 'labels.list':
            return {'labels': [{'id': 'L1', 'name': self.cfg.gmail_label}]}
        if op == 'messages.import':
            self.imports += 1
            gid = 'g%d' % (len(self.messages)+1)
            self.add(gid, Path(cmd[cmd.index('--upload')+1]).read_bytes(), body['labelIds'])
            return {'id': gid}
        if op == 'messages.list':
            ids = sorted(self.messages, reverse=True)
            if 'q' in params:
                ids = [g for g in ids if message_id(self.messages[g][0]) == params['q'].removeprefix('rfc822msgid:')]
            ids = [g for g in ids if all(label in self.messages[g][1] for label in params.get('labelIds', []))]
            out = {'messages': [{'id': g} for g in ids[:params['maxResults']]]} if ids else {}
            if len(ids) > params['maxResults']:
                out['nextPageToken'] = 'next'
            return out
        gid = params['id']
        if gid not in self.messages:
            return {'error': {'code': 404}}
        raw, labels = self.messages[gid]
        if op == 'messages.modify':
            labels.update(body['addLabelIds'])
            return {'id': gid}
        if params.get('format') == 'raw':
            return {'id': gid, 'raw': base64.urlsafe_b64encode(raw).decode(), 'labelIds': sorted(labels)}
        if params.get('format') == 'metadata':
            return {'id': gid, 'payload': {'headers': [{'name': k, 'value': str(v)} for k, v in headers(raw).items()]}}
        return {'id': gid, 'labelIds': sorted(labels)}


class GmailBudgetTests(unittest.TestCase):
    """Running out of the per-run Gmail budget must not become a permanent block."""
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown

    def run_cycle(self, backend, source, advance, durations=None):
        backend.durations = {k: list(v) for k, v in (durations or {}).items()}
        with patch('email2gmail.core.subprocess.run', backend.run), \
             patch('email2gmail.core.time.monotonic', backend.clock):
            sync(self.cfg, self.state, Gmail(self.cfg), source, self.now+advance)

    # Earlier calls use 119 s, then one search takes 117 s: 4 s remain (< 5 s).
    SLOW = {'getProfile': [60], 'labels.list': [59], 'messages.list': [117]}

    def test_slow_search_keeps_unsent_mail_prepared_and_delivers_next_run(self):
        backend = GwsBackend(self.cfg)
        backend.add('g0', mail(body='v1 original'))          # same Message-ID, other content
        source = Source({'u1': mail(body='v2 corrected')})
        self.run_cycle(backend, source, 0, self.SLOW)
        row = self.state.row('u1')
        self.assertEqual((row['status'], row['attempts'], backend.imports), ('prepared', 0, 0))
        self.assertTrue(self.state.spool('u1').exists())
        self.run_cycle(backend, source, 121)
        self.assertEqual(self.state.row('u1')['status'], 'imported')
        self.assertEqual(backend.imports, 1)

    def test_slow_reconciliation_keeps_landed_import_importing_without_resend(self):
        backend = GwsBackend(self.cfg)
        source = Source({'u1': mail()})
        backend.lose_import_reply = True
        self.run_cycle(backend, source, 0)
        self.assertEqual(self.state.row('u1')['status'], 'importing')
        self.run_cycle(backend, source, 121, self.SLOW)
        self.assertEqual(self.state.row('u1')['status'], 'importing')
        self.run_cycle(backend, source, 242)
        self.assertEqual(self.state.row('u1')['status'], 'imported')
        self.assertEqual(backend.imports, 1)

    def test_oversized_search_result_is_still_blocked(self):
        backend = GwsBackend(self.cfg)
        for i in range(26):
            backend.add('g%02d' % i, mail(body='other %d' % i))
        self.run_cycle(backend, Source({'u1': mail(body='new')}), 0)
        row = self.state.row('u1')
        self.assertEqual((row['status'], row['error'], backend.imports), ('blocked', 'gmail_search_limit', 0))



class RequeueTests(unittest.TestCase):
    """Operator-reviewed requeue of gmail_search_limit blocks."""
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    run_cycle = GmailBudgetTests.run_cycle

    def blocked_unsent(self, uid='u1', raw=None):
        raw = raw or mail()
        with patch.object(self.gmail, 'find_match', side_effect=BridgeError('gmail_search_limit')):
            sync(self.cfg, self.state, self.gmail, Source({uid: raw}), self.now)
        self.assertEqual((self.state.row(uid)['status'], self.state.row(uid)['attempts']), ('blocked', 0))
        return raw

    def blocked_after_ambiguous_import(self):
        raw = mail('two@example.com')
        self.gmail.fail = GmailError('gmail_timeout')       # accepted, reply lost
        sync(self.cfg, self.state, self.gmail, Source({'u2': raw}), self.now)
        self.gmail.fail = None
        with patch.object(self.gmail, 'find_match', side_effect=BridgeError('gmail_search_limit')):
            recover(self.cfg, self.state, self.gmail, self.now+121)
        self.assertEqual((self.state.row('u2')['status'], self.state.row('u2')['attempts']), ('blocked', 1))
        return raw

    def oversized_search_backend(self):
        backend = GwsBackend(self.cfg)
        for i in range(26):          # same Message-ID, other content: more than one result page
            backend.add('other%02d' % i, mail(body='different %d' % i))
        return backend

    def preview(self, ref=None):
        return requeue_blocked(self.cfg, ref)

    def apply(self, uid):
        ref = digest(uid.encode())[:16]
        token = {m['ref']: m['token'] for m in self.preview()['messages']}[ref]
        return requeue_blocked(self.cfg, ref, token, apply=True)

    def test_preview_is_read_only_and_proposes_safe_actions(self):
        self.blocked_unsent()
        self.blocked_after_ambiguous_import()
        before = (self.cfg.state/'state.sqlite3').read_bytes()
        report = self.preview()
        self.assertEqual((self.cfg.state/'state.sqlite3').read_bytes(), before)
        actions = {m['ref']: m['action'] for m in report['messages']}
        self.assertEqual(actions, {digest(b'u1')[:16]: 'prepared', digest(b'u2')[:16]: 'importing'})
        self.assertNotIn('u1', json.dumps(report))

    def test_preview_of_unmigrated_database_does_not_create_history(self):
        self.blocked_unsent()
        self.state.db.execute('DROP TABLE requeue_history')
        self.state.db.commit()
        self.assertEqual(self.preview()['messages'][0]['action'], 'prepared')
        self.assertFalse(self.state.has_table('requeue_history'))

    def test_unsent_item_returns_to_prepared_and_is_delivered_once(self):
        self.blocked_unsent()
        self.assertEqual(self.apply('u1')['status'], 'prepared')
        sync(self.cfg, self.state, self.gmail, Source({'u1': mail()}), self.now+121)
        self.assertEqual(self.state.row('u1')['status'], 'imported')
        self.assertEqual(len(self.gmail.imports), 1)
        history = self.state.db.execute('SELECT * FROM requeue_history').fetchall()
        self.assertEqual([(h['from_status'], h['from_error'], h['to_status']) for h in history],
                         [('blocked', 'gmail_search_limit', 'prepared')])
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.db.execute('DELETE FROM requeue_history')
        self.state.db.rollback()

    def test_attempted_item_only_resumes_search_and_never_resends(self):
        self.blocked_after_ambiguous_import()
        self.assertEqual(self.apply('u2')['status'], 'importing')
        self.gmail.visible = False                       # landed copy not searchable yet
        for i in range(1, 4):
            sync(self.cfg, self.state, self.gmail, Source({'u2': mail('two@example.com')}), self.now+121*(i+1))
            self.assertEqual(self.state.row('u2')['status'], 'importing')
        self.assertEqual(len(self.gmail.imports), 1)
        self.gmail.visible = True
        sync(self.cfg, self.state, self.gmail, Source({'u2': mail('two@example.com')}), self.now+121*6)
        self.assertEqual(self.state.row('u2')['status'], 'imported')
        self.assertEqual(len(self.gmail.imports), 1)

    def test_tampered_payload_changed_state_and_other_blocks_are_refused(self):
        self.blocked_unsent()
        ref = digest(b'u1')[:16]
        token = self.preview()['messages'][0]['token']
        self.state.spool('u1').write_bytes(b'tampered')
        self.assertEqual(self.preview()['messages'][0]['action'], 'not_eligible')
        with self.assertRaisesRegex(BridgeError, 'requeue_not_eligible'):
            requeue_blocked(self.cfg, ref, token, apply=True)
        self.state.update('u1', error='message_too_large')
        with self.assertRaisesRegex(BridgeError, 'requeue_candidate_not_found'):
            requeue_blocked(self.cfg, ref, token, apply=True)
        with self.assertRaisesRegex(BridgeError, 'requeue_confirmation_required'):
            requeue_blocked(self.cfg, ref, apply=True)
        self.assertEqual(self.state.row('u1')['status'], 'blocked')

    def test_stale_token_is_refused(self):
        self.blocked_after_ambiguous_import()
        ref = digest(b'u2')[:16]
        token = self.preview()['messages'][0]['token']
        self.state.update('u2', attempts=2)
        with self.assertRaisesRegex(BridgeError, 'requeue_exception_changed'):
            requeue_blocked(self.cfg, ref, token, apply=True)
        self.assertEqual(self.state.row('u2')['status'], 'blocked')

    def test_same_unsent_item_can_be_requeued_after_second_search_limit_block(self):
        backend = self.oversized_search_backend()
        source = Source({'review': mail(body='desired payload')})
        self.run_cycle(backend, source, 0)
        self.assertEqual((self.state.row('review')['status'], backend.imports), ('blocked', 0))
        first = self.preview()['messages'][0]
        self.assertEqual(self.apply('review')['status'], 'prepared')
        self.run_cycle(backend, source, 121)
        self.assertEqual((self.state.row('review')['status'], backend.imports), ('blocked', 0))
        backend.messages.clear()            # the operator resolves the oversized search
        second = self.preview()['messages'][0]
        self.assertNotEqual(second['token'], first['token'])   # a new recovery incident
        requeue_blocked(self.cfg, second['ref'], second['token'], apply=True)
        self.run_cycle(backend, source, 242)
        self.assertEqual((self.state.row('review')['status'], backend.imports), ('imported', 1))
        history = self.state.db.execute('SELECT to_status FROM requeue_history').fetchall()
        self.assertEqual([h[0] for h in history], ['prepared', 'prepared'])

    def test_previous_token_cannot_apply_to_a_new_incident(self):
        backend = self.oversized_search_backend()
        source = Source({'review': mail(body='desired payload')})
        self.run_cycle(backend, source, 0)
        first = self.preview()['messages'][0]
        requeue_blocked(self.cfg, first['ref'], first['token'], apply=True)
        self.run_cycle(backend, source, 121)
        with self.assertRaisesRegex(BridgeError, 'requeue_exception_changed'):
            requeue_blocked(self.cfg, first['ref'], first['token'], apply=True)
        self.assertEqual(self.state.row('review')['status'], 'blocked')



class OwnMarkerReconciliationTests(unittest.TestCase):
    """Ambiguous-import reconciliation prefers this UIDL's own marker copy."""
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    run_cycle = GmailBudgetTests.run_cycle
    BASE = mail()                       # one fixed MIME boundary for both copies
    SIBLING = b'Received: from relay2.example.com\r\n' + BASE   # same Message-ID + fingerprint

    def test_foreign_identical_copy_becomes_existing_and_gets_label(self):
        backend = GwsBackend(self.cfg)
        source = Source({'u5': self.SIBLING})
        backend.drop_import = True
        self.run_cycle(backend, source, 0)
        self.assertEqual(self.state.row('u5')['status'], 'importing')
        backend.add('g9', self.BASE)                  # e.g. a forwarded copy, no marker, no label
        self.run_cycle(backend, source, 121)
        row = self.state.row('u5')
        self.assertEqual((row['status'], row['gmail_id']), ('existing', 'g9'))
        self.assertIn('L1', backend.messages['g9'][1])
        self.assertFalse(self.state.spool('u5').exists())
        self.assertEqual(backend.imports, 0)

    def test_own_marker_copy_wins_over_foreign_identical_copy(self):
        backend = GwsBackend(self.cfg)
        source = Source({'u5': self.SIBLING})
        backend.lose_import_reply = True              # our import landed as g1
        self.run_cycle(backend, source, 0)
        backend.add('g9', self.BASE)                  # listed before g1
        self.run_cycle(backend, source, 121)
        row = self.state.row('u5')
        self.assertEqual((row['status'], row['gmail_id']), ('imported', 'g1'))
        self.assertEqual(backend.imports, 1)


if __name__ == '__main__':
    unittest.main()
