"""The pieces StatusPanel (client/status_panel.py, the Alt+5 tab) is assembled from.

status_panel.py used to hold the whole Status tab — the panel and its two
dialogs — in one 3,200-line file. It now keeps only ``__init__``,
``init_UI``, the accelerator table, show/escape handling and
``refresh_labels``; every other method lives in one mixin per responsibility
below, and ``class StatusPanel(<every mixin>, wx.Panel)`` puts them back
together. The methods were moved verbatim, so ``self`` is still the panel.
Behaviour of the tab is documented in docs/reference/status-tab.md.

Where to look (and where new code goes):

  Mixins (methods of StatusPanel)
    status_loading       API fetch, parsing, merging with the cache, my-status reconcile
    status_list          the contacts-with-status list: rows, keys, selection, activation
    status_viewer        my-status dialog, media viewer, legacy viewer, prev/next, viewed
    status_interactions  liking and replying to a status
    status_media         video playback, copy text, save media
    status_composer      composer panels, posting text and media statuses
    status_voice         recording, previewing and posting a voice status
    status_ai            describing a photo or video status with the AI providers
    status_transcription transcribing an audio or video status on this computer
    my_status_transcription  the same keys in MyStatusDialog (a base class of that dialog)

  Plain modules
    status_rules         post-result checks, content label, media download, save extension
    status_dialogs       StatusReactionsDialog and MyStatusDialog

Rules for this package are the same as client/main_window/'s: new code goes
into the module that owns the responsibility, or a new module here — never
back into status_panel.py; a module global is looked up where the method is
defined (tests patch it with tests.god_modules.patch_status_panel_global());
speech only through main_window.output(), list mutations inside
Freeze()/Thaw().
"""
