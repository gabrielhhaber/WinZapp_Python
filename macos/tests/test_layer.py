"""The macOS layer, without ever showing a window (WinZapp rule 1: frames
are created but never Show()n, so nothing takes focus from VoiceOver).

Run from the repo root:  python3 -m pytest macos/tests -q
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client"), os.path.join(ROOT, ".pydeps")]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS layer")

import wx  # noqa: E402


@pytest.fixture(scope="module")
def app():
    import winzapp_mac
    winzapp_mac.install()
    a = wx.App(False)
    yield a


@pytest.fixture
def frame(app):
    f = wx.Frame(None)          # never shown
    yield f
    f.Destroy()


# ---------------------------------------------------------------- lists --

def _list(frame, rows=(("Alice", "hi"), ("Bob", "voice message 0:12"), ("Carol", ""))):
    lc = wx.ListCtrl(frame, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
    lc.InsertColumn(0, "name")
    lc.InsertColumn(1, "detail")
    for i, (a, b) in enumerate(rows):
        lc.InsertItem(i, a)
        lc.SetItem(i, 1, b)
    return lc


def test_listctrl_is_the_native_stand_in(frame):
    from winzapp_mac.listctrl import MacListCtrl
    assert isinstance(_list(frame), MacListCtrl)


def test_row_text_joins_non_empty_columns(frame):
    lc = _list(frame)
    assert [lc.GetString(i) for i in range(3)] == ["Alice, hi", "Bob, voice message 0:12", "Carol"]
    assert lc.GetItemText(1, 1) == "voice message 0:12"


def test_programmatic_focus_emits_focused_and_selected(frame):
    lc = _list(frame)
    seen = []
    lc.Bind(wx.EVT_LIST_ITEM_FOCUSED, lambda e: seen.append(("focused", e.GetIndex())))
    lc.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: seen.append(("selected", e.GetIndex())))
    lc.Focus(2)
    assert seen == [("focused", 2), ("selected", 2)]
    assert lc.GetFirstSelected() == 2


def test_insert_above_selection_keeps_the_same_row_selected(frame):
    lc = _list(frame)
    lc.Focus(1)
    lc.InsertItem(0, "Zed")
    assert lc.GetItemText(lc.GetFirstSelected()) == "Bob"


def test_item_data_survives_delete(frame):
    lc = _list(frame)
    for i in range(3):
        lc.SetItemData(i, 100 + i)
    lc.DeleteItem(0)
    assert [lc.GetItemData(i) for i in range(2)] == [101, 102]


def test_checkbox_rows_show_state(frame):
    lc = _list(frame)
    lc.EnableCheckBoxes(True)
    lc.CheckItem(0, True)
    assert lc.IsItemChecked(0) and lc.GetString(0).startswith("✓")
    lc.CheckItem(0, False)
    assert not lc.GetString(0).startswith("✓")


def test_row_labels_use_mac_shortcut_names(frame):
    lc = wx.ListCtrl(frame, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
    lc.InsertColumn(0, "x")
    lc.InsertItem(0, "Chats alt+1")
    assert lc.GetString(0) == "Chats Command-1"


# ---------------------------------------------------- VoiceOver rows ----

def _cell(lc, row):
    import objc
    table = objc.objc_object(c_void_p=lc.GetHandle()).documentView()
    return table.viewAtColumn_row_makeIfNecessary_(0, row, True)


def _menu_handler(frame, lc, events, destroy=True):
    def on_ctx(evt):
        row = lc.GetFirstSelected()
        m = wx.Menu()
        it = m.Append(wx.ID_ANY, "Reply\tCtrl+R")
        frame.Bind(wx.EVT_MENU, lambda e, r=row: events.append(("reply", r)), it)
        sub = wx.Menu()
        s1 = sub.Append(wx.ID_ANY, "Thumbs up")
        frame.Bind(wx.EVT_MENU, lambda e, r=row: events.append(("react", r)), s1)
        m.AppendSubMenu(sub, "React")
        frame.PopupMenu(m)
        if destroy:
            m.Destroy()          # what WinZapp's handlers do
    lc.Bind(wx.EVT_CONTEXT_MENU, on_ctx)


def test_rows_are_native_cells_with_the_row_text(frame):
    lc = _list(frame)
    cell = _cell(lc, 1)
    assert cell.className() == "WZRowCell"
    assert cell.accessibilityLabel() == "Bob, voice message 0:12"


def test_vo_space_activates_the_row(frame, app):
    lc = _list(frame)
    seen = []
    lc.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: seen.append(e.GetIndex()))
    assert _cell(lc, 2).accessibilityPerformPress()
    app.ProcessPendingEvents()
    wx.SafeYield()
    assert seen == [2]


def test_context_menu_items_become_voiceover_actions(frame):
    lc = _list(frame)
    _menu_handler(frame, lc, [])
    cell = _cell(lc, 1)
    assert cell.accessibilityCustomActions() == []      # not the focused row
    lc.Focus(1)
    assert [a.name() for a in cell.accessibilityCustomActions()] == ["Reply", "React: Thumbs up"]


def test_running_an_action_reaches_winzapps_handler(frame):
    from winzapp_mac import native_rows
    lc = _list(frame)
    events = []
    _menu_handler(frame, lc, events)
    lc.Focus(1)
    assert native_rows.run_menu_action(lc, "React: Thumbs up")
    assert events == [("react", 1)]


def test_reading_actions_leaves_no_handlers_behind(frame):
    from winzapp_mac import native_rows
    lc = _list(frame)
    events = []
    _menu_handler(frame, lc, events)
    lc.Focus(1)
    for _ in range(100):
        native_rows.menu_actions(lc)
    native_rows.run_menu_action(lc, "Reply")
    assert events == [("reply", 1)]


# ----------------------------------------------------------- audio ------

def test_microphone_recovers_after_coreaudio_restart(monkeypatch):
    from winzapp_mac import audio_mac
    calls = []

    def fake_open(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("Unanticipated host error", -9999)
        return "stream"

    monkeypatch.setattr(audio_mac._OrigPyAudio, "open", fake_open)
    monkeypatch.setattr(audio_mac.HealingPyAudio, "_reinitialise", lambda self: calls.append("reinit"))
    pa = audio_mac.HealingPyAudio.__new__(audio_mac.HealingPyAudio)
    pa._healed_since_success = False
    assert pa.open() == "stream"
    assert calls == [1, "reinit", 1]


def test_format_errors_do_not_trigger_a_rebuild(monkeypatch):
    from winzapp_mac import audio_mac

    def fake_open(self, *a, **k):
        raise OSError("Invalid sample rate", -9997)

    monkeypatch.setattr(audio_mac._OrigPyAudio, "open", fake_open)
    rebuilt = []
    monkeypatch.setattr(audio_mac.HealingPyAudio, "_reinitialise", lambda self: rebuilt.append(1))
    pa = audio_mac.HealingPyAudio.__new__(audio_mac.HealingPyAudio)
    pa._healed_since_success = False
    with pytest.raises(OSError):
        pa.open()
    assert rebuilt == []


# -------------------------------------------------------- lifecycle ----

class _StubWindow:
    def __init__(self):
        self.calls = []
        self._window_hidden = False

    def lock_chat_vault(self, **k):
        self.calls.append("lock")

    def Hide(self):
        self.calls.append("hide")

    def real_exit(self):
        self.calls.append("exit")


class _StubClose:
    def __init__(self, can_veto):
        self._can = can_veto
        self.vetoed = False

    def CanVeto(self):
        return self._can

    def Veto(self):
        self.vetoed = True


def test_closing_the_window_hides_it_and_keeps_running():
    from winzapp_mac.lifecycle_mac import on_close
    w, e = _StubWindow(), _StubClose(True)
    on_close(w, e)
    assert w.calls == ["lock", "hide"] and e.vetoed and w._window_hidden


def test_a_forced_close_still_exits():
    from winzapp_mac.lifecycle_mac import on_close
    w, e = _StubWindow(), _StubClose(False)
    on_close(w, e)
    assert w.calls == ["exit"]


# ------------------------------------------------------------ spelling --

@pytest.fixture(scope="module")
def warm_spell_server(app):
    """NSSpellServer starts cold on a fresh CI VM: its first answers time out
    ("findMisspelledWordInString timed out", measured on the macos-15-intel
    runner in three of four runs) and the checker, by design, then reports no
    errors. Give it time to come up. Nothing is skipped: if it never answers,
    the tests below still fail on their own assertions."""
    import time
    from core import spell_checker as sc
    c = sc.WindowsSpellChecker(language="en-US")
    for _ in range(10):
        if c.errors_for_text("this is a mispeled word"):
            break
        time.sleep(2)


def test_mac_spell_checker_finds_misspellings(app, warm_spell_server):
    from core import spell_checker as sc
    c = sc.WindowsSpellChecker(language="en-US")
    assert c.errors_for_text("this is a mispeled word") == [(10, 18)]


def test_finishing_a_misspelled_word_plays_the_cue_once(app, warm_spell_server):
    from core import spell_checker as sc
    cues = []
    c = sc.WindowsSpellChecker(language="en-US", on_error=lambda: cues.append(1))
    for ch in "I realy like it ":
        c.text_changed(c._last_text + ch)
    assert cues == [1]


def test_spell_checker_uses_the_winzapp_language(app):
    from core import spell_checker as sc
    c = sc.WindowsSpellChecker(language="pt-BR")
    c._get_checker()
    assert c.language.lower().startswith("pt")


# ------------------------------------------------------------- camera ---

AVF_LISTING = """\
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FHD User Facing
[AVFoundation indev @ 0x1] [1] OBS Virtual Camera
[AVFoundation indev @ 0x1] [2] Capture screen 0
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] Audient iD44
"""


def test_cameras_are_listed_without_screens_or_microphones():
    from winzapp_mac.camera_mac import camera_names
    assert camera_names(AVF_LISTING) == ["FHD User Facing", "OBS Virtual Camera"]


def test_capture_uses_avfoundation_video_only():
    from winzapp_mac.camera_mac import capture_command
    cmd = capture_command("ffmpeg", "FHD User Facing", "30")
    assert cmd[cmd.index("-f") + 1] == "avfoundation"
    assert "FHD User Facing:none" in cmd and "-an" in cmd
    assert cmd[cmd.index("-framerate") + 1] == "30"


# ------------------------------------------------------ regional dates --

def test_dates_follow_the_mac_regional_format():
    import datetime
    from winzapp_mac import platform_mac as pm
    from Foundation import NSDateFormatter, NSLocale
    f = NSDateFormatter.alloc().init()
    f.setLocale_(NSLocale.currentLocale())
    f.setDateStyle_(1)
    f.setTimeStyle_(0)
    d = datetime.datetime(2026, 9, 28, 16, 55)
    fmt = pm.mac_date_strftime()
    assert fmt and "%" in fmt
    # Same fields, same order as macOS's own short date.
    mac = str(f.stringFromDate_(__import__("Foundation").NSDate.dateWithTimeIntervalSince1970_(d.timestamp())))
    assert [int(p) for p in mac.replace(".", "/").replace("-", "/").split("/")] == \
        [int(p) for p in d.strftime(fmt).replace(".", "/").replace("-", "/").split("/")]


# -------------------------------------------------------------- focus ---

class _FakeStatus:
    def __init__(self, focused):
        self._f = focused

    def isFocused(self):
        return self._f


class _FakeCenter:
    def __init__(self, auth, focused):
        self._auth, self._focused = auth, focused

    def authorizationStatus(self):
        return self._auth

    def focusStatus(self):
        return _FakeStatus(self._focused)


def test_focus_counts_only_when_permitted(monkeypatch):
    from winzapp_mac import focus_mac
    monkeypatch.setattr(focus_mac, "_center", lambda: _FakeCenter(3, True))
    assert focus_mac.focus_active() is True
    monkeypatch.setattr(focus_mac, "_center", lambda: _FakeCenter(0, True))
    assert focus_mac.focus_active() is False          # not authorized: behave as before
    monkeypatch.setattr(focus_mac, "_center", lambda: _FakeCenter(3, None))
    assert focus_mac.focus_active() is False


def test_focus_drives_winzapps_quiet_hours(monkeypatch):
    from winzapp_mac import focus_mac
    from core import quiet_hours
    focus_mac.install()
    monkeypatch.setattr(focus_mac, "_center", lambda: _FakeCenter(3, True))
    quiet_hours.invalidate_cache()
    assert quiet_hours.is_quiet_hours_active() is True
    monkeypatch.setattr(focus_mac, "_center", lambda: _FakeCenter(3, False))
    quiet_hours.invalidate_cache()
    assert quiet_hours.is_quiet_hours_active() is False


# ------------------------------------------------------- label texts ----

def test_a_label_that_names_a_control_is_not_a_separate_voiceover_stop(frame):
    import objc
    from winzapp_mac import accessibility_mac as am
    panel = wx.Panel(frame)
    label = wx.StaticText(panel, label="Search chats")
    field = wx.TextCtrl(panel)
    info = wx.StaticText(panel, label="Some information")
    am._apply(field)
    assert objc.objc_object(c_void_p=field.GetHandle()).accessibilityLabel() == "Search chats"
    cell = lambda w: objc.objc_object(c_void_p=w.GetHandle()).cell()
    assert not cell(label).isAccessibilityElement()
    assert cell(info).isAccessibilityElement()


# ------------------------------------------------------- pop-up buttons --

def test_readonly_combo_is_a_popup_button_that_still_talks_combo(frame):
    events = []
    combo = wx.ComboBox(frame, value="Two", choices=["One", "Two", "Three"], style=wx.CB_READONLY)
    assert isinstance(combo, wx.Choice) and isinstance(combo, wx.ComboBox)
    assert combo.GetValue() == "Two"
    combo.Bind(wx.EVT_COMBOBOX, lambda e: events.append(("combo", e.GetString())))
    combo.Bind(wx.EVT_TEXT, lambda e: events.append(("text", e.GetString())))
    combo.SetSelection(2)
    evt = wx.CommandEvent(wx.wxEVT_CHOICE, combo.GetId())
    evt.SetEventObject(combo)
    combo.GetEventHandler().ProcessEvent(evt)          # what a user's pick sends
    assert events == [("combo", "Three"), ("text", "Three")]
    combo.SetValue("One")
    assert combo.GetSelection() == 0


def test_editable_combo_stays_a_combo(frame):
    combo = wx.ComboBox(frame, choices=["a"])
    assert not isinstance(combo, wx.Choice)


# ------------------------------------------------------ notifications ---

def test_every_usernotifications_call_in_notify_mac_exists():
    """A misspelt pyobjc selector fails only at run time, inside a try that
    falls back to speech — notifications silently never appeared once."""
    import re
    import UserNotifications as UN
    src = open(os.path.join(ROOT, "macos", "winzapp_mac", "notify_mac.py"), encoding="utf-8").read()
    objs = {"center": UN.UNUserNotificationCenter, "content": UN.UNMutableNotificationContent,
            "UN.UNNotificationRequest": UN.UNNotificationRequest,
            "UN.UNTextInputNotificationAction": UN.UNTextInputNotificationAction,
            "UN.UNNotificationAction": UN.UNNotificationAction,
            "UN.UNNotificationCategory": UN.UNNotificationCategory,
            "_center": UN.UNUserNotificationCenter}
    checked = 0
    for obj, name in re.findall(r"([A-Za-z_.]+)\.([a-zA-Z]+_[A-Za-z_]*)\(", src):
        if obj in objs:
            assert hasattr(objs[obj], name), f"{obj}.{name} does not exist"
            checked += 1
    assert checked >= 8


def test_a_failed_regional_format_is_logged_and_tried_again(caplog):
    from winzapp_mac import platform_mac as pm
    answers = [RuntimeError("no formatter"), None, "%d/%m/%Y", "never asked"]

    @pm._cached_success
    def fmt():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert fmt() is None and "no formatter" in caplog.text
    caplog.clear()
    assert fmt() is None            # no pattern: not cached either
    assert fmt() == "%d/%m/%Y"
    assert fmt() == "%d/%m/%Y"      # a success is cached
    assert answers == ["never asked"]


def test_a_persistent_formatter_failure_is_logged_once(caplog):
    from winzapp_mac import platform_mac as pm

    @pm._cached_success
    def fmt():
        raise RuntimeError("always broken")

    for _ in range(5):
        assert fmt() is None
    assert caplog.text.count("[platform_mac]") == 1


# ---------------------------------------------------------- stale node ---

def test_only_the_runtimes_own_node_is_stopped(tmp_path):
    import shutil
    import subprocess
    import time
    from winzapp_mac import paths_mac
    node = str(tmp_path / "node")
    shutil.copy("/bin/sleep", node)
    real = subprocess.Popen([node, "30"])
    # Mentions the runtime path on its command line but runs something else.
    decoy = subprocess.Popen(["/bin/sh", "-c", "sleep 30", node])
    try:
        time.sleep(0.3)
        assert paths_mac.runtime_node_pids(node) == [real.pid]
        paths_mac._stop_stale_node(node, grace=5)
        assert real.wait(timeout=5) is not None
        assert decoy.poll() is None
    finally:
        for proc in (real, decoy):
            if proc.poll() is None:
                proc.kill()
                proc.wait()


# ----------------------------------------------------- message-box log ---

def test_the_message_box_log_never_records_the_text():
    from winzapp_mac import diagnostics_mac
    entry = diagnostics_mac.persistent_entry(
        "WinZapp", "Delete the chat with Maria Silva (5511987654321@s.whatsapp.net)?",
        "  File connect.py, line 1\n", when="2026-09-30 15:00:00")
    assert "Maria" not in entry and "5511987654321" not in entry
    assert entry.startswith("2026-09-30 15:00:00 'WinZapp' | 64 characters")
    assert "connect.py" in entry


def test_the_message_box_log_is_capped(tmp_path):
    from winzapp_mac import diagnostics_mac
    path = str(tmp_path / "message-boxes.log")
    with open(path, "w") as fh:
        fh.write("x" * 100)
    diagnostics_mac._rotate(path, max_bytes=1000)
    assert not (tmp_path / "message-boxes.log.1").exists()
    with open(path, "w") as fh:
        fh.write("x" * 2000)
    diagnostics_mac._rotate(path, max_bytes=1000)
    assert (tmp_path / "message-boxes.log.1").exists() and not (tmp_path / "message-boxes.log").exists()


def test_home_and_end_move_to_the_list_edges():
    import wx
    from winzapp_mac.listctrl import edge_row

    assert edge_row(wx.WXK_HOME, 5, 10) == 0
    assert edge_row(wx.WXK_END, 5, 10) == 9
    assert edge_row(wx.WXK_NUMPAD_END, -1, 10) == 9


def test_home_on_the_first_row_stays_load_older():
    import wx
    from winzapp_mac.listctrl import edge_row

    assert edge_row(wx.WXK_HOME, 0, 10) is None
    assert edge_row(wx.WXK_END, 9, 10) is None
    assert edge_row(wx.WXK_HOME, -1, 0) is None
    assert edge_row(wx.WXK_DOWN, 3, 10) is None
