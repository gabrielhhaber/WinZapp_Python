# Logging phone numbers, LIDs and contact names

> Why a JID logged directly is a phone number leak, why the fix is a global
> formatter rather than 150+ call-site edits, and what it still cannot catch.

Reported 2026-09-20 by testers in the public group: `log.log` and
`wppconnect.log` are shared there for diagnosis (CLAUDE.md's own diagnosing
docs instruct asking for them), and nearly every line carried a phone number
or LID in plain text. A prior fix
([`1ffd8fe3`](https://github.com/gabrielhhaber/WinZapp_Python/commit/1ffd8fe3),
Igor Alves) had already caught one severe case — `get_remote_chats()` logging
a whole raw `@lid` chat object, name/pushname/signed-photo-URL included — but
that was one call site among many, not the pattern.

**Why this is pervasive by construction.** A WhatsApp JID's leading digits
*are* the contact's phone number (`@s.whatsapp.net`/`@c.us`), or a
linked-device id that identifies one just as specifically (`@lid`) — see
CLAUDE.md's own JID-handling section. Well over a hundred `logging.*()` call
sites across `main.py` alone log a JID directly, because a JID is the natural
thing to name in a diagnostic ("failed for %s", "%s not found", ...). Auditing
and fixing every one of them individually is not a stable fix: it is
impossible to prove complete in a codebase this size, and the very next new
call site — which will keep being added, since logging a JID is the obvious
thing to do — silently reopens the leak.

**The fix: mask once, where all output already converges, on both sides.**

- **Python**: `client/core/pii_redaction.py`'s `redact_phone()` — a regex for
  a WhatsApp phone/LID/group-id digit run (`\d{8,20}`, optionally followed by
  a JID suffix), replacing each match with `~<8-hex-char md5 of the digits>`.
  Not full removal: the tag is deterministic, so a log can still show "this
  is the same contact three times" without ever spelling out which one — the
  same trade-off `redact_token()` makes differently (it drops the secret
  half entirely, because unlike a phone number there is no diagnostic value
  in knowing two calls used the same token). Wired into `main.py`'s
  `_PiiRedactingFormatter` (installed on the root logger's only handler in
  `setup_logging()`), which redacts the fully-rendered message — after
  %-args or an f-string are already merged into text — so it covers every
  existing and future call site without touching any of them. Also applied
  directly inside `core/api_client.py`'s `redact_api_url()`, so that
  function's own "log-safe" contract holds for a caller that doesn't go
  through `logging` at all.
- **Node**: `client/api_patches/src/util/logger.ts` (a new patched file —
  added to `CUSTOM_SRC_FILES` in `setup_api.py` **and**
  `ApiSetupDialog`'s `_CUSTOM_SRC_FILES`, and to `build.py`'s
  `API_CUSTOM_SRC_FILES` — see `docs/traps/wppconnect-upgrade.md` for why all
  three lists exist and must move together). The same regex, mirrored as
  `redactPhone()`, runs as a `winston.format()` step placed *after*
  `winston.format.errors({stack:true})` (so it also reaches `info.stack`,
  not just `info.message`) and *before* `colorize()`/`printf()`/
  `prettyPrint()`, in both the Console transport (which is what `main.py`
  pipes into `wppconnect.log`) and the file transport. This is what a status
  message id like `false_status@broadcast_<hex>_100742836789440@lid` needed
  — that trailing segment is always the poster's own JID
  (`_serialize_msg_id()` in `main.py`), and `deviceController.ts`'s
  `reactMessage()` logs `msgId` directly at the start of every status
  reaction.

**Digit range is 8–20, not a phone-number-shaped range.** A phone/LID digit
run is at most ~15; a *group* JID's own id runs longer —
`120363067944453158@g.us` is 18 digits — and a group JID's digits are never
a participant's digits (same invariant as `deduplicate_chats()`'s guard), so
group ids get masked too, deliberately on the safe side. The lower bound (8)
existed to avoid mangling small numbers (`count=200`, port numbers,
durations); it also means a message id's own hex segment can get partially,
harmlessly mangled if it happens to contain an 8+ digit pure-numeric run —
inspected and accepted, since the actual phone-bearing suffix is what has to
survive intact and does.

**What this does NOT catch: a bare contact name with no JID next to it.**
`redact_phone()` matches digit patterns; a name like "Bruna" has none. Two
severed cases found in the same audit, fixed at their own call sites instead
(the `get_remote_chats()` pattern, extended):

- `get_remote_contacts()` logged the first contact's whole raw dict *and* the
  actual text of up to 50 contact names, at INFO, on every contact sync.
  Fixed to log key names and value **types** only (shape, not content) —
  see `tests/test_contact_sync_log_has_no_pii.py`, sibling to
  `tests/test_chat_log_has_no_pii.py`.
- The LID-resolution profile-rejected branch logged the candidate name and
  the whole raw profile-API response. Fixed to log why the name was
  rejected (empty / placeholder / phone-like) and the response's shape.

**How to apply:** a new log statement that names a JID needs no special
care — the formatter catches it. A new log statement that could carry a
contact's *display name*, pushname, or any other free-text field pulled from
a chat/contact/profile object needs its own "shape, not content" treatment,
the way the two cases above got it; grep for `.get("name")`, `pushName`,
`pushname`, `formattedName`, `displayName` near a `logging.*()`/
`req.logger.*()` call before assuming the global formatter has it covered.
