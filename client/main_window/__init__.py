"""The pieces MainWindow (client/main.py) is assembled from.

main.py used to hold all of MainWindow in one 35,000-line file. It now keeps
only ``__init__``, ``init_UI``, startup/crash handling and the ``__main__``
block; every other method lives in one mixin per responsibility below, and
``class MainWindow(<every mixin>, wx.Frame)`` puts them back together. The
methods were moved verbatim, so ``self`` is still the MainWindow instance and
every attribute ``__init__`` sets is available in every mixin.

Where to look (and where new code goes):

  Mixins (methods of MainWindow)
    window_chrome       menu bar, title, accounts menu, IPC, bookmark/global hotkeys
    window_lifecycle    activation, tray, close/hide/restore, focus, shutdown
    shortcuts           accelerator table, Alt+N navigation, output() speech funnel
    settings            first run, settings load/save/migrate/export, sounds
    connection          connection state, suspend/resume, WebSocket, reachability
    account_link        periodic health check, "another number linked" wipe
    session_tokens      WA_token vault, session store, abandoned sessions
    session_lifecycle   Windows end-session teardown, profile recovery/snapshots
    wpp_server          local WPPConnect Server: install, version, ports, start/stop
    api_start_behind_window  a paired launch: window first, Node started behind it (#407)
    updates             app and WPPConnect Server update checks
    wpp_background_update  WPPConnect Server update built in the background, then swapped in
    sync                prepare_sync, _run_sync, per-chat sync planning
    backfill            background history backfill, history-sync status
    history             on-demand older history (fetch_older_messages)
    conversation_sync   sync_chat_messages, remote windows, deletion reconcile
    message_events      on_new_message / on_historical_message, edits, revokes
    chat_events         acks, presence, unread counters, archive/pin events
    contact_presence    bounded open-contact refresh and connection invalidation
    sending             text/audio/media/contact/reaction sends, queue callbacks
    message_actions     edit, delete, forward, resend, mark played
    message_stars       verified WhatsApp star writes, remote star persistence
    message_pins        read the linked device pinned-message overview
    media               media download, failed ids, base64 fetch, durations
    transcription_store saved voice-message transcriptions: store, delete
    read_state          mark read/unread, local-read anchor
    chat_actions        block, mute, archive, delete, clear, typing, pin
    chat_list           computing and rendering the chat list and previews
    chat_lists          WhatsApp custom lists, membership and account-scoped jobs
    chats_store         local chat storage, remote chats, dedup, saving
    contacts            local/remote contacts, self reference
    identity            JID normalization, @lid <-> phone, name resolution
    groups              group metadata, permissions, group management
    calls               voice/video calls, call bar, call-log watcher
    chat_lock           locked-chats vault
    phone_chat_lock     chats locked with WhatsApp Chat Lock on the phone
    quick_audio_devices Ctrl+Alt+Shift+H/G quick output/recording device switch
    message_ack         live delivered/read/played times of our own sent messages

  Plain functions (import and test them directly — no wx, no stub)
    message_rules       unread/history-gap/countable-message rules
    history_boundary    older history that is only on the phone (API verdict, probe)
    identity_rules      linked phone number, group participant identity
    runtime_setup       legacy API state, npm health marker, restore choice
    wpp_update_validation  HTTP validation and recovery of the previous API after an update
    win32_helpers       elevation, hotkeys, de-elevated spawn
    log_files           early log-file plumbing
    http_pool           the pooled requests session
    core.sync_lifecycle   generation/session ownership, backfill handoff, phone budget

Rules for this package:

* A new MainWindow feature goes into the mixin that owns its responsibility,
  or into a NEW module here when none does — never back into main.py, and
  never into a mixin just because it is the file you have open.
* A mixin must not import ``main`` (running as ``__main__`` it would execute
  main.py a second time). To reach a static/class member defined in another
  mixin, import that mixin class, or call it through ``self``.
* Logic that does not need ``self`` belongs in a plain-function module (here
  or under client/core/) with a direct test, not on a mixin.
* A module global a method uses is looked up in the module that method is
  defined in. Tests patch it with ``tests.god_modules.patch_main_global()``.
"""
