# Diagnosing from `log.log` and `shutdown_audit.log`

> Which log answers which question, and why a lost session is diagnosed from the *previous* run.
>
> Moved verbatim out of `CLAUDE.md` so it is read when the area is touched, not on every session. Keep it here: this is measured history, not a summary.

- Logging (`log_path()`): a single `log.log`, truncated at the start of every launch (plain `logging.FileHandler(mode="w")`, not a `RotatingFileHandler` — there are deliberately no `log.log.1`/`.2` backups). `setup_logging()` runs only after the single-instance mutex is acquired, so a second launch while WinZapp is already running never wipes the log of the instance actually doing the work. When diagnosing anything startup/pairing-related, this file's breadcrumb `logging.info(...)` calls (`[prepare_sync]`, `[init_UI]`, `[show_connection_dial]`, `[show_pairing_dial]`, `[on_pairing_complete]`, ...) are the fastest way to pinpoint exactly which step never happened — always ask for the current run's `log.log` rather than guessing.

  **Ask for `shutdown_audit.log` as well, and always for anything that looks like a lost session.** It sits beside `log.log` and is opened in append mode, so unlike `log.log` it survives every launch: it is the only place a *previous* run's ending is recorded (`STARTUP` with the active session and store contents, `_stop_wpp_server START`, the `flush poll` sequence, `FLUSH OK`, the final `taskkill`). That distinction decides the diagnosis. A user reporting "it asks me to pair again" hands over a `log.log` that begins with the session *already* dead — the cause is in the run before, whose log was overwritten the moment they reopened the app to check. Read against `shutdown_audit.log`, the answer is usually immediate: a run that ends with `FLUSH OK — session reached CLOSED` shut down cleanly and its session should have survived, while a `STARTUP` line with no `_stop_wpp_server` before it means the previous process was killed rather than closed, so WhatsApp's auth state never flushed to `userDataDir` and the profile comes back unusable. Note that killing the process (Task Manager, a harness stopping it, a crash) is enough to cause that — it is not a bug on its own, and mistaking it for one costs a lot of time.

## `[listChats] diag` in `wppconnect.log`: why a sync lists no chats

`POST /list-chats` answering 200 with `[]` says nothing about WHY. For every call that ends with no visible chat, and for the first call of a session that returns any (at most one line per 30 s per session, plus one whenever the numbers change), `deviceController.listChats` logs one line after the response, with counts, booleans and short ASCII codes only (never a JID, number, name or text):

```
[listChats] diag raw=0 rawFirst=0 rawSecond=0 visible=0 groups=0 users=0 lids=0 other=0 droppedShells=0 droppedNoT=0 droppedNoLastMsg=0 droppedNoUnread=0 droppedNoOpened=0 droppedNoContact=0 recovered=true firstError=none storeReady=true storeChats=0 idbChats=0 ms=181
```

- `raw` is the list WPP.chat.list() returned, serialised, BEFORE the `visibleChats` filter; `rawFirst` / `rawSecond` are the first read and the read after the IndexedDB recovery (`rawSecond=none` when the recovery did not run, which it only does when the first read is empty). `visible` is after the filter; `droppedShells = raw - visible`, and `droppedNo*` count, among the dropped chats, those lacking `t`, `lastMessage`, an unread count, `hasChatBeenOpened`, `contact.isMyContact` (the five things the filter looks at).
- `groups` / `users` / `lids` / `other` split `raw` by JID suffix.
- `storeReady` is wa-js's `WPP.isReady` and `conn.isMainReady()` and a `ChatStore`; `storeChats` is the ChatStore's own size (`na` when unreadable). `idbChats` is a read-only count of the `chat` store of the `model-storage` IndexedDB, taken only when `visible=0` and at most once a minute (`skipped` otherwise; `unavailable` or `timeout` when the page would not answer).
- `firstError` is the error of the first read as a short code (`none` when it did not fail); `ms` is the time the evaluate took.

How to read it:

- **`raw>0`, `visible=0`**: WhatsApp Web has chats, and the filter dropped all of them. Read the `droppedNo*` counts: all equal to `droppedShells` means shells with no activity (group participants' key notifications), which is what the filter is for; if real conversations are among them, a field the filter reads is missing from the serialised chat, and the filter is what to fix.
- **`raw=0`, `idbChats>0`**: the page's store is empty while the IndexedDB holds chats, the split-brain the recovery exists for. With `recovered=true` and `rawSecond=0` the recovery ran and `find()` did not put them back (see the existing `IndexedDB recovery:` warning for its numbers); `storeReady=false` points at a page that is not ready yet.
- **`raw=0`, `idbChats=0`**: nothing is persisted either, so WhatsApp has not delivered the chat list to this device (an account whose sync WhatsApp itself has not completed: the official client would fail the same way). Nothing in WinZapp can produce chats here; wait or re-pair.
- `storeChats>0` with `raw=0` means the store has chats that `WPP.chat.list` does not return; `firstError` names the failure when there is one.

Ask the tester for `wppconnect.log` AND `log.log`: the Python side's `Chat list still empty after every attempt` warning points here.
