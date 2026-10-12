from __future__ import annotations

import base64
import contextlib
import dataclasses
import email.policy
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import poplib
import re
import shlex
import sqlite3
import ssl
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request


class BridgeError(Exception):
    """Only static, non-sensitive codes belong in this exception."""


class GmailError(BridgeError):
    def __init__(self, code: str, rejected: bool = False):
        super().__init__(code)
        self.rejected = rejected


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_secrets(path: Path, keys: set[str]) -> dict[str, str]:
    """Read selected keys only, without evaluating shell or expanding values.

    Supports KEY=value and KEY: value lines.
    Unquoted # is literal unless preceded by whitespace.
    """
    result = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        m = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$', line)
        if not m or m[1] not in keys:
            continue
        value = m[2].strip()
        if value.startswith(('"', "'")):
            parts = shlex.split(value, comments=True)
            if len(parts) != 1:
                raise BridgeError('invalid_secret_format')
            value = parts[0]
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
        result[m[1]] = value
    for key in keys:
        if os.environ.get(key):
            result[key] = os.environ[key]
    if any(not result.get(k) for k in keys):
        raise BridgeError('missing_credentials')
    return result


POP_USER_KEY = 'EMAIL2GMAIL_POP_USER'
POP_PASSWORD_KEY = 'EMAIL2GMAIL_POP_PASSWORD'


@dataclasses.dataclass
class Config:
    gmail_account: str
    pop_account: str
    pop_host: str
    telegram_chat_id: str
    pop_port: int = 995
    credentials_file: str = '~/.config/email2gmail/secrets.env'
    telegram_file: str = '~/.config/email2gmail/secrets.env'
    state_dir: str = '~/.local/state/email2gmail/runtime'
    gws_bin: str = 'gws'
    gmail_label: str = 'Email2Gmail'
    initial_days: int = 30  # -1 = all, 0 = only future UIDLs
    max_messages: int = 25
    max_message_bytes: int = 150_000_000
    network_timeout: int = 30
    gmail_timeout: int = 120
    summary_seconds: int = 1800
    heartbeat_seconds: int = 21600
    stale_seconds: int = 900
    source_cleanup_enabled: bool = False
    source_retention_days: int = 7
    cleanup_interval_seconds: int = 3600

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            cfg = cls(**tomllib.loads(path.read_text()))
            if cfg.initial_days < -1 or cfg.max_messages < 1 or cfg.network_timeout < 1 or cfg.source_retention_days < 0:
                raise ValueError()
            if type(cfg.source_cleanup_enabled) is not bool or type(cfg.source_retention_days) is not int or cfg.cleanup_interval_seconds < 60:
                raise ValueError()
            # Numeric chat ID as a string: rejects unquoted TOML integers and @names.
            if type(cfg.telegram_chat_id) is not str or not re.fullmatch(r'-?[0-9]+', cfg.telegram_chat_id):
                raise ValueError()
            if not cfg.pop_host or type(cfg.pop_host) is not str:
                raise ValueError()
            return cfg
        except (TypeError, ValueError):
            raise BridgeError('invalid_config') from None

    @property
    def state(self) -> Path:
        return Path(self.state_dir).expanduser()

    @property
    def identity(self) -> str:
        return digest(json.dumps([self.pop_host, self.pop_port, self.pop_account.lower(),
                                  self.gmail_account.lower()]).encode())


def atomic_write(path: Path, data: bytes):
    fd, name = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        d = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


@contextlib.contextmanager
def lock(path: Path):
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError('already_running') from None
        yield


class State:
    def __init__(self, cfg: Config, readonly=False):
        if readonly:
            self.root = cfg.state
            self.db = sqlite3.connect((cfg.state/'state.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
            self.db.row_factory = sqlite3.Row
            if self.get('identity') != cfg.identity:
                self.db.close()
                raise BridgeError('state_account_mismatch')
            return
        cfg.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(cfg.state, 0o700)
        (cfg.state / 'spool').mkdir(exist_ok=True, mode=0o700)
        self.root = cfg.state
        self.db = sqlite3.connect(cfg.state / 'state.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS messages(
            uid TEXT PRIMARY KEY, size INTEGER NOT NULL, initial INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new', sha TEXT, fingerprint TEXT, mid TEXT,
            marker TEXT, gmail_id TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            next_try REAL NOT NULL DEFAULT 0, delivered_at REAL, present INTEGER NOT NULL DEFAULT 1
          );
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}
        for name, kind in [('cleanup_state', 'TEXT'), ('source_deleted_at', 'REAL'),
                           ('cleanup_issue', 'TEXT'), ('cleanup_next_try', 'REAL NOT NULL DEFAULT 0'), ('source_issue', 'TEXT')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE messages ADD COLUMN {name} {kind}')
        self.db.commit()
        old = self.get('identity')
        if old and old != cfg.identity:
            self.db.close()
            raise BridgeError('state_account_mismatch')
        self.set('identity', cfg.identity)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))

    def update(self, uid, **values):
        with self.db:
            self.db.execute('UPDATE messages SET '+', '.join(k+'=?' for k in values)+' WHERE uid=?',
                            (*values.values(), uid))

    def row(self, uid):
        return self.db.execute('SELECT * FROM messages WHERE uid=?', (uid,)).fetchone()

    def snapshot(self, listing, cfg, now):
        first = self.get('initial_cutoff') is None
        if first:
            # Transactionally seal the entire initial set before any deliveries.
            with self.db:
                self.db.execute('INSERT INTO meta VALUES(?,?)', ('initial_cutoff', json.dumps(now-cfg.initial_days*86400)))
                self.db.execute('INSERT INTO meta VALUES(?,?)', ('initial_days', json.dumps(cfg.initial_days)))
                self.db.executemany('INSERT INTO messages(uid,size,initial,status) VALUES(?,?,1,?)',
                                    [(u, z, 'skipped' if cfg.initial_days == 0 else 'new') for _, u, z in listing])
        else:
            with self.db:
                for _, uid, size in listing:
                    row = self.row(uid)
                    if row is None:
                        self.db.execute('INSERT INTO messages(uid,size,initial) VALUES(?,?,0)', (uid, size))
                    elif row['size'] != size or not row['present']:
                        # Never silently discard a reused UIDL. Human recovery retains prior evidence.
                        self.db.execute(
                            "UPDATE messages SET source_issue='uidl_reappeared_or_changed', "
                            "status=CASE WHEN status IN ('new','skipped') OR (status='ignored' AND size!=?) "
                            "THEN 'blocked' ELSE status END, size=? WHERE uid=?", (size, size, uid))
                self.db.execute('UPDATE messages SET present=0')
                self.db.executemany('UPDATE messages SET present=1 WHERE uid=?', [(u,) for _, u, _ in listing])
        self.set('source_count', len(listing))

    def spool(self, uid):
        return self.root / 'spool' / (digest(uid.encode())+'.eml')

    def counts(self):
        return dict(self.db.execute('SELECT status, count(*) FROM messages GROUP BY status'))

    def unresolved_count(self):
        return self.db.execute(
            "SELECT count(*) FROM messages WHERE status!='ignored' "
            'AND (error IS NOT NULL OR source_issue IS NOT NULL)').fetchone()[0]


class PopSource:
    def __init__(self, cfg):
        secrets = read_secrets(Path(cfg.credentials_file).expanduser(), {POP_USER_KEY, POP_PASSWORD_KEY})
        if secrets[POP_USER_KEY].lower() != cfg.pop_account.lower():
            raise BridgeError('pop_account_mismatch')
        self.max_bytes = cfg.max_message_bytes
        self.p = poplib.POP3_SSL(cfg.pop_host, cfg.pop_port, timeout=cfg.network_timeout,
                                 context=ssl.create_default_context())
        try:
            self.p.user(secrets[POP_USER_KEY])
            self.p.pass_(secrets[POP_PASSWORD_KEY])
        except Exception:
            self.p.close()
            raise BridgeError('pop_login_failed') from None

    def uid_at(self, number):
        return self.p.uidl(number).split()[2].decode('ascii')

    def delete(self, number):
        self.p.dele(number)

    def commit_deletions(self):
        self.p.quit()

    def abort_deletions(self):
        # Close without QUIT: the server must roll back uncommitted DELE marks.
        self.p.close()

    def close(self):
        try:
            self.p.quit()
        except Exception:
            self.p.close()

    def listing(self):
        sizes = {int(line.split()[0]): int(line.split()[1]) for line in self.p.list()[1]}
        result = []
        seen = set()
        for line in self.p.uidl()[1]:
            num, uid = line.split()
            uid = uid.decode('ascii')
            if uid in seen or int(num) not in sizes:
                raise BridgeError('inconsistent_pop_listing')
            seen.add(uid)
            result.append((int(num), uid, sizes[int(num)]))
        if len(result) != len(sizes):
            raise BridgeError('inconsistent_pop_listing')
        return result

    def retrieve(self, number):
        # poplib.retr caps each line at 2048 bytes, but real HTML mail exceeds that.
        # Bound the entire response instead, preserving POP dot unstuffing and CRLF.
        self.p._putcmd('RETR %s' % number)
        self.p._getresp()
        data = bytearray()
        while True:
            line = self.p.file.readline(self.max_bytes-len(data)+4)
            if not line:
                raise BridgeError('pop_unexpected_eof')
            if line in (b'.\r\n', b'.\n'):
                return bytes(data)
            if not line.endswith(b'\n'):
                raise BridgeError('message_too_large')
            line = line.removesuffix(b'\n').removesuffix(b'\r')
            if line.startswith(b'..'):
                line = line[1:]
            data.extend(line+b'\r\n')
            if len(data) > self.max_bytes:
                raise BridgeError('message_too_large')


def headers(raw):
    return BytesHeaderParser(policy=email.policy.default).parsebytes(raw)


def message_date(raw):
    try:
        d = parsedate_to_datetime(str(headers(raw).get('Date', '')))
        return d.timestamp() if d.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def message_id(raw):
    mids = headers(raw).get_all('Message-ID', [])
    if len(mids) == 1:
        m = re.fullmatch(r'\s*<([A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+)>\s*', str(mids[0]))
        if m:
            return m[1]
    return None


def fingerprint(raw):
    """Transport headers may change at Gmail; MIME/body bytes must match.

    CRLF canonicalization is solely for comparison, never for upload rewriting.
    """
    normalized = raw.replace(b'\r\n', b'\n')
    h = headers(raw)
    transport = {'received', 'return-path', 'delivered-to', 'authentication-results',
                 'received-spf', 'x-email2gmail-key', 'x-received', 'x-forwarded-encrypted'}
    try:
        items = list(h.items())
    except (OverflowError, ValueError):
        items = list(BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw).items())
    stable = sorted((k.lower(), str(v)) for k, v in items
                    if k.lower() not in transport and not k.lower().startswith(('x-google-', 'x-gm-')))
    body = normalized.partition(b'\n\n')[2]
    return digest(json.dumps(stable, ensure_ascii=False).encode() + b'\0' + body)


def prepare(raw, identity, uid):
    # Preserve all original bytes, adding only trace and (if absent/invalid) identity headers.
    marker = digest((identity+'\0'+uid+'\0'+digest(raw)).encode())
    mid = message_id(raw)
    prefix = f'X-Email2Gmail-Key: {marker}\r\n'.encode()
    if not mid:
        # Invalid/duplicate existing Message-ID headers stay untouched; reconciliation uses marker fallback scan.
        if not headers(raw).get_all('Message-ID'):
            mid = f'{digest(identity.encode()+raw)}@email2gmail.invalid'
            prefix += f'Message-ID: <{mid}>\r\n'.encode()
    return prefix+raw, mid, marker


class Gmail:
    def __init__(self, cfg):
        self.cfg = cfg
        self.deadline = time.monotonic()+240

    def time_left(self):
        return self.deadline-time.monotonic()

    def call(self, resource, method, params=None, body=None, upload=None):
        cmd = [self.cfg.gws_bin, 'gmail', 'users', *resource, method,
               '--params', json.dumps({'userId': self.cfg.gmail_account, **(params or {})})]
        if body is not None:
            cmd += ['--json', json.dumps(body)]
        if upload:
            cmd += ['--upload', str(upload), '--upload-content-type', 'message/rfc822']
        env = {k: v for k, v in os.environ.items() if k in
               {'HOME', 'PATH', 'LANG', 'LC_ALL', 'XDG_CONFIG_HOME', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'}}
        remaining = self.time_left()
        if remaining <= 0:
            raise GmailError('cycle_time_budget', rejected=True)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=min(self.cfg.gmail_timeout, remaining), env=env,
                               cwd=str(Path(upload).parent) if upload else None)
        except subprocess.TimeoutExpired:
            raise GmailError('gmail_timeout') from None
        except OSError:
            raise GmailError('gws_launch_failed', rejected=True) from None
        try:
            obj = json.loads(r.stdout)
        except (ValueError, UnicodeDecodeError):
            raise GmailError('gws_invalid_response') from None
        if r.returncode or 'error' in obj:
            code = (obj.get('error') or {}).get('code') if isinstance(obj, dict) else None
            # Explicit client/auth/quota rejection did not accept the message. 5xx is ambiguous.
            raise GmailError('gmail_http_'+str(code) if isinstance(code, int) else 'gws_failed',
                             rejected=code in (400, 401, 403, 404, 413, 429))
        return obj

    def verify_account(self):
        if self.call([], 'getProfile').get('emailAddress', '').lower() != self.cfg.gmail_account.lower():
            raise BridgeError('gmail_account_mismatch')

    def label(self):
        matches = [x['id'] for x in self.call(['labels'], 'list').get('labels', []) if x['name'] == self.cfg.gmail_label]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise BridgeError('ambiguous_gmail_label')
        return self.call(['labels'], 'create', body={'name': self.cfg.gmail_label,
                          'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'})['id']

    def current_labels(self, gid):
        return self.call(['messages'], 'get', {'id': gid, 'format': 'minimal'}).get('labelIds', [])

    def raw_with_labels(self, gid):
        obj = self.call(['messages'], 'get', {'id': gid, 'format': 'raw'})
        data = obj['raw']
        return base64.urlsafe_b64decode(data+'='*(-len(data)%4)), obj.get('labelIds', [])

    def raw(self, gid):
        return self.raw_with_labels(gid)[0]

    def find_match(self, row, label):
        # No searchable ID and no earlier attempt: no remote side effect to reconcile.
        if not row['mid'] and row['attempts'] == 0:
            return None
        params = {'maxResults': 25, 'includeSpamTrash': True}
        if row['mid']:
            params['q'] = 'rfc822msgid:'+row['mid']
        else:
            params['labelIds'] = [label]
        data = self.call(['messages'], 'list', params)
        for item in data.get('messages', []):
            if self.time_left() < 5:
                raise BridgeError('gmail_search_limit')
            if not row['mid']:
                metadata = self.call(['messages'], 'get', {'id': item['id'], 'format': 'metadata',
                                     'metadataHeaders': ['X-Email2Gmail-Key']})
                markers = [h['value'] for h in metadata.get('payload', {}).get('headers', [])
                           if h.get('name', '').lower() == 'x-email2gmail-key']
                if row['marker'] not in markers:
                    continue
            raw = self.raw(item['id'])
            marker_match = row['marker'] in [str(v) for v in headers(raw).get_all('X-Email2Gmail-Key', [])]
            if (marker_match or row['mid']) and fingerprint(raw) == row['fingerprint']:
                return item['id']
        if data.get('nextPageToken'):
            # Keep payload and surface operator action; never loop over an unbounded mailbox.
            raise BridgeError('gmail_search_limit')
        return None

    def add_label(self, gid, label):
        self.call(['messages'], 'modify', {'id': gid}, {'addLabelIds': [label]})

    def import_mail(self, path, label, dated):
        result = self.call(['messages'], 'import',
                           {'internalDateSource': 'dateHeader' if dated else 'receivedTime',
                            'neverMarkSpam': False, 'processForCalendar': False, 'deleted': False},
                           {'labelIds': ['INBOX', 'UNREAD', label]}, upload=path)
        if not result.get('id'):
            raise GmailError('gmail_missing_import_id')
        return result['id']


class Telegram:
    def __init__(self, cfg):
        self.cfg = cfg

    def send(self, text):
        token = read_secrets(Path(self.cfg.telegram_file).expanduser(), {'EMAIL2GMAIL_TELEGRAM_TOKEN'})['EMAIL2GMAIL_TELEGRAM_TOKEN']
        payload = json.dumps({'chat_id': self.cfg.telegram_chat_id, 'text': text[:4000],
                              'link_preview_options': {'is_disabled': True}}).encode()
        req = urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage', payload,
                                     {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.network_timeout) as r:
                result = json.load(r)
            if not result.get('ok'):
                raise BridgeError('telegram_api_failed')
            return result['result']['message_id']
        except urllib.error.HTTPError as e:
            raise BridgeError('telegram_http_'+str(e.code)) from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise BridgeError('telegram_connection_failed') from None


def safe_error(exc):
    return str(exc) if isinstance(exc, BridgeError) else type(exc).__name__


def notify(cfg, state, telegram, now, force=False):
    counts = state.counts()
    last_notice = state.get('last_notice', 0)
    pending = sum(v for k, v in counts.items() if k in ('new', 'prepared', 'importing', 'blocked'))
    last_summary = state.get('last_summary', 0)
    delivered = state.db.execute('SELECT count(*) FROM messages WHERE delivered_at>?', (last_summary,)).fetchone()[0]
    error = state.get('last_error') or (state.get('cleanup_error') if cfg.source_cleanup_enabled else None)
    item_errors = state.unresolved_count()
    cleanup_result = state.get('last_cleanup_result', {}) if cfg.source_cleanup_enabled else {}
    cleanup_issues = dict(state.db.execute("SELECT cleanup_issue,count(*) FROM messages WHERE present=1 AND cleanup_issue IS NOT NULL GROUP BY cleanup_issue")) if cfg.source_cleanup_enabled else {}
    cleanup_pending = state.db.execute("SELECT count(*) FROM messages WHERE cleanup_state='pending'").fetchone()[0] if cfg.source_cleanup_enabled else 0
    alert = bool(error or cleanup_result.get('mismatch') or any(k != 'spam_trash' for k in cleanup_issues) or cleanup_pending or item_errors or counts.get('blocked') or counts.get('importing'))
    due = force or (alert and now-state.get('last_alert', 0) >= 3600) or (
        delivered and now-last_summary >= cfg.summary_seconds) or now-last_notice >= cfg.heartbeat_seconds
    if not due:
        return
    last_ok = state.get('last_scan_ok')
    stamp = time.strftime('%Y-%m-%d %H:%M:%S KST', time.gmtime(last_ok+9*3600)) if last_ok else '아직 없음'
    kind = '확인 필요' if alert else '정상 동작'
    text = (f'Email2Gmail · {kind}\n'
            f'최근 처리 {delivered}건 / 누적 가져옴 {counts.get("imported", 0)}건\n'
            f'기존 Gmail 확인 {counts.get("existing", 0)}건 / 대기 {pending}건\n'
            f'서버 보관 {state.get("source_count", 0)}건 · 마지막 점검 {stamp}\n'
            '메일 확인 2분 · 요약 30분 · heartbeat 6시간')
    deleted = state.db.execute('SELECT count(*) FROM messages WHERE source_deleted_at IS NOT NULL').fetchone()[0]
    text += f'\n서버 정리 누적 {deleted}건 · 자동 삭제 '+('켜짐' if cfg.source_cleanup_enabled else '꺼짐')
    if cleanup_result.get('held_spam_trash') or cleanup_result.get('mismatch') or cleanup_pending:
        text += (f'\n서버 정리 보류: 스팸/휴지통 {cleanup_result.get("held_spam_trash", 0)}건 · '
                 f'검증 불일치 {cleanup_result.get("mismatch", 0)}건 · 삭제 결과 미확정 {cleanup_pending}건')
    if cleanup_issues.get('gmail_missing'):
        text += f'\n서버 원본 보존: Gmail 사본 없음 {cleanup_issues["gmail_missing"]}건'
    if cleanup_issues.get('gmail_unavailable'):
        text += f'\nGmail 확인 재시도 대기 {cleanup_issues["gmail_unavailable"]}건'
    if error:
        text += '\n오류: '+error
    if counts.get('importing'):
        text += '\n전송 결과 미확정: 중복 방지를 위해 재전송을 보류하고 Gmail에서 확인 중'
    if counts.get('blocked') or item_errors:
        text += '\n개별 메일 오류: 로컬 status에서 확인 필요'
    mid = telegram.send(text)
    state.set('last_notice', now)
    state.set('last_summary', now)
    state.set('last_telegram_id', mid)
    state.set('telegram_error', None)
    if alert:
        state.set('last_alert', now)


def deliver(cfg, state, gmail, row, label, now):
    uid = row['uid']
    if row['status'] == 'importing':
        gid = gmail.find_match(row, label)
        if gid:
            state.update(uid, status='imported', gmail_id=gid, error=None, delivered_at=now)
            state.spool(uid).unlink(missing_ok=True)
        else:
            state.update(uid, next_try=now+120)
        return
    if row['status'] != 'prepared' or row['next_try'] > now:
        return
    path = state.spool(uid)
    raw = path.read_bytes()
    if digest(raw) != row['sha']:
        raise BridgeError('spool_hash_mismatch')
    match = gmail.find_match(row, label)
    if match:
        gmail.add_label(match, label)
        state.update(uid, status='existing', gmail_id=match, error=None)
        path.unlink(missing_ok=True)
        return
    state.update(uid, status='importing', attempts=row['attempts']+1, error=None, next_try=now+120)
    try:
        gid = gmail.import_mail(path, label, message_date(raw) is not None)
    except GmailError as e:
        if e.rejected:
            permanent = str(e) in ('gmail_http_400', 'gmail_http_413')
            delay = min(3600, 120*2**min(row['attempts'], 5))
            state.update(uid, status='blocked' if permanent else 'prepared', error=str(e), next_try=now+delay)
        else:
            state.update(uid, error=str(e))
        return
    state.update(uid, status='imported', gmail_id=gid, error=None, delivered_at=now)
    path.unlink(missing_ok=True)


def recover(cfg, state, gmail, now, max_messages=None):
    gmail.verify_account()
    label = gmail.label()
    budget = max_messages or cfg.max_messages
    waiting = state.db.execute("SELECT * FROM messages WHERE status IN ('importing','prepared') AND next_try<=? ORDER BY next_try, rowid LIMIT ?", (now, budget)).fetchall()
    for row in waiting:
        if hasattr(gmail, 'time_left') and gmail.time_left() < 5:
            break
        # Fairness survives a hard service timeout during a remote call.
        state.update(row['uid'], next_try=now+120)
        try:
            deliver(cfg, state, gmail, row, label, now)
        except Exception as e:
            values = {'error': safe_error(e), 'next_try': now+120}
            if str(e) == 'gmail_search_limit':
                values['status'] = 'blocked'
            state.update(row['uid'], **values)
    return label


def sync(cfg, state, gmail, source, now, max_messages=None, label=None):
    if label is None:
        label = recover(cfg, state, gmail, now, max_messages)
    listing = source.listing()
    state.snapshot(listing, cfg, now)
    budget = max_messages or cfg.max_messages
    # A separate quota prevents uncertain imports from starving newly arrived mail.
    fetched = 0
    # Unattempted mail (next_try=0) precedes failed downloads; oldest retry rotates first.
    for number, uid, size in sorted(listing, key=lambda item: (state.row(item[1])['next_try'], -item[0])):
        row = state.row(uid)
        if row['status'] != 'new' or row['next_try'] > now:
            continue
        if fetched >= budget or (hasattr(gmail, 'time_left') and gmail.time_left() < 5):
            break
        fetched += 1
        state.update(uid, next_try=now+120)
        try:
            if size > cfg.max_message_bytes:
                raise BridgeError('message_too_large')
            try:
                original = source.retrieve(number)
            except Exception as exc:
                # A partial multi-line response leaves the POP stream desynchronized.
                state.update(uid, status='blocked' if str(exc) == 'message_too_large' else 'new',
                             error=safe_error(exc), next_try=now+120)
                break
            if len(original) > cfg.max_message_bytes:
                raise BridgeError('message_too_large')
            date = message_date(original)
            days = state.get('initial_days')
            if row['initial'] and days > 0 and date is not None and date < state.get('initial_cutoff'):
                state.update(uid, status='skipped')
                continue
            # Missing dates are included, never silently discarded.
            payload, mid, marker = prepare(original, cfg.identity, uid)
            atomic_write(state.spool(uid), payload)
            state.update(uid, status='prepared', next_try=0, sha=digest(payload), fingerprint=fingerprint(payload), mid=mid, marker=marker)
            deliver(cfg, state, gmail, state.row(uid), label, now)
        except Exception as e:
            current = state.row(uid)
            if str(e) in ('message_too_large', 'gmail_search_limit'):
                state.update(uid, status='blocked', error=str(e))
            else:
                state.update(uid, error=safe_error(e), next_try=now+120)
    missing = state.db.execute("SELECT uid FROM messages WHERE present=0 AND status='new'").fetchall()
    for row in missing:
        state.update(row['uid'], status='blocked', error='source_removed_before_download')
    state.set('last_scan_ok', now)
    state.set('last_error', None)
