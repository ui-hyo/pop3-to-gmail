"""Conservative source cleanup. Preview is the default; automation is opt-in."""
from __future__ import annotations

import contextlib
import time

from .core import BridgeError, Gmail, GmailError, PopSource, State, fingerprint, headers, prepare, safe_error


def cleanup(cfg, state: State, *, apply=False, gmail=None, source=None, now=None):
    """Delete only aged source copies that match durable, non-expiring Gmail copies.

    Fresh POP UIDL + source body + Gmail body are all verified in one session.
    SPAM/TRASH are ineligible because Gmail can auto-expire those folders.
    DELE is only staged after every candidate has been inspected. QUIT commits it.
    A lost QUIT reply stays pending and is reconciled from a fresh complete listing.
    """
    now = time.time() if now is None else now
    gmail = gmail or Gmail(cfg)
    gmail.verify_account()
    source = source or PopSource(cfg)
    finished = False
    staging = False
    try:
        listing = source.listing()
        available = {uid: (n, size) for n, uid, size in listing}
        result = {'mode': 'apply' if apply else 'preview', 'retention_days': cfg.source_retention_days,
                  'eligible': 0, 'too_recent': 0, 'held_spam_trash': 0, 'mismatch': 0,
                  'absent': 0, 'deleted': 0, 'pending_commit': 0, 'gmail_missing': 0,
                  'gmail_unavailable': 0, 'deferred': 0, 'checked': 0}
        def hold(uid, issue, delay=3600):
            if apply:
                state.update(uid, cleanup_issue=issue, cleanup_next_try=now+delay)

        def gmail_failure(uid, exc):
            missing = str(exc) == 'gmail_http_404'
            key = 'gmail_missing' if missing else 'gmail_unavailable'
            result[key] += 1
            hold(uid, key, 86400 if missing else 3600)

        candidates = []
        rows = state.db.execute("SELECT * FROM messages WHERE status='imported' AND gmail_id IS NOT NULL ORDER BY cleanup_next_try, rowid").fetchall()
        for row in rows:
            uid = row['uid']
            if uid not in available:
                result['absent'] += 1
                if apply and row['cleanup_state'] == 'pending':
                    state.update(uid, cleanup_state='confirmed_absent', source_deleted_at=now)
                continue
            if row['delivered_at'] is None or now-row['delivered_at'] < cfg.source_retention_days*86400:
                result['too_recent'] += 1
                continue
            if apply and row['cleanup_next_try'] > now:
                result['deferred'] += 1
                continue
            if result['checked'] >= cfg.max_messages:
                break
            result['checked'] += 1
            n, _ = available[uid]
            try:
                # Cheap metadata check avoids downloading bodies for known spam/trash.
                if {'SPAM', 'TRASH'} & set(gmail.current_labels(row['gmail_id'])):
                    result['held_spam_trash'] += 1
                    hold(uid, 'spam_trash')
                    continue
                remote, labels = gmail.raw_with_labels(row['gmail_id'])
            except GmailError as exc:
                gmail_failure(uid, exc)
                continue
            if {'SPAM', 'TRASH'} & set(labels):
                result['held_spam_trash'] += 1
                hold(uid, 'spam_trash')
                continue
            # Check mapping immediately before RETR, never rely on a prior session's number.
            if source.uid_at(n) != uid:
                raise BridgeError('cleanup_uidl_changed')
            original = source.retrieve(n)
            payload, _, marker = prepare(original, cfg.identity, uid)
            expected = fingerprint(payload)
            marker_ok = marker in [str(v) for v in headers(remote).get_all('X-Email2Gmail-Key', [])]
            if not marker_ok or expected != row['fingerprint'] or fingerprint(remote) != expected:
                result['mismatch'] += 1
                hold(uid, 'fingerprint_mismatch', 86400)
                continue
            candidates.append((n, uid))
        result['eligible'] = len(candidates)
        if not apply or not candidates:
            return result
        staged_uids = []
        staging = True
        try:
            for number, uid in sorted(candidates, reverse=True):
                # The session stays open throughout verification and staging.
                if source.uid_at(number) != uid:
                    raise BridgeError('cleanup_uidl_changed')
                try:
                    latest_labels = gmail.current_labels(state.row(uid)['gmail_id'])
                except GmailError as exc:
                    gmail_failure(uid, exc)
                    result['eligible'] -= 1
                    continue
                if {'SPAM', 'TRASH'} & set(latest_labels):
                    result['held_spam_trash'] += 1
                    result['eligible'] -= 1
                    hold(uid, 'spam_trash')
                    continue
                state.update(uid, cleanup_state='pending', cleanup_issue=None)
                staged_uids.append(uid)
                source.delete(number)
            source.commit_deletions()
            finished = True
        except Exception:
            # Without a positive QUIT reply the result is unknown; never claim success.
            finished = True
            with contextlib.suppress(Exception):
                source.abort_deletions()
            result['pending_commit'] = len(staged_uids)
            raise BridgeError('cleanup_commit_unconfirmed') from None
        for uid in staged_uids:
            state.update(uid, cleanup_state='confirmed', source_deleted_at=now, present=0, cleanup_issue=None)
        result['deleted'] = len(staged_uids)
        state.set('last_cleanup_ok', now)
        return result
    finally:
        if not finished:
            if staging:
                with contextlib.suppress(Exception):
                    source.abort_deletions()
            else:
                source.close()
