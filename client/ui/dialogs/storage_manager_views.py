"""Standard native controls for storage; no custom-drawn or colour-only state."""
import wx

from core.storage_manager.types import CATEGORIES
from .storage_options import StorageOptionsPanel
from .storage_protections import StorageProtectionsPanel
from .storage_presentation import category_text
from .storage_rules import MEDIA_TYPES, TYPE_KEYS, StorageRulesPanel
from .storage_layout import note


def button(parent, sizer, owner, key, callback):
    control = wx.Button(parent, label=owner.i18n.t(key))
    control.Bind(wx.EVT_BUTTON, callback)
    sizer.Add(control, 0, wx.ALL, 4)
    return control


def build(owner):
    outer = wx.BoxSizer(wx.VERTICAL)
    owner.summary = wx.TextCtrl(owner, style=wx.TE_MULTILINE | wx.TE_READONLY,
                               size=(-1, 145), name=owner.i18n.t("storage_summary"))
    outer.Add(owner.summary, 0, wx.EXPAND | wx.ALL, 8)
    actions = wx.WrapSizer(wx.HORIZONTAL)
    owner.scan_button = button(owner, actions, owner, "storage_scan", owner.start_scan)
    owner.stop_button = button(owner, actions, owner, "storage_stop", owner.stop)
    owner.stop_button.Disable()
    outer.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
    owner.book = wx.Notebook(owner)
    files = wx.ScrolledWindow(owner.book)
    files.SetScrollRate(0, 16)
    make_files(files, owner)
    owner.book.AddPage(files, owner.i18n.t("storage_files_tab"))
    owner.rules = StorageRulesPanel(owner.book, owner)
    owner.book.AddPage(owner.rules, owner.i18n.t("storage_rules_tab"))
    owner.options = StorageOptionsPanel(owner.book, owner)
    owner.book.AddPage(owner.options, owner.i18n.t("storage_options_tab"))
    owner.protections = StorageProtectionsPanel(owner.book, owner)
    owner.book.AddPage(owner.protections, owner.i18n.t("storage_protections_tab"))
    held = wx.Panel(owner.book)
    make_held(held, owner)
    owner.book.AddPage(held, owner.i18n.t("storage_held_tab"))
    history = wx.Panel(owner.book)
    box = wx.BoxSizer(wx.VERTICAL)
    owner.history = wx.TextCtrl(history, style=wx.TE_MULTILINE | wx.TE_READONLY,
                                name=owner.i18n.t("storage_history_tab"))
    box.Add(owner.history, 1, wx.EXPAND | wx.ALL, 8)
    row = wx.WrapSizer(wx.HORIZONTAL)
    button(history, row, owner, "storage_refresh_history", owner.refresh_history)
    button(history, row, owner, "storage_clear_history", owner.clear_history)
    box.Add(row, 0, wx.EXPAND | wx.ALL, 4)
    history.SetSizer(box)
    owner.book.AddPage(history, owner.i18n.t("storage_history_tab"))
    outer.Add(owner.book, 1, wx.EXPAND | wx.ALL, 8)
    owner.status = wx.StaticText(owner, label="")
    outer.Add(owner.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
    close = wx.Button(owner, wx.ID_CLOSE, owner.i18n.t("close").replace("&", ""))
    close.Bind(wx.EVT_BUTTON, owner.close)
    outer.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
    owner.SetSizer(outer)


def make_files(panel, owner):
    sizer = wx.BoxSizer(wx.VERTICAL)
    filters = wx.WrapSizer(wx.HORIZONTAL)
    owner.category = wx.Choice(panel, choices=[owner.i18n.t("storage_all_categories")]
                               + [category_text(c, owner.i18n) for c in CATEGORIES],
                               name=owner.i18n.t("storage_category"))
    owner.category.SetSelection(0)
    filters.Add(owner.category, 0, wx.ALL, 4)
    owner.media_type = wx.Choice(panel, choices=[owner.i18n.t(key) for key in TYPE_KEYS],
                                 name=owner.i18n.t("storage_media_type"))
    owner.media_type.SetSelection(0)
    filters.Add(owner.media_type, 0, wx.ALL, 4)
    for attr, key, choices in (
        ("scope", "storage_filter_scope", ("storage_target_scope_all", "storage_protect_scope_account",
            "storage_protect_scope_shared", "storage_scope_program", "storage_scope_external", "storage_scope_other_accounts")),
        ("state", "storage_state", ("storage_state_all", "storage_state_eligible", "storage_state_protected")),
        ("sort", "storage_sort", ("storage_size", "storage_modified", "storage_category"))):
        control = wx.Choice(panel, choices=[owner.i18n.t(k) for k in choices], name=owner.i18n.t(key))
        control.SetSelection(0)
        control.Bind(wx.EVT_CHOICE, owner.filter_rows)
        setattr(owner, attr, control)
        filters.Add(control, 0, wx.ALL, 4)
    owner.search = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER, name=owner.i18n.t("storage_search"))
    filters.Add(wx.StaticText(panel, label=owner.i18n.t("storage_search")), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
    filters.Add(owner.search, 0, wx.ALL, 4)
    for attr, key in (("minimum", "storage_filter_minimum"), ("days", "storage_filter_days")):
        label = owner.i18n.t(key)
        filters.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
        control = wx.TextCtrl(panel, value="0", size=(80, -1), name=label.replace("&", ""))
        setattr(owner, attr, control)
        filters.Add(control, 0, wx.ALL, 4)
    owner.combine = wx.Choice(panel, choices=[owner.i18n.t("storage_match_all"), owner.i18n.t("storage_match_any")],
                              name=owner.i18n.t("storage_rule_combine"))
    owner.combine.SetSelection(0)
    filters.Add(owner.combine, 0, wx.ALL, 4)
    sizer.Add(filters, 0, wx.EXPAND)
    row = wx.WrapSizer(wx.HORIZONTAL)
    button(panel, row, owner, "storage_filter_apply", owner.filter_rows)
    button(panel, row, owner, "storage_reverse_order", owner.reverse_order)
    button(panel, row, owner, "storage_select_visible", owner.select_visible)
    button(panel, row, owner, "storage_clear_selection", owner.clear_selection)
    button(panel, row, owner, "storage_select_suggested", owner.select_suggested)
    button(panel, row, owner, "storage_open_models", lambda e: owner.open_owner("models"))
    button(panel, row, owner, "storage_open_backups", lambda e: owner.open_owner("backups"))
    sizer.Add(row, 0, wx.EXPAND)
    owner.files = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
                              name=owner.i18n.t("storage_files_tab"))
    for index, key in enumerate(("storage_item", "storage_size", "storage_state", "storage_modified")):
        owner.files.InsertColumn(index, owner.i18n.t(key).replace("&", ""), width=245 if index == 0 else 150)
    owner.files.EnableCheckBoxes(True)
    owner.files.Bind(wx.EVT_KEY_DOWN, owner.file_key)
    owner.files.Bind(wx.EVT_LIST_ITEM_CHECKED, owner.check_changed)
    owner.files.Bind(wx.EVT_LIST_ITEM_UNCHECKED, owner.check_changed)
    sizer.Add(owner.files, 1, wx.EXPAND | wx.ALL, 5)
    owner.selection_status = wx.StaticText(panel, label="")
    sizer.Add(owner.selection_status, 0, wx.ALL, 5)
    row = wx.WrapSizer(wx.HORIZONTAL)
    button(panel, row, owner, "storage_previous_page", lambda e: owner.page_by(-1))
    button(panel, row, owner, "storage_next_page", lambda e: owner.page_by(1))
    button(panel, row, owner, "storage_details", owner.details)
    button(panel, row, owner, "storage_protect", lambda e: owner.protect(True))
    button(panel, row, owner, "storage_unprotect", lambda e: owner.protect(False))
    button(panel, row, owner, "storage_protect_chats", lambda e: owner.protect(True, chats=True))
    button(panel, row, owner, "storage_unprotect_chats", lambda e: owner.protect(False, chats=True))
    sizer.Add(row, 0, wx.EXPAND)
    row = wx.WrapSizer(wx.HORIZONTAL)
    owner.method = wx.Choice(panel, choices=[owner.i18n.t("storage_method_hold"), owner.i18n.t("storage_method_remove")],
                             name=owner.i18n.t("storage_method"))
    owner.method.SetSelection(0)
    row.Add(owner.method, 0, wx.ALL, 4)
    button(panel, row, owner, "storage_preview", owner.preview)
    owner.apply_button = button(panel, row, owner, "storage_apply_plan", owner.apply_plan)
    owner.apply_button.Disable()
    sizer.Add(row, 0, wx.EXPAND)
    owner.preview_text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY,
                                    size=(-1, 75), name=owner.i18n.t("storage_preview"))
    sizer.Add(owner.preview_text, 0, wx.EXPAND | wx.ALL, 5)
    sizer.Add(note(panel, owner.i18n.t("storage_owner_note")), 0, wx.EXPAND | wx.ALL, 5)
    panel.SetSizer(sizer)


def make_held(panel, owner):
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(note(panel, owner.i18n.t("storage_held_note")), 0, wx.EXPAND | wx.ALL, 8)
    owner.held_list = wx.ListBox(panel, style=wx.LB_EXTENDED, name=owner.i18n.t("storage_held_tab"))
    sizer.Add(owner.held_list, 1, wx.EXPAND | wx.ALL, 8)
    row = wx.WrapSizer(wx.HORIZONTAL)
    button(panel, row, owner, "storage_refresh_held", owner.refresh_held)
    button(panel, row, owner, "storage_next_held", owner.next_held)
    button(panel, row, owner, "storage_restore_held", lambda e: owner.finish_held(True))
    button(panel, row, owner, "storage_purge_held", lambda e: owner.finish_held(False))
    button(panel, row, owner, "storage_retire_tokens", owner.retire_tokens)
    sizer.Add(row, 0, wx.EXPAND | wx.ALL, 4)
    panel.SetSizer(sizer)
