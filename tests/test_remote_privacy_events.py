from types import SimpleNamespace as N

from core import remote_privacy_events as events
from core.whatsapp_privacy import PRIVACY_FIELDS, StatusPrivacy, option_index, status_option_index


class Choice:
    def __init__(self, index): self.index = index
    def GetSelection(self): return self.index
    def SetSelection(self, index): self.index = index
    def Enable(self, *_args): pass


class Check:
    def __init__(self, value): self.value = value
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value


class Panel:
    def __init__(self, mw):
        self.main_window = mw
        self._loaded = {field.setting: field.options[0][0] for field in PRIVACY_FIELDS}
        self._choices = {field.setting: Choice(0) for field in PRIVACY_FIELDS}
        self._keep_loaded = True
        self._keep_archived_check = Check(True)
        self._status_loaded = StatusPrivacy("contact", ())
        self._status_choice = Choice(status_option_index("contact"))
        self._status_people = None
        self._status_summary = type("Label", (), {"SetLabel": lambda *a: None, "Wrap": lambda *a: None})()
        self._status_people_button = type("Control", (), {"Enable": lambda *a: None})()
        self._i18n = type("I18n", (), {"t": lambda _s, key: key})()
    def _refresh_status_controls(self): pass


def test_remote_values_update_untouched_choices_but_preserve_user_edits():
    mw = N(token="account")
    panel = Panel(mw)
    events.register_panel(panel)
    panel._choices["about"].SetSelection(1)  # user preparing a local choice
    events.apply_privacy_event(mw, {"about": "none", "readReceipts": "none"})
    assert panel._loaded["about"] == panel._loaded["readReceipts"] == "none"
    assert panel._choices["about"].GetSelection() == 1
    assert panel._choices["readReceipts"].GetSelection() == 1


def test_only_observations_after_a_read_started_override_its_response():
    mw = N(token="account")
    events.apply_privacy_event(mw, {"about": "none"})
    request_revision = events.event_state(mw)["revision"]
    events.apply_privacy_event(mw, {"online": "match_last_seen"})
    assert events.observations_since(mw, request_revision) == {"online": "match_last_seen"}


def test_old_account_panels_and_queued_events_cannot_follow_new_account(monkeypatch):
    mw = N(token="first")
    panel = Panel(mw)
    events.register_panel(panel)
    queued = []
    monkeypatch.setattr(events.wx, "CallAfter", lambda fn,*a: queued.append((fn,a)))
    client = N(main_window=mw, _belongs_to_this_session=lambda d:d.get("session")=="ours")
    events.on_privacy_event(client, {"session":"ours","settings":{"about":"none"}})
    mw.token="second"
    fn,args=queued.pop();fn(*args)
    assert panel._loaded["about"]=="all"
    assert events.event_state(mw)["fields"]=={}
    events.apply_privacy_event(mw,{"about":"none"})
    assert panel._loaded["about"]=="all"


def test_unknown_values_and_other_accounts_are_ignored(monkeypatch):
    queued=[]
    monkeypatch.setattr(events.wx,"CallAfter",lambda fn,*a:queued.append(a))
    client=N(main_window=N(),_belongs_to_this_session=lambda d:d.get("session")=="ours")
    for data in [{"session":"other","settings":{"about":"none"}},
                 {"session":"ours","settings":{"online":"none","readReceipts":False}},
                 {"session":"ours","settings":{"about":"none","unknown":"all"}}]:
        events.on_privacy_event(client,data)
    assert len(queued)==1 and queued[0][1]=={"about":"none"}


def test_remote_keep_archived_updates_only_an_untouched_checkbox_and_tracks_revision():
    mw = N(token="account")
    panel = Panel(mw)
    events.register_panel(panel)
    panel._keep_archived_check.SetValue(False)  # user has a pending local edit
    events.apply_privacy_event(mw, {}, keep_archived=True)
    assert panel._keep_loaded is True
    assert panel._keep_archived_check.GetValue() is False
    revision = events.event_state(mw)["revision"]
    events.apply_privacy_event(mw, {}, keep_archived=False)
    assert events.keep_archived_since(mw, revision) is False
    assert events.keep_archived_for_load(mw, revision, True) is False
    assert events.keep_archived_for_load(mw, events.event_state(mw)["revision"], True) is True


def test_remote_keep_archived_for_old_account_is_dropped(monkeypatch):
    queued = []
    monkeypatch.setattr(events.wx, "CallAfter", lambda fn, *a: queued.append((fn, a)))
    mw = N(token="first")
    client = N(main_window=mw, _belongs_to_this_session=lambda d: d.get("session") == "ours")
    events.on_privacy_event(client, {"session": "ours", "keepArchived": False})
    mw.token = "second"
    fn, args = queued.pop()
    fn(*args)
    assert events.event_state(mw)["keep_archived"] is None


def test_remote_status_privacy_updates_untouched_audience_and_keeps_pending_draft():
    mw = N(token="account")
    panel = Panel(mw)
    events.register_panel(panel)
    status = {"setting": "deny-list", "list": ["1234567890@c.us", "123456@lid"]}
    events.apply_privacy_event(mw, {}, status=status)
    assert panel._status_loaded == StatusPrivacy("deny-list", ("1234567890@c.us", "123456@lid"))
    assert panel._status_choice.GetSelection() == status_option_index("deny-list")

    panel._status_choice.SetSelection(status_option_index("allow-list"))
    panel._status_people = ["111111@c.us"]
    newer = {"setting": "allow-list", "list": ["222222@lid"]}
    events.apply_privacy_event(mw, {}, status=newer)
    assert panel._status_loaded == StatusPrivacy("allow-list", ("222222@lid",))
    assert panel._status_choice.GetSelection() == status_option_index("allow-list")
    assert panel._status_people == ["111111@c.us"]


def test_status_event_revision_overrides_slow_read_and_invalid_lists_are_ignored(monkeypatch):
    mw = N(token="account")
    request_revision = events.event_state(mw)["revision"]
    queued = []
    monkeypatch.setattr(events.wx, "CallAfter", lambda fn, *a: queued.append((fn, a)))
    client = N(main_window=mw, _belongs_to_this_session=lambda d: d.get("session") == "ours")
    client_data = {"session": "ours", "status": {"setting": "allow-list", "list": ["123456@lid"]}}
    events.on_privacy_event(client, client_data)
    assert len(queued) == 1
    fn, args = queued.pop()
    fn(*args)
    assert events.status_since(mw, request_revision) == {
        "setting": "allow-list", "list": ["123456@lid"]}
    assert events.normalize_status({"setting": "allow-list", "list": ["group@g.us"]}) is None
    assert events.normalize_status({"setting": "contact", "list": ["123456@lid"]}) is None


def test_unloaded_or_closed_panel_and_shutdown_are_untouched():
    panel=Panel(N());panel._loaded=None
    events.update_panel(panel,{"about":"none"})
    events.apply_privacy_event(N(_shutting_down=True),{"about":"none"})


def test_malformed_audience_members_do_not_reset_the_current_audience():
    for member in [None, False, 42, {}, [], "123456@g.us", "invalid"]:
        assert events.normalize_status({"setting":"allow-list", "list":[member]}) is None
    assert events.normalize_status({"setting": [], "list": []}) is None
