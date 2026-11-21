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
| Calls | placed and answered when `send-capabilities` says `calls: true` (WinZapp's neonize build with the call layer, `neonize_bridge/call_media.py`); video calls, upgrading to video and the camera when it also says `videoCalls: true` (neonize 0007 and an ffmpeg on the bridge's PATH, `neonize_bridge/call_video.py`), group calls with `groupCalls: true`; the others are refused up front (`core/call_logic.engine_refuses_call`); rejecting always works |

## The phone's call history

A call reaches only the devices of the person called, so the account's own
phone does not see a call placed from a linked device. It learns of it when
that device, at the end of the call, writes a `call_log` record to the
`regular` app-state collection. WhatsApp Web does this for every call it
placed (`fromMe`, not a call link), from its call-ending handler. WinZapp's
bridge sent nothing, so its calls worked and were missing from the iPhone's
history (user report, 2026-11-16).

`neonize_bridge/call_log_sync.py` writes the record that WhatsApp Web
2.3000.1052669929 wrote, captured over CDP on 2026-11-17 by hooking
`WAWebCallLogSync.getCallLogMutation` (`diagnostico_cdp/calllog_out.js`).
`calls.record_finished` sends it after the chat's own record. The record is:

- index `["call_log", <own LID without the device>, <call id>, "1"]`,
  version 1;
- `callLogAction.callLogRecord` with `callResult`, `duration` (seconds
  connected), `startTime` (Unix seconds of the offer), `isIncoming: true`
  (WhatsApp Web copies `fromMe` into it; the phone takes the direction from
  `callCreatorJID`), `isVideo`, `callID`, `callCreatorJID`,
  `participants: []` and `callType: REGULAR`.

Only one-to-one calls get this record. A group call's record also carries
`groupJID` and each participant's outcome, and has not been captured yet.

Two other things WhatsApp Web sends when it hangs up, which meowcaller does
not:

- `duration` and `audio_duration` on the `<terminate>`;
- a `<call to="call"><duration type="1x1" peer=… audio_duration=…/>` report.

WhatsApp Web builds a call placed on another device from the `call_log`
record alone (`WAWebVoipActionWriteCallLogSync`), so neither is sent yet.

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

## Changes made here (app state the bridge sends)

`Session.send_state` names a person's chat by its LID in every mutation index
(`chat_aliases.mutation_index_jid`), as WhatsApp Web does
(`WAWebSyncdGetChat.getChatJidMutationIndexForChat`, read 2026-11-09): a phone
does not find a chat named by its number and keeps the mutation as an orphan.
A clear or delete carries a message range built as WhatsApp Web builds it
(`chat_actions.range_plan`): the newest message's time, the newest system
message's when newer, and our own messages from the newest received one on by
key. A key the phone's check refuses (a received group message with no
`participant`) is never listed, since one such key drops the whole mutation.
The clear's index ends in `"0"`, like WhatsApp Web's and Baileys'. Reported
2026-11-05: chats cleared in WinZapp stayed full on the phone, because the
bridge listed only the newest stored item, often a group notification with no
author.

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

## Pairing history cap and omitted conversations

WhatsApp's iPhone client caps `INITIAL_BOOTSTRAP` (`syncType = 0`) at 399 chats
and delivers older conversations only as `OmittedConversationMetadata` (field
25) batches when the companion advertises
`DeviceProps.HistorySyncConfig.supportOmittedConversationMetadata = true` (field
28) during registration (`neonize_bridge/omitted_history.py`,
`neonize_bridge/server.py`). Because `DeviceProps` capabilities are sent only
in `getRegistrationPayload`, an account paired on an older build must re-pair
once to receive omitted conversations. Unopened omitted chats hold zero local
messages and trigger no phone requests in the background; opening one or
pressing Home requests its initial 50-message page via
`PeerDataOperationRequestMessage` in `INITIAL_REVERSE_HISTORY` mode (`mode = 1`,
`neonize_bridge/older_history.py`), while community parent containers
(`isParentGroup` / `GroupParent.IsParent`) are skipped from `chats`. See
[the sync completion traps](docs/traps/sync-completion.md) for wire fields and
protocol details.

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
- `messages.update` and `presence.update` are alternative client input shapes;
  Neonize uses `onack` and `onpresencechanged`. Poll votes now emit
  `onpollresponse` and trigger a refresh for a poll in the open conversation.
  Revoke and label-association event contracts are described in
  `neonize_bridge/COVERAGE.md` (label events serve API consumers; WinZapp has
  no Business-label interface).
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
