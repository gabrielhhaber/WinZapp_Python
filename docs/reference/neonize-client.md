# The client on the Neonize engine

What the client assumes when `wpp_engine` is `neonize` (a "custom API" client
pointed at the local bridge in `neonize_bridge/`: no Node, no Chrome). The
bridge's own routes are in `neonize_bridge/COVERAGE.md`. With no Chrome there
is no CDP: inspecting a running bridge is in `docs/reference/live-inspection.md`.
The neonize the bridge runs is built from source with WinZapp's patches:
`docs/reference/neonize-build.md`.

## Contact profile names

The client's `get_remote_contacts()` normalizes both `pushName` (WPPConnect)
and `pushname` (Neonize). `core/remote_contact_names.py` learns public nicknames
before the phonebook filter, including group-only participants without a private
chat. It fills the name cache under known phone/LID aliases without marking a
person as saved or replacing an address-book name. The filtered phonebook records
are merged separately. Regression tests: `tests/test_remote_contact_names.py`.

On a newly linked account, a nickname can still be absent from both the initial
push-name history and the messages' `notifyName`. The bridge learns later names
through `MessageEv` and `PushNameUpdateEv`; a missing name is not recoverable by
changing the client's casing or filter alone. Inspect the live API and bridge
store before treating a number fallback as a display regression.

## Both engines ship in a compiled build

`build.py` freezes `neonize_bridge/` into `neonize_bridge/neonize_bridge.exe`
(`winzapp_tools/bridge_build.py`) and stages it next to `api/`, so a compiled
install has WPPConnect's code and Neonize together. Node.js is not bundled
(`stages_node()` in `build.py`): choosing WPPConnect downloads it. `neonize_bridge_process`
starts the packaged exe, and keeps its data and run files in
`data/global/neonize_bridge/` (not in the install folder, which an update
replaces); an older install that kept them in a sibling `neonize_bridge/data`
has them moved in on the first start (`neonize_bridge_layout.adopt_sibling_data`).
The updater stops a bridge left running before it copies files
(`update_node_kill`). The neonize inside it is WinZapp's build from source
(`docs/reference/neonize-build.md`), so a release build needs Go, a 64-bit gcc
and network access to GitHub and the Go module proxy.

## The engine is a setting

`connection.wpp_engine` (`wppconnect`, `neonize` or `custom`) is install-wide
(`app_settings._CONNECTION_GLOBAL`). It used to be inferred from
(server, websocket, port 6300) by `core/connection_engine.engine_of()`, but the
port is per account: after one account switched the install to Neonize, an
account on an automatic port read as "custom" and started no bridge, and a real
WPPConnect typed in at `127.0.0.1:6300` read as Neonize.

- `engine_from_connection()` believes the stored value only while it agrees
  with `wpp_custom_api`; otherwise it infers (installs from before the setting,
  hand-edited files). `_apply_global_settings()` stores the inferred value once,
  from the first account to start.
- `MainWindow.wpp_engine` is the engine this process started with. A change
  (Settings, import) needs a restart; `_uses_neonize()` reads it.
- With Neonize every account's port is the bridge's (`_resolve_wpp_port`).
- Settings export/import never carry the engine of a bundled or Neonize
  install, only a custom server (`settings_transfer.ENGINE_KEYS`); an import
  that would change the engine is saved, not applied, and asks for a restart.

## Things that do not apply, and what stops them

| WPPConnect machinery | With Neonize |
| --- | --- |
| WPPConnect update checker, Help > Force WPPConnect reinstall, `_update_wpp_server` | skipped (`profile_is_local`) |
| Chrome profile, snapshots, orphan Chrome kill | skipped (`profile_is_local`) |
| Orphan-session sweep at shutdown | skipped: the bridge answers 401 to name-only calls and its sessions are the other accounts' |
| Abandoned-session cleanup | asks the bridge to `delete-session` each stale name |
| Removing an account | asks the bridge to `delete-session` each session name; the account stays `deleting` until it confirms (`account_removal.purge_account(bridge_delete=...)`) |
| Node lease | registered when the bridge is started or joined (`_register_node_lease`), so "Quit all accounts" and the updater see the account |
| Calls | placed and answered when `send-capabilities` says `calls: true` (WinZapp's neonize build with the call layer, `neonize_bridge/call_media.py`); video calls, upgrading to video and the camera when it also says `videoCalls: true` (neonize 0008 and an ffmpeg on the bridge's PATH, `neonize_bridge/call_video.py`), group calls with `groupCalls: true`; the others are refused up front (`core/call_logic.engine_refuses_call`); rejecting always works |

## Call tones

With WPPConnect the WhatsApp Web page plays the sounds of a call; Neonize has no
page, so `core/call_tones.py` makes them and `core/call_tone_player.py` plays
them (looped, through `sound_system.load_sound`, from WAVs cached under
`data/cache/call_tones`). The hooks are in `main_window/calls.py`
(`on_voice_call_state_event` and `_stop_voice_call_audio`), and only run when
`_uses_neonize()`.

- Wired today: the ringback while OUR call is `RINGING`, by the country of the
  number dialed (`call_tone_data.RINGBACK_ROWS`: the first row whose calling
  code is a prefix of the number wins, the first row is the default, as in the
  original); the 480 Hz beeps while `CONNECTING` and `REJOINING`.
- In the catalogue but not wired, because the bridge sends no event for them
  yet: busy, interruption, the group-call short tone, the incoming call waiting
  while another is in progress, the video upgrade request.
- The "offer sent" tone is not made: the original takes it from a server
  property (`connecting_tone_desc`) and has no default.
- The numbers were read from the tone table of the Microsoft Store build
  2.2641.101.0 (`WhatsAppNative.Voip.dll`, built on pjmedia's `tonegen`). A row
  whose second frequency is 16-50 Hz and carries the flag byte is treated as
  amplitude modulation at that rate; that reading of the flag was not checked
  against the original's synthesis loop. Check by ear against a real call.
- The ringtone of an incoming call and the notification alerts are recorded files in
  that app (`Sounds/`), not synthesized tones, and are not part of WinZapp. The
  hang-up sound (`whatsapp_windows_hangup_01_705kbps.wav`, WhatsApp's own recording, so third-party audio) is shipped in the default
  pack as the `call_ended` Sound Event (`call_ended.ogg`); `play_call_ended_sound`
  plays it once when a call that was ACTIVE ends, on Neonize only.

## Changes made on the phone (app state)

neonize forwards the phone's chat and message changes as events, and the bridge
mirrors each into its store (`server.py`, registered once per event type):
`ChatMarkAsReadEv` (read or marked unread; answers `chats-update` with
`unreadCount`), `ChatArchiveEv` and `ChatPinEv` (`chats-update` with `archive` /
`pin`, which WinZapp's `on_chats_update` consumes), `ChatMuteEv` and
`ChatLockEv`, `MessageStarEv`, `MessageDeleteForMeEv` and `ChatDeleteEv`. A
chat deleted on the phone is deleted in the bridge unless the mutation is a full
sync's replay or a newer message is stored (`phone_chat_delete.applies`). WinZapp
has no socket event for a chat that disappears, so the app's list drops it on
its next `list-chats`.

## Reading a chat writes no app state

A read goes out as read receipts, the way WhatsApp's own clients do it:
`WAWebChatSendConversationSeen` sends `markChatAsRead` only when undoing a chat
marked unread by hand, and reads made on the phone reach the bridge as
`READ [mine]` receipts with no notice behind them. `read_receipts.needs_read_notice()`
keeps the notice for what a receipt cannot reach: a chat marked unread by hand,
or a count higher than the messages the store holds.

The bridge once sent a notice per chat opened. Measured 2026-11-02 after five
days away from WPPConnect: 585 patches in `regular_low`, at least 226 with nothing
to read, applied by WhatsApp Web at about 0.7 s each. The offline queue started at
18:49 and counted messages already read here; the collection finished at 18:56.
The order matters twice: a notice applied first keeps an active range that covers
the messages arriving after it, and one applied to a chat already holding newer
messages is stored as `Success` and changes nothing.

## Known leftovers (not fixed)

- Video and group call buttons are still shown when the bridge cannot take
  those calls (an older build, no ffmpeg); they answer with "This action is
  not available for calls." `send-capabilities` already says which kinds it
  takes (`videoCalls`, `groupCalls`), so the client could hide the others.
- Video and group calls through meowcaller were not tried on a real account
  yet; meowcaller marks its video media path "NOT VALIDATED".
- `delete-session` logs the device out only if the session is connected on the
  bridge at that moment. The account removed while in use is closed first, so
  its device can stay linked on the phone (remove it there).
- Texts that send the user to Help > Force WPPConnect reinstall
  (`voice_call_voip_unavailable`, `clear_chat_starred_kept_on_phone`) are
  misleading with Neonize.
- Custom-list filters and buttons are always shown; on a neonize build without
  app state mutations the bridge answers 501 and the client says "unavailable"
  only after the click. With WinZapp's build the first read of the lists
  downloads the `regular` collection once and can take longer than the
  client's 15 s budget: the next refresh answers.
- Events the bridge never emits (announcement-replies, media-*-progress,
  `call:*:remote`, messages.update, presence.update) are harmless.
- `reconnect-socket-stream` answering 400 counts strikes towards
  `_restart_wpp_session` (`connection.py`); the bridge side is F1's.
- `_start_wpp_update_checker` with no `client/api/` folder is now skipped for
  Neonize; for WPPConnect it still schedules a retry every 12 h.
- Settings dialog: choosing another engine still sets `main_window.wpp_custom_api`
  live (before the restart the dialog asks for); only `wpp_engine` waits.
- Name-only calls (`close-session`, `check-connection-session`, `logout-session`
  with `Authorization: Bearer ...`) depend on the bridge accepting that
  credential (F1). `ui/dialogs/connect.py` line ~291 and
  `session_tokens._logout_abandoned_session` send the *signature half* of the
  token as the Bearer, not the bridge secret; the other sites send the global
  key (the secret under Neonize).
- The packaged build does not know the bridge yet (`build.py`, the updater's
  `xcopy`, `own_node_pids` only handles `node.exe`).
