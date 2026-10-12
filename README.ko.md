# pop3-to-gmail

POP3 메일함의 메일을 Gmail로 복사하고 Telegram으로 처리 건수와 상태를 알립니다. Python 패키지와 명령 이름은 `email2gmail`입니다. Gmail의 "다른 계정의 메일 확인" 기능이 [종료되는 것](https://support.google.com/mail/answer/16604719)을 대신하기 위해 직접 운영하는 도구입니다. Python 3.11 이상과 로그인된 `gws` CLI가 필요하며 Python 외부 패키지는 없습니다.

English: [README.md](README.md)

개인 프로젝트이며 작성자가 자기 메일함에 쓰고 있습니다. 있는 그대로 공개하며 지원을 보장하지 않습니다.

## 기본 동작

- 수집: 2분마다, 한 번에 신규 최대 25건 + 재시도 최대 25건
- Telegram: 처리 내역이 있으면 30분 간격 요약, 6시간 간격 heartbeat, 오류는 시간당 최대 1회
- 별도 watchdog: 5분마다 점검, 마지막 수집 성공이 15분 이상 지연되면 알림
- 처음에는 최근 30일분, 이후에는 새 UIDL을 날짜와 무관하게 수집
- 원본 서버의 메일 보존이 기본값입니다. 검증 후 서버 정리는 별도 옵션으로 활성화합니다.
- 첨부·본문·원본 헤더를 보존하고 추적용 헤더만 앞에 추가합니다. Message-ID가 없으면 결정적인 ID를 추가합니다. 스팸 판정은 Gmail에 맡기고 캘린더 초대 자동 등록은 비활성화합니다.
- Telegram에는 메일 제목·본문·첨부·자격 증명 없이 건수와 상태만 보냅니다.

## 필요한 것

- systemd 사용자 unit을 쓸 수 있는 Linux
- Python 3.11 이상
- [`gws` CLI](https://www.npmjs.com/package/@googleworkspace/cli)(`@googleworkspace/cli`). Google이 공식 지원하는 제품은 아니라고 패키지에 명시되어 있습니다.
- TLS로 접속할 수 있는 POP3 서버(기본 포트 995)
- Telegram 봇 토큰과 알림을 받을 채팅의 숫자 ID

## 설치와 설정

1. `gws` CLI를 설치하고 OAuth client를 준비합니다. Google Cloud 프로젝트가 필요하며 [gws 인증 안내](https://github.com/googleworkspace/cli#authentication)를 따릅니다. 그다음 `gws auth login`으로 Gmail `gmail.modify` 권한을 가진 계정에 로그인합니다.
2. 이 저장소를 `~/email2gmail`에 받습니다. 다른 경로를 쓰면 service unit 두 개의 `WorkingDirectory`를 고칩니다.
3. `mkdir -p ~/.config/email2gmail`로 설정 디렉터리를 만들고 `config.example.toml`을 `~/.config/email2gmail/config.toml`에 복사해 계정, POP3 호스트, Telegram 채팅 ID, gws 절대 경로를 설정합니다. 실제 설정은 저장소 밖에 두고 파일 권한을 600으로 제한합니다.
4. 설정의 `credentials_file`(기본 `~/.config/email2gmail/secrets.env`)에 아래 세 값을 둡니다. 파일 권한은 600으로 제한합니다.

   ```
   EMAIL2GMAIL_POP_USER=you@example.com
   EMAIL2GMAIL_POP_PASSWORD=...
   EMAIL2GMAIL_TELEGRAM_TOKEN=...
   ```

   `EMAIL2GMAIL_POP_USER`는 설정의 `pop_account`와 같아야 합니다. `KEY=value`, `KEY: value` 형식을 모두 읽으며 셸을 실행하거나 변수 치환을 하지 않습니다. 같은 이름의 환경변수가 있으면 파일 값보다 우선합니다. Telegram 토큰을 다른 파일에 두려면 `telegram_file`을 따로 지정합니다.
5. Telegram 앱에서 봇과 대화를 열고 Start를 누릅니다. 알림은 설정의 `telegram_chat_id` 한 곳으로만 보냅니다.
6. 아래 명령으로 읽기 전용 점검 후 실행합니다.

```bash
cd ~/email2gmail
python3 -m email2gmail.cli probe
python3 -m email2gmail.cli once --max-messages 1
python3 -m email2gmail.cli once
python3 -m email2gmail.cli status
python3 -m email2gmail.cli health
```

`--config /absolute/path/config.toml`은 하위 명령 앞에 지정합니다. `initial_days=-1`은 초기 전체, `0`은 초기 목록을 건너뛰고 이후 수신분만 뜻합니다. 초기 범위는 첫 목록과 함께 DB에 고정하므로 나중에 이 값을 바꿔도 과거 제외 메일을 자동 재수집하지 않습니다.

자동 실행 설치:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/email2gmail* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now email2gmail.timer email2gmail-watchdog.timer
loginctl enable-linger "$USER"
```

unit은 `gws`가 자신의 `PATH`에 있고 기본 `state_dir`와 `~/.config/gws`에만 쓴다고 가정합니다. 다르면 unit의 `PATH`와 `ReadWritePaths`를 수정합니다. `linger`를 켜면 로그아웃 후에도 사용자 서비스가 실행되고 재부팅 시 시작됩니다. 호스트 전원이 켜져 있고 네트워크가 연결되어 있어야 수집과 알림이 가능합니다. 호스트 전체 중단을 직접 통지하려면 별도 머신의 외부 감시가 필요합니다.

## 운영 명령

```bash
python3 -m email2gmail.cli status
python3 -m email2gmail.cli ignore <status에 표시된 ref>
systemctl --user list-timers 'email2gmail*'
journalctl --user -u email2gmail.service -n 30 --no-pager
python3 -m email2gmail.cli once --notify
systemctl --user stop email2gmail.timer email2gmail-watchdog.timer
```

타이머를 꺼도 이미 실행 중인 수집은 종료될 때까지 이어집니다. 서비스 강제 중단은 업로드 결과가 미확정인 상태를 만들 수 있으므로 정상 완료 후 중지하는 편이 좋습니다.

`status`에서 `blocked`로 표시된 메일을 운영자가 별도로 확인해 불필요한 메일로 판단했다면 `ignore`에 16자리 `ref`를 지정합니다. `blocked`는 원래 자동 재시도되지 않으며, 이 명령은 운영자 판단을 `ignored` 종료 상태로 기록해 해당 항목을 활성 오류 경고에서 제외합니다. 기존 오류 코드와 `ref`, 원본 서버의 메일, 로컬 spool은 감사·복구용으로 보존하며 Gmail 전송이나 외부 알림도 수행하지 않습니다. 전송 결과 재확인이 끝나지 않은 `gmail_search_limit`, `blocked`가 아닌 항목, 존재하지 않거나 잘못된 `ref`, 동시에 실행 중인 수집은 변경하지 않습니다.

### 모니터링 연동

`health` 명령은 상태 DB를 읽기 전용으로 확인하고 두 사용자 타이머와 watchdog 실패 여부를 점검합니다. 전체 정상은 종료 코드 0, 이상은 1을 반환하며 JSON으로 `healthy`, `liveness_issues`, `queue_issues`, `scan_age_seconds`를 출력합니다. 메일 수집·삭제·Telegram 전송은 실행하지 않습니다.

- `liveness_issues`: 수집 성공이 `stale_seconds`(기본 15분) 이상 지연되거나, 타이머가 멈추거나, watchdog·수집·알림이 실패한 경우
- `queue_issues`: 미해결 개별 메일, 전송 결과 미확정, 서버 정리 오류

스팸/휴지통 때문에 원본을 정상 보존 중인 항목은 오류로 보지 않지만, Gmail 사본이 실제로 사라지면 확인이 필요한 상태로 표시합니다. 출력에는 고정 오류 코드와 건수만 있고 계정·메일 식별자·본문·API 오류 원문은 없습니다. 종료 코드나 두 목록을 쓰는 모니터에 연결하면 됩니다. 기존 메일 오류가 계속 남아 있어도 새 수집 중단을 놓치지 않으려면 두 목록을 서로 다른 감시 항목으로 두는 편이 좋습니다.

## 중복 방지와 복구

SQLite 상태는 `state_dir`(기본 `~/.local/state/email2gmail/runtime`)의 `state.sqlite3`에 있습니다. 미완료 메일과 운영자가 `ignored`로 보존한 메일만 같은 디렉터리의 private `spool/`에 남기고 Gmail 성공 후 삭제합니다. 상태 DB는 삭제하거나 다른 계정과 공유하지 마세요. Gmail/POP 계정 identity가 달라지면 실행을 거부합니다. 계정 비밀번호나 Telegram 토큰 변경은 자격 증명 파일 갱신 후 다음 실행·알림에 반영됩니다. Gmail 인증 갱신은 같은 대상 계정으로 `gws auth login`을 사용합니다.

- `imported`: Gmail ID가 확인된 복사본
- `existing`: 같은 Message-ID와 내용이 이미 Gmail에 있어 라벨만 추가
- `new`: 아직 다운로드하지 않은 메일
- `prepared`: 로컬 보존 완료, 전송 대기 또는 명시적으로 거절된 요청의 재시도
- `importing`: 요청을 보낸 뒤 성공 여부가 미확정. 재시작 시에도 Gmail에서 먼저 확인
- `blocked`: 큰 메일, UIDL 변경/재등장, 다운로드 전 원본 삭제 등 수동 확인이 필요한 항목
- `ignored`: 운영자가 확인해 가져오지 않기로 한 메일. 원본과 로컬 spool은 보존하며 활성 오류 경고에서 제외
- `skipped`: 초기 범위 밖

Gmail import에는 문서화된 idempotency key가 없습니다. 응답 유실·timeout·5xx에서는 자동 재전송을 보류하고 Message-ID 검색, 추적 헤더와 본문/헤더 fingerprint로 확인합니다. 검색 결과가 없어도 실패로 단정하지 않습니다. 오래 남은 `importing`은 관리자 확인이 필요하며 프로그램은 다른 메일을 계속 처리합니다. DB를 지우거나 상태를 수동 초기화하면 중복 위험이 생깁니다.

POP UIDL의 지속성을 전제로 하되 크기가 달라지거나 삭제 후 재등장하면 source_issue 경보를 남깁니다. 아직 내려받지 않은 항목은 보류하지만, 이미 검증해 spool에 보존한 prepared/importing 사본은 전송·재확인을 계속합니다. 점검 사이에 같은 UIDL과 같은 크기의 내용이 교체되면 탐지하지 못할 수 있습니다. POP가 노출하지 않는 보낸편지함·다른 폴더 및 서버에서 이미 삭제된 과거 메일은 가져올 수 없습니다. Gmail에서 읽음/삭제 상태를 바꿔도 원본 서버에 역동기화하지 않습니다. 원본 주소를 발신자로 한 답장 설정(SMTP/send-as)은 이 수신 프로그램의 범위에 포함되지 않습니다.

## Gmail 확인 후 원본 서버 정리 (기본 비활성)

`source_cleanup_enabled=false`가 기본값입니다. 다음 명령은 삭제 후보 건수만 확인합니다.

```bash
python3 -m email2gmail.cli cleanup
```

설정 파일의 `source_cleanup_enabled=true`로 바꾸면 1시간마다 보관 기간이 지난 원본을 정리합니다 (`cleanup_interval_seconds=3600`). `source_retention_days=7`이면 **Gmail 복사 성공 시점부터 7일** 보관하며, `0`은 검증 후 즉시 정리합니다. `cleanup --apply`는 자동 실행 설정과 별개로 한 번의 실제 삭제를 명시 실행하는 운영자 명령입니다.

삭제 조건은 모두 만족해야 합니다: import 성공과 Gmail ID 보존, 보관 기간 경과, 같은 POP 세션의 UIDL 일치, 현재 서버 원본·Gmail 본문/첨부·의미 있는 헤더 fingerprint 일치, 추적 헤더 일치, Gmail SPAM/TRASH 제외. Gmail의 스팸함/휴지통은 자동 삭제될 수 있으므로 원본을 계속 보관합니다. Gmail에서 정상 보관함으로 옮긴 뒤 다음 실행에 다시 판단합니다. 각 DELE 예약 직전에 Gmail 폴더를 재확인합니다. 실제 확정은 마지막 QUIT 시점이므로 그 사이 다른 사용자가 Gmail 사본을 변경하는 경쟁 상태까지 원자적으로 막을 수는 없습니다.

삭제 intent를 DB에 먼저 기록하고 POP `DELE`를 예약한 뒤 `QUIT`의 성공 응답으로 확정합니다. 중간 실패/중단은 QUIT 없이 연결을 닫습니다. QUIT 응답을 잃으면 성공을 단정하지 않고 다음 새 목록에서 원본 부재를 확인합니다. 목록에 여전히 있으면 같은 검증 절차를 다시 거칩니다. Telegram 요약에 서버 정리 누적 건수와 자동 삭제 설정이 표시됩니다.

Gmail 사본이 영구삭제되어 404가 나면 해당 원본만 보존하고 다른 메일 정리는 계속합니다. 이 경우 하루 뒤 재확인하며 Telegram에 사본 없음 건수를 표시합니다. 일시 Gmail 오류·스팸/휴지통 보류는 1시간 뒤, 내용 불일치는 하루 뒤 재확인합니다. 보류 메일도 한 번의 최대 점검 건수에 포함하고 스팸·휴지통은 본문을 받기 전에 라벨로 걸러냅니다. 초기 범위 밖 `skipped`와 원래 Gmail에 있던 `existing` 메일은 자동 정리 대상에 포함하지 않습니다.

형식이 비정상인 Message-ID는 첫 전송 전에 Gmail 전체 라벨을 스캔하지 않습니다. 응답 유실 재확인 시에는 최대 25건의 메타데이터에서 추적 헤더를 먼저 확인하며, 범위가 너무 넓으면 `gmail_search_limit`로 보류하고 원본 spool을 유지합니다. Gmail 호출은 실행 객체별 총 4분 예산 안에서 수행하며, 개별 요청 timeout도 남은 예산으로 제한됩니다. 이런 보류 항목은 `status`의 비식별 `ref`를 기준으로 원인과 Gmail 실제 수신 여부를 확인해야 하며, DB 초기화나 무조건 재전송으로 우회하지 않습니다.

watchdog은 상태 DB를 읽기 전용으로 열고 알림을 먼저 보냅니다. DB가 손상되거나 읽을 수 없어도 Telegram으로 오류를 알리며, 알림 간격 기록과 lock은 메일 상태 디스크와 분리된 `/run/user/<uid>/email2gmail-watchdog`에 둡니다. 네트워크·Telegram 자체 장애 또는 호스트 전체 정지는 알림을 전달할 수 없으므로 외부 heartbeat로 확인해야 합니다.

## 알려진 한계

- 알림 문구는 한국어이고 시각은 KST로 표시합니다.
- Telegram 요약의 주기 안내 줄은 기본 타이머 기준의 고정 문구입니다.
- POP 로그인 이름이 `pop_account`와 같아야 합니다.
- 설정 하나가 POP 메일함 하나와 Gmail 계정 하나를 다룹니다.

## 다른 방법

- 메일 서비스가 **서버 자동 전달 + 서버 원본 보관**을 제공한다면 별도 수집 호스트가 필요 없어 운영 부담이 줄어듭니다.
- [fetch2gmail](https://github.com/threehappypenguins/fetch2gmail): IMAP에서 Gmail API로 가져오며 OAuth를 직접 설정합니다.
- [mailpopbox-router](https://github.com/rsesek/mailpopbox): mailpopbox 서버의 일부로 POP3에서 Gmail로 옮깁니다.

Gmail의 기존 외부 POP 수집 기능은 신규 이용이 종료되었고 기존 이용도 2027년 1월까지라고 안내되어 있어 새 시스템의 기반으로 사용하지 않았습니다.

## 검증

```bash
python3 -m unittest discover -s tests -v
systemd-analyze --user verify systemd/email2gmail.service systemd/email2gmail.timer systemd/email2gmail-watchdog.service systemd/email2gmail-watchdog.timer
```

테스트는 가짜 POP/Gmail/Telegram으로 응답 유실, 프로세스 중단, 중복 ID, MIME/첨부 보존, 초기 범위, 큐 공정성, API 거절, Telegram 실패를 재현하며 실제 메일을 보내지 않습니다.

참고: [Gmail POP 지원 변경](https://support.google.com/mail/answer/16604719), [Gmail import API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/import), [미디어 업로드](https://developers.google.com/workspace/gmail/api/guides/uploads).

## 라이선스

[MIT](LICENSE)
