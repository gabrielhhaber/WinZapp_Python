"""Inspectable keep-lists, including protections whose file is now absent."""
import wx

from core.storage_manager.types import CATEGORIES
from .storage_presentation import category_text, entry_name


class StorageProtectionsPanel(wx.ScrolledWindow):
    def __init__(self, parent, owner):
        super().__init__(parent)
        self.owner, self.rows = owner, []
        self.SetScrollRate(0, 16)
        t = owner.i18n.t
        box = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(self, label=t("storage_protection_note"))
        note.Wrap(650)
        box.Add(note, 0, wx.EXPAND | wx.ALL, 8)
        self.scope = wx.Choice(self, choices=[t("storage_protect_scope_account"), t("storage_protect_scope_shared")],
                               name=t("storage_protect_scope"))
        self.scope.SetSelection(0)
        box.Add(self.scope, 0, wx.EXPAND | wx.ALL, 6)
        self.category = wx.Choice(self, choices=[category_text(c, owner.i18n) for c in CATEGORIES],
                                  name=t("storage_category"))
        self.category.SetSelection(0)
        box.Add(self.category, 0, wx.EXPAND | wx.ALL, 6)
        self.list = wx.ListBox(self, style=wx.LB_EXTENDED, name=t("storage_protections_tab"))
        box.Add(self.list, 1, wx.EXPAND | wx.ALL, 6)
        row = wx.WrapSizer(wx.HORIZONTAL)
        for key, handler in (("storage_protect_category", self.add_category),
                             ("storage_refresh_protections", self.refresh),
                             ("storage_remove_protection", self.remove)):
            button = wx.Button(self, label=t(key))
            button.Bind(wx.EVT_BUTTON, handler)
            row.Add(button, 0, wx.ALL, 4)
        box.Add(row, 0, wx.EXPAND)
        self.SetSizer(box)

    def add_category(self, event=None):
        owner = self.owner
        if not owner.available():
            return
        category = CATEGORIES[max(0, self.category.GetSelection())]
        shared = self.scope.GetSelection() == 1
        def work(cancel):
            if cancel() or not owner.service.valid():
                raise ValueError("scope changed")
            owner.service.change_protection("protected_categories", {category}, True, shared=shared)
        owner.service.jobs.start(work, self.changed)

    def changed(self, value, error):
        owner = self.owner
        if not owner._alive:
            return
        owner.invalidate_preview()
        owner.render_rows()
        if error:
            owner.error("storage_preferences_invalid")
        else:
            self.refresh()

    def refresh(self, event=None):
        owner = self.owner
        if not owner.available():
            return
        def work(cancel):
            return [(shared, field, value) for shared, store in
                    ((False, owner.service.store), (True, owner.service.shared_store))
                    for field in ("protected", "protected_chats", "protected_categories")
                    for value in sorted(getattr(store.preferences(strict=True), field))]
        def done(rows, error):
            if not owner._alive:
                return
            if error:
                owner.error("storage_preferences_invalid")
                return
            self.rows = rows
            snapshot = owner.service.snapshot
            entries = snapshot.entries if snapshot is not None else []
            names = {e.key: entry_name(e, n + 1, owner.i18n) for n, e in enumerate(entries)}
            chats = {e.chat_key: e.display_name for e in entries if e.chat_key and e.display_name}
            labels = []
            for n, (shared, field, value) in enumerate(rows):
                scope = owner.i18n.t("storage_protect_scope_shared" if shared else "storage_protect_scope_account")
                if field == "protected_categories":
                    label = category_text(value, owner.i18n)
                else:
                    label = (chats if field == "protected_chats" else names).get(value)
                    label = label or owner.i18n.t("storage_private_item").format(number=n + 1)
                labels.append(owner.i18n.t("storage_protection_row").format(scope=scope, name=label))
            self.list.Freeze()
            try:
                self.list.Set(labels)
            finally:
                self.list.Thaw()
            owner.say(owner.i18n.t("storage_protection_count").format(count=len(rows)))
        owner.service.jobs.start(work, done)

    def remove(self, event=None):
        owner = self.owner
        rows = [self.rows[n] for n in self.list.GetSelections() if 0 <= n < len(self.rows)]
        if not owner.available() or not rows:
            return
        if not owner.confirm("storage_remove_protection_confirm", count=len(rows)):
            return
        def work(cancel):
            for shared, field, value in rows:
                if cancel() or not owner.service.valid():
                    break
                owner.service.change_protection(field, {value}, False, shared=shared)
        owner.service.jobs.start(work, self.changed)
