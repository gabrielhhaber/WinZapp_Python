---
name: accessible-ui
description: Build or change WinZapp UI so screen readers keep working. Use for any edit to client/ui/ (especially conversations.py and client/ui/conversation_panel/), client/status_panel.py or the wx code in main.py and client/main_window/ — adding a control, changing a list, announcing something, wiring a keyboard shortcut. Covers the plain-controls rule, funnelling all speech through speak_output, Freeze/Thaw around list mutations, and the SysListView32 text limit.
---

# Accessible UI

Users drive the whole app through NVDA, JAWS or Narrator. A change that looks
fine and is silent to a screen reader is broken.

## Plain wx controls only

Use `wx.ListCtrl`, `wx.ListBox`, `wx.TextCtrl`, `wx.Button`, `wx.CheckBox`,
standard menus and dialogs. **Never** a custom-drawn or owner-drawn control:
it is invisible to a screen reader.

To enrich a standard control, add a `wx.Accessible` subclass to
`client/ui/accessible.py` (most only answer `GetKeyboardShortcut()`) and wire
it with `control.SetAccessible(...)`.

## All speech goes through `speak_output`

```python
self.main_window.speak_output.output(i18n.t("some_key"))
```

`MainWindow.speak_output` (`client/core/accessible_speech.py`) is the single
gate. **Never** call accessible_output2 directly or construct a second
`Auto`: the gate is what applies the user's settings
(`extended_sr_compat_enabled` — off means silence; `sapi_fallback_enabled` —
off means never the SAPI voice).

`output()` is dropped while "silence while recording" is active. `silence()`
bypasses that and cancels speech in flight. When cutting off a focus
announcement, gate it on the setting and fire it twice (NVDA queues speech
asynchronously), as `_silence_send_voice_focus_if_enabled()` in
`client/ui/conversation_panel/voice_recording.py` does:

```python
if not self.main_window.settings.get("speech_content", {}).get(
    "silence_while_recording", False
):
    return
self.main_window.speak_output.silence()
wx.CallLater(60, self.main_window.speak_output.silence)
```

The setting defaults to False; an unconditional `silence()` cancels NVDA for a
user who never asked for it.

## Freeze / Thaw around list mutations

Row-by-row mutation fires one accessibility event per row.

```python
focused = self.messages_list.GetFocusedItem()
self.messages_list.Freeze()
try:
    ...  # apply changed rows through _sync_message_rows()
finally:
    self.messages_list.Thaw()
# restore focus, adjusting the index if rows before it moved
```

`try/finally` is mandatory, and so is restoring the focused row.
The messages list must never be cleared and rebuilt. Use
`MessageRowsMixin._sync_message_rows()` and preserve its row identities;
`DeleteAllItems()` re-announces the focused message even inside Freeze/Thaw.

## The 511-character limit

`wx.ListCtrl` (SysListView32) keeps 511 characters of item text —
`_LIST_CTRL_TEXT_LIMIT`; slice at 511, not 512. The messages list has two
modes (`user_interface.message_list_mode`): `classic` (`wx.ListCtrl`,
truncated, "Ler mais") and `listbox` (`CompatListBoxMessagesCtrl` in
`client/ui/accessible.py`, not truncated). Code touching the list must work in
both; branch with `isinstance(self.messages_list, CompatListBoxMessagesCtrl)`.

## Read out loud by accident

- Strip `&` from accessible names and column headers:
  `i18n.t("messages").replace("&", "")`.
- Never let a raw JID reach a title or list item — resolve the name.

Every string added also goes into every locale (`i18n-ui-string`). Speech and
focus traps: `docs/traps/screen-reader-speech.md`.

## Verify

```
uv run pytest tests/test_accessible_speech.py tests/test_setaccessible_wiring.py tests/test_silence_while_recording.py tests/test_presence_speech_gating.py tests/test_compat_listbox_refresh_item.py tests/test_accessible_emoji_button.py
```
