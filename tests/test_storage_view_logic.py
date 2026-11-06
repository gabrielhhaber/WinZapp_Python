"""Unbound view behavior uses plain stubs; no wx window is constructed."""
from types import SimpleNamespace

import wx

from core.storage_manager.policy import Preferences
from core.storage_manager.types import Entry, Root, Snapshot
from ui.dialogs.storage_manager import StorageManagerDialog
from ui.dialogs.storage_manager_actions import StorageActionsMixin
from ui.dialogs.storage_presentation import entry_name, size_text


def field(value=0):
    return SimpleNamespace(GetSelection=lambda: value, SetSelection=lambda v: None,
                           GetValue=lambda: value, Enable=lambda v: None)


def test_filtering_invalidates_a_preview_but_keeps_identity_based_selection():
    prefs = Preferences(match_media=True)
    first = Entry(Root("media", "/fixture", "media", managed=True), "one", 10, 10,
                  (1, 1, 10, 10, 10), "media", reason="received", media_id="one")
    invalidations, renders = [], []
    stub = SimpleNamespace(service=SimpleNamespace(snapshot=Snapshot(entries=[first]),
        references_complete=True, preferences=prefs, effective_preferences=lambda: prefs, present=lambda e: e),
        invalidate_preview=lambda: invalidations.append(1), category=field(0), media_type=field(0),
        minimum=field("0"), days=field("0"), combine=field(0), scope=field(0), state=field(0),
        sort=field(0), search=field(""), _descending=True, selected={first.key},
        render_rows=lambda: renders.append(1), error=lambda key: None)
    StorageManagerDialog.filter_rows(stub)
    assert stub._filtered == [first] and stub.selected == {first.key}
    assert invalidations == renders == [1]


def test_space_toggles_a_checkbox_independently_of_keyboard_cursor():
    entry = SimpleNamespace(key="fixed-identity")
    calls = []
    stub = SimpleNamespace(files=SimpleNamespace(GetFocusedItem=lambda: 0), _rows=[entry],
        selected=set(), invalidate_preview=lambda: calls.append("preview"), render_rows=lambda: calls.append("render"))
    event = SimpleNamespace(GetKeyCode=lambda: wx.WXK_SPACE)
    StorageManagerDialog.file_key(stub, event)
    assert stub.selected == {"fixed-identity"}
    StorageManagerDialog.file_key(stub, event)
    assert stub.selected == set() and calls == ["preview", "render", "preview", "render"]


def test_close_invalidates_callbacks_before_requesting_cancellation():
    events = []
    stub = SimpleNamespace(_alive=True, _timer=SimpleNamespace(Stop=lambda: events.append("timer")),
        service=SimpleNamespace(jobs=SimpleNamespace(cancel=lambda: events.append(stub._alive))),
        EndModal=lambda code: events.append("modal"))
    StorageManagerDialog.close(stub)
    assert not stub._alive and events == ["timer", False, "modal"]


def test_rows_do_not_expose_raw_cache_paths_or_message_identifiers():
    translations = {"storage_private_item": "Item {number}", "storage_category_media": "Media",
                    "storage_item_label": "{name}; {category}; {number}", "decimal_separator": ","}
    i18n = SimpleNamespace(t=lambda key: translations[key])
    entry = Entry(Root("media", "/private/account", "media"), "SECRET_IDENTIFIER.wzmedia", 1536, 1,
                  (1, 2, 1536, 1, 1), "media")
    assert entry_name(entry, 2, i18n) == "Item 2; Media"
    assert size_text(1536, i18n) == "1,5 KiB"


def test_a_late_scan_callback_does_not_touch_a_closed_view():
    StorageActionsMixin.scanned(SimpleNamespace(_alive=False), None, None)


def test_privacy_reset_clears_all_named_collections_and_user_search():
    class Control:
        def __init__(self): self.value = "Synthetic private name"
        def Freeze(self): pass
        def Thaw(self): pass
        def DeleteAllItems(self): self.value = []
        def Set(self, value): self.value = value
        def ChangeValue(self, value): self.value = value
        def SetLabel(self, value): self.value = value
    controls = {key: Control() for key in ("files", "held_list", "search", "selection_status")}
    protected = Control()
    stub = SimpleNamespace(**controls, _alive=True, service=SimpleNamespace(visibility_epoch=2),
        selected={"private"}, _rows=["private"], _filtered=["private"], _held=["private"],
        invalidate_preview=lambda: None, protections=SimpleNamespace(clear_private_state=lambda: protected.Set([])),
        refresh_summary=lambda: None, say=lambda value: None,
        i18n=SimpleNamespace(t=lambda key: "{count}" if key == "storage_selected_count" else key))
    StorageManagerDialog.clear_private_state(stub)
    assert not stub._rows and not stub._filtered and not stub._held and not stub.selected
    assert controls["files"].value == controls["held_list"].value == protected.value == []
    assert controls["search"].value == "" and stub.privacy_epoch == 2


def test_held_media_purge_requires_current_matching_before_confirmation():
    from ui.dialogs.storage_manager_auxiliary import StorageAuxiliaryMixin
    errors = []
    def forbidden(*args, **kwargs):
        raise AssertionError("No confirmation or worker before matching consent")
    stub = SimpleNamespace(available=lambda: True,
        _held=[SimpleNamespace(original=SimpleNamespace(media_id="synthetic"))],
        held_list=SimpleNamespace(GetSelections=lambda: [0]),
        service=SimpleNamespace(preferences=Preferences(match_media=False),
                                jobs=SimpleNamespace(start=forbidden)),
        confirm=forbidden, error=errors.append)
    StorageAuxiliaryMixin.finish_held(stub, False)
    assert errors == ["storage_purge_matching_required"]
