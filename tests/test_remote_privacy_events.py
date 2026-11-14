from types import SimpleNamespace as N

from core import remote_privacy_events as events
from core.whatsapp_privacy import PRIVACY_FIELDS, option_index


class Choice:
    def __init__(self, index): self.index = index
    def GetSelection(self): return self.index
    def SetSelection(self, index): self.index = index


class Panel:
    def __init__(self, mw):
        self.main_window = mw
        self._loaded = {field.setting: field.options[0][0] for field in PRIVACY_FIELDS}
        self._choices = {field.setting: Choice(0) for field in PRIVACY_FIELDS}


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


def test_unloaded_or_closed_panel_and_shutdown_are_untouched():
    panel=Panel(N());panel._loaded=None
    events.update_panel(panel,{"about":"none"})
    events.apply_privacy_event(N(_shutting_down=True),{"about":"none"})
