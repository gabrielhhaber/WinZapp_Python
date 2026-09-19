"""Tests for adding a group member by typed phone number.

Reported live (2026-09-16): pressing "Adicionar número" in the add-member dialog
did nothing — nobody was added and no error was shown. The handler only
appended the number as a new row at the bottom of the contact list and selected
it, leaving the real add to the other button; nothing was announced and focus
stayed in the field, so from a screen reader there was no effect at all. The
button now performs the add itself, through the same path as the contact list.

AddMemberDialog is a wx.Dialog, so the handler is bound onto a plain stub whose
controls are tiny fakes — nothing here opens a window.
"""

import inspect

import wx

from ui.dialogs.add_member_dialog import AddMemberDialog, typed_number_to_jid


class TestTypedNumberToJid:
    def test_a_local_number_gets_the_country_code(self):
        assert typed_number_to_jid("51 99999-8888", "55") == "5551999998888@c.us"

    def test_a_number_already_carrying_the_code_is_left_alone(self):
        assert typed_number_to_jid("+55 (51) 99999-8888", "55") == "5551999998888@c.us"

    def test_brazilian_area_code_55_is_not_mistaken_for_the_country_code(self):
        """DDD 55 (Santa Maria, RS) starts with the same digits as Brazil's
        country code; 11 digits is a local number with its area code."""
        assert typed_number_to_jid("55 99999-8888", "55") == "5555999998888@c.us"
        assert typed_number_to_jid("55 9999-8888", "55") == "555599998888@c.us"

    def test_other_countries_use_the_prefix_rule(self):
        assert typed_number_to_jid("202 555 0123", "1") == "12025550123@c.us"
        assert typed_number_to_jid("351 912 345 678", "351") == "351912345678@c.us"

    def test_nothing_typed_is_not_a_number(self):
        assert typed_number_to_jid("", "55") == ""
        assert typed_number_to_jid("abc", "55") == ""

    def test_a_trunk_zero_is_dropped_before_the_country_code(self):
        assert typed_number_to_jid("051 99999-8888", "55") == "5551999998888@c.us"
        assert typed_number_to_jid("07911 123456", "44") == "447911123456@c.us"

    def test_italian_numbers_keep_their_zero(self):
        assert typed_number_to_jid("06 1234 5678", "39") == "390612345678@c.us"

    def test_too_short_or_too_long_is_not_a_number(self):
        assert typed_number_to_jid("123", "55") == ""
        assert typed_number_to_jid("1" * 20, "55") == ""


class _Field:
    def __init__(self, value=""):
        self.value = value
        self.focused = False

    def GetValue(self):
        return self.value

    def SetFocus(self):
        self.focused = True


class _Combo:
    def GetSelection(self):
        return 0


class _Button:
    def __init__(self):
        self.enabled = True

    def Disable(self):
        self.enabled = False

    def Enable(self):
        self.enabled = True


class _I18n:
    def t(self, key):
        return key


class _Stub:
    _on_add_typed_number = AddMemberDialog._on_add_typed_number
    _start_add = AddMemberDialog._start_add
    _finish = AddMemberDialog._finish

    def __init__(self, typed):
        self._i18n = _I18n()
        self._countries = [("Brasil", "55")]
        self._country_combo = _Combo()
        self._phone_field = _Field(typed)
        self._ok_btn = _Button()
        self._add_number_btn = _Button()
        self.added = []

    def _do_add(self, jids):  # the thread target, run inline below
        self.added.append(list(jids))


class _InlineThread:
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class TestAddTypedNumber:
    def test_the_button_actually_adds_the_number(self, monkeypatch):
        monkeypatch.setattr("ui.dialogs.add_member_dialog.threading.Thread", _InlineThread)
        stub = _Stub("51 99999-8888")
        stub._on_add_typed_number(None)
        assert stub.added == [["5551999998888@c.us"]]

    def test_only_the_typed_number_is_added(self):
        """The old handler selected the new row next to the contact that was
        pre-selected when the dialog opened, so the next "Add" took both."""
        src = inspect.getsource(AddMemberDialog._on_add_typed_number)
        assert "_contact_jids" not in src
        assert "Select(" not in src

    def test_both_buttons_are_disabled_while_the_add_runs(self, monkeypatch):
        started = []
        monkeypatch.setattr(
            "ui.dialogs.add_member_dialog.threading.Thread",
            lambda target, args=(), daemon=None: type("T", (), {"start": lambda self: started.append(args)})(),
        )
        stub = _Stub("51 99999-8888")
        stub._on_add_typed_number(None)
        assert started
        assert not stub._ok_btn.enabled and not stub._add_number_btn.enabled

    def test_a_failed_add_reenables_both_buttons(self, monkeypatch):
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **kw: wx.OK)
        stub = _Stub("51 99999-8888")
        stub._ok_btn.Disable()
        stub._add_number_btn.Disable()
        stub._finish(False, "boom")
        assert stub._ok_btn.enabled and stub._add_number_btn.enabled

    def test_focus_returns_to_the_button_after_a_failed_add(self, monkeypatch):
        """Disabling the focused button makes Windows move focus elsewhere,
        often onto Cancel, where the next Enter closes the dialog."""
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **kw: wx.OK)
        monkeypatch.setattr(
            "ui.dialogs.add_member_dialog.threading.Thread",
            lambda target, args=(), daemon=None: type("T", (), {"start": lambda self: None})(),
        )
        stub = _Stub("51 99999-8888")
        focused = _Field()
        monkeypatch.setattr("ui.dialogs.add_member_dialog._focused_window", lambda: focused)
        stub._on_add_typed_number(None)
        stub._finish(False, "boom")
        assert focused.focused

    def test_an_invalid_number_is_reported_not_ignored(self, monkeypatch):
        shown = []
        monkeypatch.setattr(wx, "MessageBox", lambda msg, *a, **kw: shown.append(msg))
        monkeypatch.setattr("ui.dialogs.add_member_dialog.threading.Thread", _InlineThread)
        stub = _Stub("12")
        stub._on_add_typed_number(None)
        assert shown == ["add_member_invalid_number"]
        assert stub.added == []
        assert stub._phone_field.focused


class TestSearchFieldLabel:
    def test_the_search_field_is_labelled_as_a_search(self):
        """NVDA names a control after the StaticText right before it; the list's
        "Selecionar um contato" label used to sit there.

        This ordering now lives in the shared ContactListPicker
        (contact_list_picker.py) rather than in AddMemberDialog._build_ui
        directly: both AddMemberDialog and AttachContactDialog build their
        contact list through it, so the same ordering guard covers both.
        """
        from ui.dialogs.contact_list_picker import ContactListPicker

        src = inspect.getsource(ContactListPicker.__init__)
        search_label = src.index('i18n.t("group_search_label")')
        field = src.index("self.search_field = wx.TextCtrl")
        list_label = src.index("i18n.t(list_label_key)")
        the_list = src.index("self.list = wx.ListCtrl")
        assert search_label < field < list_label < the_list
