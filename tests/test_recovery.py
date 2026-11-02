"""Gmail reconciliation and recovery against the real ``Gmail.call()``.

Only the gws subprocess and the monotonic clock are fake, and every call stays
under the real per-call timeout (``gmail_timeout``, 120 s).
"""
import base64
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from email2gmail.core import Gmail, headers, message_id, sync
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


if __name__ == '__main__':
    unittest.main()
