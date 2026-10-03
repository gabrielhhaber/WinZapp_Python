"""The pieces ConversationsPanel (client/ui/conversations.py) is assembled from.

conversations.py used to hold all of ConversationsPanel (and the archived
list) in one 18,000-line file. It now keeps only ``__init__``, ``init_UI``,
the message-list control setup and ``refresh_labels``; every other method
lives in one mixin per responsibility below, and
``class ConversationsPanel(<every mixin>, wx.Panel)`` puts them back
together. The methods were moved verbatim, so ``self`` is still the panel and
every attribute ``__init__``/``init_UI`` set is available in every mixin.

Where to look (and where new code goes):

  Mixins (methods of ConversationsPanel)
    accelerators             accelerator tables (list and open conversation)
    conversation_navigation  open/close/restore a conversation, chat-list filter
    panel_visibility         which panel an open conversation belongs to, and showing it only there
    composer                 message field: spell check, link preview, keys, paste
    text_sending             sending/editing text, pending rows, cancelled sends
    voice_recording          recording and sending voice messages
    system_audio_recording   mixed microphone + system-audio (WASAPI loopback) recording
    list_refresh             populate_messages, repaints, incoming messages
    message_list             moving in the message list: select, activate, jump
    message_rendering        a message record -> its row text
    message_rows             writing rows into the list one by one, never clearing it
    unread_separator         the unread-messages separator row
    history_loading          loading older history into the open conversation
    message_menu             message context menu and read-only actions
    message_actions          star, pin, delete, cancel, edit, resend
    message_accels           accelerator handlers for messages
    bulk_messages            bulk actions on selected messages
    forwarding               forwarding messages
    reactions                sending/applying/backfilling reactions
    attachments              attaching files and contacts
    contact_messages         contact (vCard) and location messages
    media_files              opening/saving/downloading media, transfer progress
    ai_actions               AI transcription/description of a message's media
    audio_playback           voice/audio playback, chaining, speed, seek
    links                    links panel of the focused message
    mentions                 @mentions panel and suggestions
    bookmarks                message bookmarks
    message_search           search inside the conversation
    chat_menu                conversations-list context menu and chat actions
    chat_list_selection      chat multi-selection shared by every chat list (main, archived, locked)
    chat_selection           the conversations list's keys and bulk chat actions
    conversation_info        conversation data, profile, presence note
    formatting               timestamps, dates, durations, file sizes

  Plain modules
    archived_panel           ArchivedConversationsPanel (Alt+3)
    media_paths              media cache/saved paths, reveal in folder, probing
    text_helpers             small text helpers (_URL_RE, captions, last seen)
    selection_rules          pure chat multi-selection rules
    transfer_gauge           the focus-keeping transfer gauge control

Rules for this package:

* A new ConversationsPanel feature goes into the mixin that owns its
  responsibility, or into a NEW module here when none does — never back into
  conversations.py.
* A module global a method uses is looked up in the module that method is
  defined in. Tests patch it with
  ``tests.god_modules.patch_conversations_global()``.
* Pure logic (no ``self``, no wx) belongs in a plain module with a direct
  test, not on a mixin. UI rules still apply everywhere: speech only through
  ``main_window.output()``, list mutations inside Freeze()/Thaw().
"""
