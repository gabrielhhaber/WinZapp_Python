"""The connection-engine picker: a row in Settings > Connection and the
first-run question. The rules live in ``core.connection_engine``."""

import wx

from core import connection_engine as engines


def labels(i18n) -> list:
    return [i18n.t(engines.LABEL_KEYS[e]) for e in engines.ENGINES]


def build_row(parent, sizer, i18n):
    """Add the label, the list and the hint to ``sizer``; return
    (label, choice, hint)."""
    label = wx.StaticText(parent, label=i18n.t("connection_engine_label"))
    sizer.Add(label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
    choice = wx.Choice(parent, choices=labels(i18n), name=i18n.t("connection_engine_label").replace("&", ""))
    sizer.Add(choice, 0, wx.EXPAND | wx.ALL, 8)
    hint = wx.StaticText(parent, label=i18n.t("connection_engine_hint"))
    sizer.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
    return label, choice, hint


def relabel(label, choice, hint, i18n):
    """Re-apply the texts after a language change, keeping the selection."""
    selected = choice.GetSelection()
    label.SetLabel(i18n.t("connection_engine_label"))
    choice.SetItems(labels(i18n))
    choice.SetSelection(selected if selected != wx.NOT_FOUND
                        else engines.index_of(engines.WPPCONNECT))
    hint.SetLabel(i18n.t("connection_engine_hint"))


def ask_neonize_offer(i18n, main_window=None, announce=None, **dialogs) -> bool:
    """The one-time notice for installs on WPPConnect. True when the user chose to
    switch to Neonize; closing the dialog keeps WPPConnect. In a start in the
    background the window is hidden, so the dialog is announced and brought to the
    front (core/dialog_foreground.py): a blind user must never be left with a
    modal nobody spoke."""
    from core.dialog_foreground import choice_box

    return choice_box(main_window, i18n.t("neonize_offer_message"), i18n.t("neonize_offer_title"),
                      i18n.t("neonize_offer_switch"), i18n.t("neonize_offer_keep"),
                      announce=announce, **dialogs)


def ask_first_run(i18n):
    """The first-run question. Returns the chosen engine, or None if the user
    closed it without choosing."""
    from core import neonize_bridge_process as bridge

    offered = engines.first_run_engines(bridge.command(bridge.bridge_dir()) is not None)
    dialog = wx.SingleChoiceDialog(
        None, i18n.t("connection_engine_ask_message"),
        i18n.t("connection_engine_ask_title"),
        [i18n.t(engines.LABEL_KEYS[e]) for e in offered])
    dialog.SetSelection(0)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        selection = dialog.GetSelection()
        return offered[selection] if 0 <= selection < len(offered) else engines.WPPCONNECT
    finally:
        dialog.Destroy()
