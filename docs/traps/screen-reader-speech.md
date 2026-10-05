# Screen-reader speech: the single funnel, the focus cloak, and list-row repaints

> `speak_output` gating, why cancelling speech is the wrong half of suppressing a focus announcement, and why a list row must not be rewritten while focus leaves it.
>
> Moved verbatim out of `CLAUDE.md` so it is read when the area is touched, not on every session. Keep it here: this is measured history, not a summary.

`MainWindow.speak_output` — the single funnel every spoken announcement in the app goes through, both via `MainWindow.output()` and the handful of call sites that reach it directly (`main.py`, `ui/conversations.py`, `core/websocket_client.py`, `ui/dialogs/connect.py`) — is an `AccessibleSpeechOutput` (`client/core/accessible_speech.py`) wrapping accessible_output2's `outputs.auto.Auto()`, not the bare `Auto()` instance. It gates every call on **Settings > Acessibilidade** (`settings["accessibility"]`, tab inserted right before Conteúdo Falado in `settings_dialog.py` — inserting/removing a tab there means bumping every hardcoded `_notebook.SetSelection(N)`/`SetPageText(N, ...)` for tabs after it, both in that file and in `main.py`'s custom-API first-run flow): `extended_sr_compat_enabled` (default on) is a master switch — off means `speak_output` never calls into accessible_output2 at all, silently, on the assumption the user is relying on the visual UI only; `sapi_fallback_enabled` (default on, matches the historical behavior) controls whether losing/never having an active screen reader falls back to the system SAPI voice the way `Auto()` does on its own (SAPI5 reports `is_active()` unconditionally True and outranks nothing missing) — off restricts `speak_output` to real screen readers only (`is_system_output() == False`), checked live on every call so turning NVDA/JAWS off mid-session silences WinZapp immediately rather than SAPI quietly taking over.

**Suppressing a focus announcement is a different problem from silencing speech, and cancelling is the wrong half of it.** Settings > Conteúdo Falado's "silence while recording a voice message" started as a `silence_screen_reader_focus()` burst fired right after `SetFocus()` on the Enviar/Descartar button. That is a race the app loses either way: Windows delivers `EVENT_OBJECT_FOCUS` synchronously, but NVDA speaks it asynchronously on its own thread, so a cancel at 0 ms cancels nothing and one at 80 ms lands after speech has begun. Users on air heard "enviar mensagem de voz, botão, Ctrl+R" clipped part-way. Blanking the accessible name was tried and removed (it strips the control's identity for every consumer); `wx.Window.SetName()` was tried too and does nothing at all — it sets wx's internal window name, never the MSAA name.

`client/core/focus_cloak.py` is the actual mechanism. NVDA decides whether a focus event is worth speaking *before* speaking it, in `IAccessibleHandler.processFocusNVDAEvent()` → `IAccessible._get_shouldAllowIAccessibleFocusEvent`, which walks the object and its ancestors for `State.FOCUSED` and discards the event when none has it — and never recovers it, because NVDA reacts to events and does not poll the system focus. So a `wx.Accessible` on the button briefly reports its MSAA state without `STATE_SYSTEM_FOCUSED` and the announcement is never produced. Three things keep it safe, and each is load-bearing: the shim answers `wx.ACC_NOT_IMPLEMENTED` whenever it is not armed (wx then falls back to the standard MSAA object, so the control is untouched the rest of the time); it is installed **once per window and reused** via a flag, because `SetAccessible()` hands ownership to C++; and it disarms after ~500 ms, because it must hide only the focus move *WinZapp* performs — swallowing the announcement of a Tab the user pressed themselves would be far worse than the noise being removed. The `silence()` burst stays behind it as the weaker fallback (a control read over UIA rather than MSAA), no longer as the mechanism. `ConversationsPanel` and `StatusPanel` each keep their own copy of `_voice_recording_silence_enabled()` / `_focus_recording_button_silently()` / `_silence_send_voice_focus_if_enabled()`; they must stay in step, and both key on the silence toggle **alone** — `extended_sr_compat_enabled` being off means "stop talking to my screen reader", never "start interrupting it".

**The same lesson, one control over: a list row must never be rewritten while focus is moving off it.** A wx.ListCtrl row is a single MSAA object whose *name* is the whole rendered line, and NVDA's `NVDAObject.event_nameChange` speaks it only `if self is api.getFocusObject()` — so marking a finished voice note "reproduzido" makes NVDA read the **entire row** back, ahead of the newly focused one, whenever it lands while NVDA still believes the finished row has focus. Two successive attempts to fix this by *ordering* the two events failed, and both are measurable on the real code path (`tests/test_audio_chain_played_repaint_hold.py`): `refresh_message_status()` does not write anything, it queues the row behind a 120 ms coalescing timer, so the write landed 142 ms after the focus move — a margin, not a guarantee; and `mark_audio_message_played()`'s own played receipt echoes back from WhatsApp onto `on_message_status_update()` with `skip_panel_refresh=False`, repainting the row without passing through the chain at all — measured writing it **95 ms before** the focus move. `ConversationsPanel._release_chain_held_repaints()` is the actual fix: while the chain is moving focus, `_flush_status_repaints()` writes nothing at all and parks every queued row, releasing them when the sequence ends — at which point focus is on the last voice note and every held row is an earlier one, so NVDA is silent by its own rule rather than by timing. The hold is armed in `on_audio_timer()` *before* `mark_audio_message_played()` runs, which is what catches the echo. If you add another place that can end a sequence, call the release there too; it is idempotent but does not itself check whether the chain is still running.

## The message list is never cleared (2026-10-01)

`messages_list` is written only through `MessageRowsMixin._sync_message_rows()`
(`ui/conversation_panel/message_rows.py`, planner in `core/list_row_diff.py`):
it deletes and inserts only the rows whose identity differs and rewrites a
row's text only when the text differs. A `DeleteAllItems()` + re-`Append()` —
even followed by a careful re-`Focus()` — re-announces the focused row, and
`populate_messages()` used to run it whenever the 60 s poll found a live message
and a reaction record together (neither the tail-append nor the in-place repaint
accepted the mix), or after the user's own send (a pending row with no id is
"not comparable"). Measured in a 11 h log: 81 rebuilds, none of them caused by
typing/recording presence. A structural test fails if a new
`messages_list.DeleteAllItems` appears in the panel's modules. Focus is restored
only when the control does not already hold it on the right row.

## Switching to a chat panel: hidden conversation, chat list focus, no work (2026-10-02)

PR #339 made an open conversation visible only in the panel it was opened
from and showed it again when its panel came back; #346 made that cheap. The
maintainer still heard a delay on every Alt+1 <-> Alt+4 with a conversation
open in the other list (the pane re-shown, a displaced conversation reopened),
so the rule went back: **a plain panel switch never makes the conversation
pane visible and never loads anything.**

- **The rule.** Alt+1, Alt+4, the navigation list, the locked panel and the
  vault close all go through `ConversationPanelVisibilityMixin.show_chat_panel()`
  (`ui/conversation_panel/panel_visibility.py`). It hides every other panel
  *and the detail pane*, and focuses that panel's **chat list**, never a
  message list (#339's regression: Alt+4 landed in the archived chat's
  messages). The open conversation stays open (`conversation` set, message
  list untouched) but hidden, so `conversation_in_view()` is false and it stays
  out of mark-as-read, sounds, notifications and typing announcements. A switch
  is Show/Hide + `restore_selection()` + the `[panel-switch]` log line: no
  `populate_messages`, no `navigate_to_conversation`, no request, no thread, no
  `wx.CallAfter`. Do not add one: that is the delay.
- **Staying is not switching.** Alt+1 inside a main conversation, Alt+4 inside
  an archived one (and the locked panel's own entry, and choosing the panel you
  are in from the navigation list) change no panel, so they must not hide the
  pane: the focus goes to that chat list and the conversation stays on screen.
  `show_chat_panel()` reads `_detail_on_screen()` *before* anything is hidden
  and `panel_layout(..., keep=True)` keeps the pane only in the panel the
  conversation belongs to; the navigation list hides the content panels first,
  so it reads it before and passes `keep=`. Coming from Status or Calls, or
  from the other chat panel, is a real switch and hides it as above.
- **Explicit reveal.** The conversation comes back only on an explicit ask:
  Alt+M, Alt+2, Alt+3 (frame-level `_on_global_*`, the chat list's
  `_on_list_*` handlers, the archived and locked lists' Alt+M), all through
  `reveal_open_conversation()` -> `show_chat_panel(origin, focus=False,
  reveal=True)`. It shows the pane in the panel the conversation **belongs
  to** (`_conversation_origin`), so Alt+M on Alt+4 with a main chat open goes to
  the main panel; an archived chat's explicit reveal switches to the archived
  panel, list and pane together. The handler then moves focus itself. Opening a
  chat from a list (Enter, notification, tray, open-by-JID) is a fresh open and
  composes its own Show/Hide as before. The native mnemonic of the messages
  label inside the hidden pane cannot do this, which is why the main, archived
  and locked list tables bind Alt+M explicitly
  (`tests/test_alt_m_reveal_accelerators.py`).
- **No parked conversations.** The `_parked_by_origin` / `_finish_panel_reopen`
  / `resume=True` machinery existed only to bring a displaced conversation back
  on a switch. Nothing asks for the displaced one explicitly (an explicit
  command always means the open conversation), so it was deleted. Archived X
  open, then main Y opened: Y is the conversation; X returns by being opened
  from its list again, paying the open cost then.
- **One function.** Do not hand-roll a Show/Hide sequence for a new switch.
  Panel **opens** (`ArchivedConversationsPanel.on_conversation_selected`,
  `chat_list.py`'s open-by-JID, `open_locked_conversation`, the
  `_restore_to_*_list` Esc paths) still compose their own Show/Hide: an open
  puts the conversation alone in the place of its list and focus follows the
  user's setting. `lock_chat_vault` moves the user to the main list only when
  the locked list or a locked chat was on screen. Each switch logs one
  `[panel-switch]` line (panel, origin, layout flags, what `IsShown()` really
  reports; no JIDs). `tests/test_panel_switch_wiring.py` runs the real
  entry-point methods against recording widgets over origin x target and every
  explicit command, `tests/test_panel_switch_wx_semantics.py` repeats the key
  sequences with wx parent/child visibility.

## What an open costs

- **Where the time goes on an open** (UI thread): the DB read of up to 200
  messages plus `populate_messages` (log: "rebuilt 200 row(s) in ~90-160 ms").
  The serial `GET /reactions/<id>` calls (up to 40, ~41 ms each, one per
  message) are a background thread that predates #339 (commit 040b589b) with a
  five-minute per-chat cooldown; they do not block the UI but each applied
  batch triggers one more `populate_messages`. `/group-info` was fetched by two
  threads at once; both now go through `get_group_info_recent()`.
