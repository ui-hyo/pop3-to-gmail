# pop3-to-gmail

Copies mail from a POP3 mailbox into Gmail and reports counts and health to Telegram.
It is a self-hosted replacement for Gmail's "Check mail from other accounts" feature, which
[Google is retiring](https://support.google.com/mail/answer/16604719). The Python package and
command are named `email2gmail`.

한국어 문서: [README.ko.md](README.ko.md)

This is a personal project that the author runs for their own mailbox. It is shared as-is, with no
support guarantee.

## What it does

- Polls one POP3 mailbox over TLS and imports each new message into one Gmail account through the
  Gmail API (`users.messages.import`).
- Keeps the original message bytes, attachments and headers. It adds one trace header
  (`X-Email2Gmail-Key`) and, only when the message has no `Message-ID`, a deterministic one.
- Applies `INBOX`, `UNREAD` and a label of your choice. Spam classification is left to Gmail, and
  calendar invitations are not auto-processed.
- Leaves the source mailbox untouched by default. Deleting source mail after a verified copy is
  opt-in (see [Source cleanup](#source-cleanup)).
- Sends Telegram summaries that contain counts and status only: no subjects, bodies, addresses or
  credentials.

## Design choices

The Gmail import call has no documented idempotency key, so a lost response could otherwise lead to
a duplicate or a missing message. The tool is built around that problem:

- The message is written to a private local spool, and the intent to import is recorded in SQLite,
  before any Gmail call.
- After a timeout, a 5xx or a lost response, it does not resend. It first looks for the message in
  Gmail by `Message-ID`, the trace header and a body/header fingerprint.
- An explicit rejection (400, 401, 403, 404, 413, 429) is retried with backoff or marked `blocked`.
- Source and destination accounts are pinned in the state database. A different account pair is
  refused.
- A changed or reappearing POP UIDL is flagged for review, not silently dropped.

## Requirements

- Linux with systemd user units (the `health` command checks the timers through `systemctl`).
- Python 3.11 or later. There are no Python package dependencies.
- The [`gws` CLI](https://www.npmjs.com/package/@googleworkspace/cli) (`@googleworkspace/cli`),
  logged in to the destination Gmail account with the `gmail.modify` scope. The package describes
  itself as not an officially supported Google product.
- A POP3 server reachable over TLS (port 995 by default).
- A Telegram bot token and the numeric ID of the chat that receives notices.

## Setup

1. Install the `gws` CLI and set up its OAuth client. This needs a Google Cloud project; follow
   [gws authentication](https://github.com/googleworkspace/cli#authentication). Then log in to the
   destination Gmail account: `gws auth login`.
2. Clone this repository to `~/email2gmail`. If you use another path, edit `WorkingDirectory` in the
   two service units.
3. Create the config directory, copy the example, fill it in and restrict it:

   ```bash
   mkdir -p ~/.config/email2gmail
   cp config.example.toml ~/.config/email2gmail/config.toml
   chmod 600 ~/.config/email2gmail/config.toml
   ```
4. Create the secrets file named in the config (`~/.config/email2gmail/secrets.env` by default) with
   mode 600:

   ```
   EMAIL2GMAIL_POP_USER=you@example.com
   EMAIL2GMAIL_POP_PASSWORD=...
   EMAIL2GMAIL_TELEGRAM_TOKEN=...
   ```

   `EMAIL2GMAIL_POP_USER` must equal `pop_account` in the config. Lines may be `KEY=value` or
   `KEY: value`. Values are never evaluated by a shell. An environment variable with the same name
   overrides the file.
5. Open a chat with your bot in Telegram and press Start, so the bot is allowed to message you.
6. Check, then run:

   ```bash
   cd ~/email2gmail
   python3 -m email2gmail.cli probe                  # read-only POP and Gmail check
   python3 -m email2gmail.cli once --max-messages 1  # import one message
   python3 -m email2gmail.cli once
   python3 -m email2gmail.cli status
   ```

   `--config /absolute/path/config.toml` goes before the subcommand.
7. Install the timers:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp systemd/email2gmail* ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now email2gmail.timer email2gmail-watchdog.timer
   loginctl enable-linger "$USER"
   ```

   The units expect `gws` on their `PATH` and write only to the default `state_dir` and
   `~/.config/gws`. Edit `PATH` and `ReadWritePaths` in the units if yours differ. With linger enabled the timers keep running after logout and
   start at boot.

### Initial range

`initial_days = 30` imports the last 30 days on the first run. `-1` imports everything on the
server and `0` imports only mail that arrives later. The range is fixed in the state database on
the first listing, so changing the value afterwards does not re-import older mail.

## Commands

| Command | What it does |
|---|---|
| `probe` | Read-only check of POP login and the Gmail account. Never imports or sends. |
| `once [--max-messages N] [--notify]` | One bounded sync plus any due notification. `--notify` sends a summary now. |
| `status` | Local state as JSON. No network access. |
| `health` | Read-only verdict for monitoring. Exit code 0 when healthy, 1 otherwise. |
| `ignore <ref>` | Acknowledge one reviewed `blocked` message. Source mail and spool are kept. |
| `watchdog` | Independent stale-run check that alerts through Telegram. |
| `cleanup [--apply]` | Preview source-deletion candidates. `--apply` deletes them. |

With the shipped timers, mail is checked every 2 minutes (up to 25 new messages and 25 retries per
run) and the watchdog runs every 5 minutes. Telegram gets a summary at most every 30 minutes when
something was delivered, a heartbeat every 6 hours and an alert at most once an hour.

Stopping the timers does not interrupt a run that is already in progress. Killing the service
mid-upload can leave an import in the `importing` state, so let it finish first.

## Message states

| State | Meaning |
|---|---|
| `new` | Listed on the server, not downloaded yet. |
| `prepared` | Saved locally, waiting to be sent or retried after an explicit rejection. |
| `importing` | Request sent, outcome unknown. Checked against Gmail before anything is resent. |
| `imported` | Copy confirmed by a Gmail message ID. |
| `existing` | The same message was already in Gmail, so only the label was added. |
| `blocked` | Needs a person: too large, UIDL changed or reappeared, removed before download. |
| `ignored` | Reviewed by the operator and intentionally not imported. Evidence is kept. |
| `skipped` | Outside the initial range. |

State lives in `state.sqlite3` under `state_dir`. Do not delete it or share it between accounts:
resetting it creates a duplicate risk. Message contents, UIDLs and hashes never appear in `status`
output. Each problem message is shown by a 16-character `ref`.

## Monitoring

`health` prints JSON with `healthy`, `liveness_issues`, `queue_issues` and `scan_age_seconds`, and
reads the state database without writing to it. Liveness covers a stale or failing scan, stopped
timers and watchdog or notification failures. Queue issues cover unresolved messages and cleanup problems. Wire
the exit code or the two lists into whatever monitor you use. Only fixed codes and counts are
reported.

The watchdog and Telegram cannot report a host that is down. Use an external heartbeat for that.

## Source cleanup

`source_cleanup_enabled = false` is the default. `cleanup` without `--apply` only counts
candidates. With `source_cleanup_enabled = true`, the tool checks once an hour
(`cleanup_interval_seconds`) and deletes source mail that satisfies every one of these conditions:

- the import succeeded and its Gmail ID is recorded;
- `source_retention_days` have passed since the copy succeeded (`0` means right after verification);
- the POP UIDL still maps to the same message in the same session;
- the current source message, the Gmail copy and the recorded fingerprint all match, and the trace
  header is present;
- the Gmail copy is not in Spam or Trash, because Gmail can expire those folders.

The delete intent is written to the database first, POP `DELE` is staged, and only a successful
`QUIT` confirms it. If the `QUIT` reply is lost, nothing is assumed: the next listing shows whether
the message is gone. An interrupted run closes the connection without `QUIT`, so the server rolls
back the staged deletions.

## Limitations

- Notification text is Korean, and timestamps in it are shown in KST.
- The schedule line in the Telegram summary is fixed text that describes the shipped timers.
- The POP login name must equal `pop_account`.
- One config handles one POP mailbox and one Gmail account.
- POP3 exposes only the inbox. Sent mail, other folders and mail already deleted from the server
  cannot be imported.
- Nothing is synced back: reading or deleting in Gmail does not change the source mailbox.
- Sending as the source address is out of scope.
- If a message is replaced on the server by another one with the same UIDL and size between two
  checks, the change is not detected.

## Alternatives

- If your mail provider can forward mail and keep a copy on the server, use that. It needs no host.
- [fetch2gmail](https://github.com/threehappypenguins/fetch2gmail) imports from IMAP through the
  Gmail API with its own OAuth setup.
- [mailpopbox-router](https://github.com/rsesek/mailpopbox) moves mail from POP3 to Gmail as part of
  the mailpopbox server.

## Tests

```bash
python3 -m unittest discover -s tests -v
systemd-analyze --user verify systemd/email2gmail.service systemd/email2gmail.timer \
  systemd/email2gmail-watchdog.service systemd/email2gmail-watchdog.timer
```

The tests use fake POP, Gmail and Telegram endpoints and never send real mail. They cover lost
responses, interrupted runs, duplicate IDs, MIME and attachment preservation, the initial range,
queue fairness, API rejections and Telegram failures.

## References

- [Gmail POP support changes](https://support.google.com/mail/answer/16604719)
- [Gmail API: users.messages.import](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/import)
- [Gmail API: media upload](https://developers.google.com/workspace/gmail/api/guides/uploads)

## License

[MIT](LICENSE)
