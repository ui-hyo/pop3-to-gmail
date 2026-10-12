from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import time

from .core import BridgeError, Config, Gmail, PopSource, State, Telegram, atomic_write, digest, lock, notify, recover, safe_error, sync


def status(cfg):
    state = State(cfg, readonly=True)
    try:
        errors = [dict(r) for r in state.db.execute(
            "SELECT uid, status, error, source_issue, attempts FROM messages WHERE error IS NOT NULL OR source_issue IS NOT NULL")]
        # State hashes/UIDLs and message contents are deliberately absent from output.
        for row in errors:
            row['ref'] = digest(row.pop('uid').encode())[:16]
        last = state.get('last_scan_ok')
        return {'gmail_account': cfg.gmail_account, 'counts': state.counts(),
                'source_count': state.get('source_count'), 'last_scan_ok': last,
                'scan_age_seconds': round(time.time()-last) if last else None,
                'last_error': state.get('last_error'), 'message_errors': errors,
                'last_telegram_id': state.get('last_telegram_id'),
                'telegram_error': state.get('telegram_error'),
                'source_cleanup_enabled': cfg.source_cleanup_enabled,
                'source_retention_days': cfg.source_retention_days,
                'cleanup_error': state.get('cleanup_error') if cfg.source_cleanup_enabled else None,
                'cleanup_interval_seconds': cfg.cleanup_interval_seconds,
                'cleanup_issues': dict(state.db.execute("SELECT cleanup_issue,count(*) FROM messages WHERE cleanup_issue IS NOT NULL GROUP BY cleanup_issue")),
                'last_cleanup_result': state.get('last_cleanup_result'),
                'cleanup_counts': dict(state.db.execute("SELECT coalesce(cleanup_state,'none'),count(*) FROM messages GROUP BY cleanup_state"))}
    finally:
        state.close()


def health(cfg):
    """Local, read-only monitoring verdict; emit only fixed codes and counts."""
    issues = []
    queue_issues = []
    age = None
    state = None
    try:
        state = State(cfg, readonly=True)
        state.db.execute('BEGIN')  # Read a consistent snapshot while sync is running.
        last = state.get('last_scan_ok')
        if last is None:
            issues.append('scan_never_succeeded')
        else:
            age = round(time.time() - last)
            if age < 0:
                issues.append('scan_clock_invalid')
            elif age > cfg.stale_seconds:
                issues.append('scan_stale')
        for key in ('last_error', 'telegram_error'):
            if state.get(key):
                issues.append('sync_error' if key == 'last_error' else key)
        counts = state.counts()
        for name in ('blocked', 'importing'):
            if counts.get(name):
                queue_issues.append(f'{name}={counts[name]}')
        errors = state.unresolved_count()
        if errors:
            queue_issues.append(f'message_errors={errors}')
        if cfg.source_cleanup_enabled:
            if state.get('cleanup_error'):
                queue_issues.append('cleanup_error')
            held = state.db.execute(
                "SELECT count(*) FROM messages WHERE present=1 AND cleanup_issue IS NOT NULL AND cleanup_issue!='spam_trash'").fetchone()[0]
            pending = state.db.execute(
                "SELECT count(*) FROM messages WHERE cleanup_state='pending'").fetchone()[0]
            if held:
                queue_issues.append(f'cleanup_issues={held}')
            if pending:
                queue_issues.append(f'cleanup_pending={pending}')
    except Exception:
        issues.append('state_unreadable')
        queue_issues.append('state_unreadable')
    finally:
        if state:
            state.close()

    # A monitoring probe may run as this user in the system manager, without a user-bus env.
    runtime = f'/run/user/{os.getuid()}'
    env = dict(os.environ, XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS=f'unix:path={runtime}/bus')
    for unit in ('email2gmail.timer', 'email2gmail-watchdog.timer'):
        try:
            result = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit],
                                    env=env, timeout=5, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            active = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            active = False
        if not active:
            issues.append(f'{unit}_inactive')
    try:
        result = subprocess.run(['systemctl', '--user', 'is-failed', '--quiet', 'email2gmail-watchdog.service'],
                                env=env, timeout=5, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            issues.append('watchdog_failed')
        elif result.returncode != 1:
            issues.append('watchdog_unavailable')
    except (OSError, subprocess.TimeoutExpired):
        issues.append('watchdog_unavailable')
    return {'healthy': not issues and not queue_issues, 'issues': issues + queue_issues,
            'scan_age_seconds': age, 'liveness_issues': issues, 'queue_issues': queue_issues}


def ignore_message(cfg, ref):
    """Acknowledge one operator-confirmed blocked message without deleting it."""
    ref = ref.strip().lower()
    if len(ref) != 16 or any(c not in '0123456789abcdef' for c in ref):
        raise BridgeError('invalid_message_ref')
    state = None
    try:
        try:
            with lock(cfg.state/'sync.lock'):
                state = State(cfg)
                matches = [row for row in state.db.execute(
                    'SELECT uid,status,present,error,source_issue FROM messages')
                    if digest(row['uid'].encode())[:16] == ref]
                if not matches:
                    raise BridgeError('message_ref_not_found')
                if len(matches) != 1:
                    raise BridgeError('message_ref_ambiguous')
                row = matches[0]
                if row['status'] != 'blocked':
                    raise BridgeError('message_not_blocked')
                if row['error'] == 'gmail_search_limit':
                    raise BridgeError('message_import_unresolved')
                state.update(row['uid'], status='ignored', next_try=0)
                return {'ignored': True, 'ref': ref, 'source_present': bool(row['present']),
                        'spool_retained': state.spool(row['uid']).is_file(),
                        'prior_error': row['error'], 'prior_source_issue': row['source_issue']}
        except BridgeError as exc:
            if str(exc) == 'already_running':
                raise BridgeError('message_state_busy') from None
            raise
    finally:
        if state:
            state.close()


def once(cfg, max_messages=None, force_notice=False):
    state = State(cfg)
    try:
        with lock(cfg.state/'sync.lock'):
            state.set('last_attempt', time.time())
            source = None
            try:
                gmail = Gmail(cfg)
                label = recover(cfg, state, gmail, time.time(), max_messages)
                source = PopSource(cfg)
                sync(cfg, state, gmail, source, time.time(), max_messages, label=label)
            except Exception as exc:
                state.set('last_error', safe_error(exc))
            finally:
                if source:
                    source.close()
            if not cfg.source_cleanup_enabled:
                state.set('cleanup_error', None)
            if (cfg.source_cleanup_enabled and not state.get('last_error')
                    and time.time()-state.get('last_cleanup_attempt', 0) >= cfg.cleanup_interval_seconds):
                from .cleanup import cleanup
                state.set('last_cleanup_attempt', time.time())
                try:
                    result = cleanup(cfg, state, apply=True)
                    state.set('last_cleanup_result', result)
                    state.set('cleanup_error', None)
                except Exception as exc:
                    state.set('cleanup_error', safe_error(exc))
            try:
                notify(cfg, state, Telegram(cfg), time.time(), force_notice)
            except Exception as exc:
                state.set('telegram_error', safe_error(exc))
            print(json.dumps(status(cfg), ensure_ascii=False))
            unresolved = state.unresolved_count()
            return 1 if state.get('last_error') or unresolved or state.get('telegram_error') or state.get('cleanup_error') else 0
    finally:
        state.close()


def watchdog(cfg):
    # /run is independent of the mail-state disk. Read DB without schema/identity writes.
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))/'email2gmail-watchdog'
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = runtime/'notice.json'
    with lock(runtime/'watchdog.lock'):
        try:
            previous = json.loads(record.read_text())
        except (OSError, ValueError):
            previous = {}
        state = None
        storage_error = None
        last = 0
        try:
            state = State(cfg, readonly=True)
            last = state.get('last_scan_ok', 0)
        except Exception as exc:
            storage_error = safe_error(exc)
        finally:
            if state:
                state.close()
        now = time.time()
        stale = now-last > cfg.stale_seconds
        unhealthy = bool(stale or storage_error)
        if (unhealthy and now-previous.get('notice_at', 0) >= 3600) or (previous.get('unhealthy') and not unhealthy):
            msg = ('Email2Gmail · 수집 지연 감지\n최근 15분 이상 메일 확인에 성공하지 못했습니다. 서비스 점검이 필요합니다.'
                   if unhealthy else 'Email2Gmail · 수집 복구\n메일 확인이 다시 정상적으로 완료됐습니다.')
            if storage_error:
                msg += '\n상태 DB 읽기 실패: '+storage_error
            mid = Telegram(cfg).send(msg)  # Send before optional persistence, even with a broken DB.
            previous.update(notice_at=now, telegram_id=mid)
        previous['unhealthy'] = unhealthy
        with contextlib.suppress(OSError):
            atomic_write(record, json.dumps(previous).encode())
        print(json.dumps({'stale': stale, 'last_scan_ok': last, 'storage_error': storage_error}))
        return 1 if unhealthy else 0


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='POP3 → Gmail, retaining source mail')
    parser.add_argument('--config', type=Path, default=Path('~/.config/email2gmail/config.toml').expanduser())
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('once', help='Run one bounded sync and due notifications')
    run.add_argument('--max-messages', type=int)
    run.add_argument('--notify', action='store_true', help='Send an immediate operator summary')
    sub.add_parser('status', help='Local status only; no network access')
    sub.add_parser('health', help='Read-only state and user-timer checks for monitoring')
    ignoring = sub.add_parser('ignore', help='Acknowledge one reviewed blocked message; keep source and spool')
    ignoring.add_argument('ref', help='16-character opaque ref from status output')
    sub.add_parser('watchdog', help='Independent stale-run check and Telegram alert')
    cleaning = sub.add_parser('cleanup', help='Preview verified source-deletion candidates; --apply explicitly deletes')
    cleaning.add_argument('--apply', action='store_true')
    sub.add_parser('probe', help='Read-only POP/Gmail checks; never imports or sends')
    args = parser.parse_args()
    try:
        cfg = Config.load(args.config)
        if args.command == 'cleanup':
            from .cleanup import cleanup
            state = State(cfg)
            try:
                with lock(cfg.state/'sync.lock'):
                    if args.apply:
                        state.set('last_cleanup_attempt', time.time())
                    try:
                        result = cleanup(cfg, state, apply=args.apply)
                        if args.apply:
                            state.set('last_cleanup_result', result)
                            state.set('cleanup_error', None)
                        print(json.dumps(result, ensure_ascii=False))
                    except Exception as exc:
                        if args.apply:
                            state.set('cleanup_error', safe_error(exc))
                        raise
                return 0
            finally:
                state.close()
        if args.command == 'status':
            print(json.dumps(status(cfg), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'health':
            result = health(cfg)
            print(json.dumps(result))
            return 0 if result['healthy'] else 1
        if args.command == 'ignore':
            print(json.dumps(ignore_message(cfg, args.ref)))
            return 0
        if args.command == 'watchdog':
            return watchdog(cfg)
        if args.command == 'probe':
            Gmail(cfg).verify_account()
            source = PopSource(cfg)
            try:
                listing = source.listing()
                print(json.dumps({'gmail': cfg.gmail_account, 'pop_auth': 'ok', 'messages': len(listing),
                                  'bytes': sum(x[2] for x in listing)}))
            finally:
                source.close()
            return 0
        if args.max_messages is not None and args.max_messages < 1:
            raise BridgeError('invalid_max_messages')
        return once(cfg, args.max_messages, args.notify)
    except Exception as exc:
        print(json.dumps({'error': safe_error(exc)}))
        return 0 if isinstance(exc, BridgeError) and str(exc) == 'already_running' else 1


if __name__ == '__main__':
    raise SystemExit(main())
