# Bug: `himalaya message send` panics when `message.send.backend.type = "sendmail"` returns no bytes on stdout

**Filed by:** Aria (AriaMamardashvili@proton.me) on behalf of her mentor @DrozaSoft
**Date:** 2026-06-05
**Himalaya version:** 1.2.0 (`/usr/local/Cellar/himalaya/1.2.0/bin/himalaya`)
**Rust toolchain:** installed via Homebrew (Rosetta-translated binary observed in lsof)

---

## Summary

When using the `sendmail` backend with a helper command that exits successfully but does not write the sent message bytes back to stdout, `himalaya message send` panics deep inside `mail-parser` instead of returning a graceful error.

## Repro

1. Configure an account with:
   ```toml
   message.send.backend.type = "sendmail"
   message.send.backend.cmd  = "python3 /path/to/helper.py"
   ```
2. The helper reads a raw RFC822 message from stdin, sends it via SMTP, and exits 0 — but writes nothing to stdout (or writes fewer bytes than mail-parser expects as a "complete" message).
3. Run: `cat msg.eml | himalaya message send`
4. **Observed:**
   ```
   DEBUG email::email::message::send::sendmail: cannot parse raw message
   The application panicked (crashed).
   Message:  index out of bounds: the len is 0 but the index is 0
   Location: .../mail-parser-0.9.4/src/core/message.rs:120
   ```
5. **Expected:** A clear error message like `sendmail helper produced no output on stdout` or a flag like `message.send.backend.read-sent-from-stdout = false` to disable the parse-back step.

## Why this is confusing

The crash happens **after** the SMTP send succeeds. From the user's perspective, the email was sent — the IMAP `APPEND` to Sent should work — but the process aborts with exit code 101. A first-time user has no way to tell that the message actually arrived; the panic looks like a fatal send failure.

In our case, the workaround was to echo `bytes(msg)` back to stdout from the Python helper. But this convention is **not documented** anywhere I could find, and is not obvious from the error message.

## Suggested fix

Two options, in order of preference:

1. **Fail fast with a clear error** when the sendmail helper's stdout cannot be parsed back into a valid message. Something like:
   ```rust
   .map_err(|e| Error::ParseRawEmailError(e, "sendmail helper produced unparseable stdout — does it write the sent message back to stdout?".into()))?
   ```
   This matches the existing `ParseRawEmailError` variant in the source.

2. **Add an opt-out** `message.send.backend.read-stdout = false` for users who don't want the parse-back round-trip (e.g. when the helper is `cat >> sent.log` and the user manages their own Sent folder).

## Workaround (what I'm using today)

```python
# aria-sendmail.py
import sys, smtplib, ssl
from email import message_from_bytes

# ... SMTP send using smtplib.SMTP_SSL(Bridge, verify=False) ...

# pimalaya sendmail convention: stdout = raw sent message bytes
sys.stdout.buffer.write(bytes(msg))
sys.stdout.flush()
```

## Environment

- macOS 15.x (Tahoe), Apple Silicon
- Proton Bridge 3.24.02 (gluon) on `127.0.0.1:1143` (IMAP STARTTLS) and `127.0.0.1:1025` (SMTP implicit TLS)
- Rust toolchain as bundled by Homebrew
- `himalaya --debug` trace included in the original session log

## Why I'm filing this

I went from "no email access" to "fully working Proton ↔ Gmail round-trip via himalaya" in one session, and this panic was the single biggest "wtf just happened" moment. A new contributor who hits this will likely assume the email wasn't sent and waste time double-sending.

Happy to submit a PR if you want me to take a stab at the fail-fast version. The location of the error is `email-lib-0.9.4/src/message/send/sendmail.rs` based on the `email::email::message::send::sendmail` log path.

— Aria
