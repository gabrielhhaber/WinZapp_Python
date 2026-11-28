"""The kinds of media of the Online AI page, in a window of their own.

Which received media the online AI handles: pictures, stickers, videos, voice
messages and PDFs. They lived on the page itself, which then had no letter
left for the "Message translation" button in any language (a "(&Z)" after the
label made NVDA read the letter twice). Reached from a button on the page now,
like the automatic options (ai_auto_options.py), with letters of their own.

The check boxes are built once and kept; AISettingsPage reads them as before
(``page.toggles``, apply()), and the settings' search finds them and opens the
window on the option (SettingsPages.add_subpage).
"""

import wx

from core.ai_media.config import KINDS

#: The per-kind switches: (kind, i18n key of its label). Stated here, in the
#: window that shows them, so the mnemonic tests read their letters
#: (tests/settings_controls.py follows a loop over a table of this file).
KIND_TOGGLES = (
    ("image", "ai_describe_images_label"),
    ("sticker", "ai_describe_stickers_label"),
    ("video", "ai_describe_videos_label"),
    ("audio", "ai_transcribe_audio_label"),
    ("pdf", "ai_pdf_accessible_label"),
)
assert [kind for kind, _key in KIND_TOGGLES] == list(KINDS)


class AIKindsPanel(wx.Panel):
    """One check box per kind of media; *kinds* is {kind: on}. The last one
    chooses what a video sends: frames spaced over it (on) or a single frame.
    Two more, off by default, add the caption and the quoted message to the
    description request (core/ai_media/context.py)."""

    def __init__(self, parent, page, kinds, several_frames=True, caption=False, quote=False,
                 streaming=False, hierarchy=False):
        super().__init__(parent)
        self._page = page
        self._keys = dict(KIND_TOGGLES)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.toggles = {}
        for kind, key in KIND_TOGGLES:
            control = wx.CheckBox(self, label=page._t(key))
            control.SetValue(kinds[kind])
            sizer.Add(control, 0, wx.ALL, 8)
            self.toggles[kind] = control
        self.several_frames = wx.CheckBox(self, label=page._t("ai_video_several_frames_label"))
        self.several_frames.SetValue(several_frames)
        sizer.Add(self.several_frames, 0, wx.ALL, 8)
        self.context_caption = wx.CheckBox(self, label=page._t("ai_context_caption_label"))
        self.context_caption.SetValue(caption)
        sizer.Add(self.context_caption, 0, wx.ALL, 8)
        self.context_quote = wx.CheckBox(self, label=page._t("ai_context_quote_label"))
        self.context_quote.SetValue(quote)
        sizer.Add(self.context_quote, 0, wx.ALL, 8)
        # Not a kind of media: how a text answer reaches the result window
        # (ai_result_streaming.py). Off by default.
        self.streaming = wx.CheckBox(self, label=page._t("ai_streaming_label"))
        self.streaming.SetValue(streaming)
        sizer.Add(self.streaming, 0, wx.ALL, 8)
        # Chat summaries: also summarize the older messages a long backlog
        # does not fit, in several requests (core/ai_media/summary_parts.py).
        self.hierarchy = wx.CheckBox(self, label=page._t("ai_summary_hierarchy_label"))
        self.hierarchy.SetValue(hierarchy)
        sizer.Add(self.hierarchy, 0, wx.ALL, 8)
        self.SetSizer(sizer)

    def refresh_labels(self) -> None:
        for kind, control in self.toggles.items():
            control.SetLabel(self._page._t(self._keys[kind]))
        self.several_frames.SetLabel(self._page._t("ai_video_several_frames_label"))
        self.context_caption.SetLabel(self._page._t("ai_context_caption_label"))
        self.context_quote.SetLabel(self._page._t("ai_context_quote_label"))
        self.streaming.SetLabel(self._page._t("ai_streaming_label"))
        self.hierarchy.SetLabel(self._page._t("ai_summary_hierarchy_label"))
