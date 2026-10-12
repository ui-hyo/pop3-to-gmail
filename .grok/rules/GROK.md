# Email2Gmail project rules

- Read README.md for operating commands and recovery semantics.
- Source cleanup defaults off. Delete only after explicit user enablement, retention expiry, fresh UIDL/source/Gmail content verification, and exclusion of Gmail SPAM/TRASH. Persist delete intent before DELE; confirm QUIT or reconcile absence. Never QUIT to clean up an interrupted deletion transaction.
- Keep credentials/config/runtime mail outside the repository and redact API error bodies/URLs.
- Pin both source and destination identity. Durable import intent must precede Gmail mutation.
- Never blindly retry ambiguous Gmail imports; preserve pending payloads and reconcile before declaring success.
- Preserve MIME bytes and original semantic headers. Telegram summaries contain counts/status only.
- Monitoring (`health`) stays read-only and exposes only fixed codes, counts, and scan age. A fresh scan alone does not prove successful delivery.
- Use CLI `ignore` only after operator review of a blocked ref; preserve source, spool, and the recorded error while removing that item from active monitoring.
- Run `python3 -m unittest discover -s tests -v` for delivery/state changes.
- Keep AGENTS.md, CLAUDE.md, GEMINI.md, and .grok/rules/GROK.md aligned.
