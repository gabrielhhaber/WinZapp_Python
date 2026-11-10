"""Who may react to a channel's updates: read from the channel, chosen when
creating or editing it, sent only when it changes."""
import types

from core import channels as ch
import ui.dialogs.channels as dialogs


def node(value=None):
    thread = {"name": {"text": "Test"}}
    if value is not None:
        thread["settings"] = {"reaction_codes": {"value": value}}
    return {"id": "120363000000000001@newsletter", "thread_metadata": thread,
            "viewer_metadata": {"role": "OWNER"}}


def test_the_setting_is_read_from_the_channel():
    assert ch.parse_channel(node("ALL")).reactions == "ALL"
    assert ch.parse_channel(node("none")).reactions == "NONE"
    # unknown, or WhatsApp Web's Blocklist (no choice of its settings screen)
    assert ch.parse_channel(node()).reactions == ""
    assert ch.parse_channel(node("BLOCKLIST")).reactions == ""


def test_the_route_is_asked_with_the_choice(monkeypatch):
    posted = []
    monkeypatch.setattr(ch, "_post", lambda conn, endpoint, payload, code, timeout=30:
                        posted.append((endpoint, payload, code)) or {"status": "success"})
    ch.set_channel_reactions(("http://h", 1, "t"), "1@newsletter", "BASIC")
    assert posted == [("channels/reactions", {"id": "1@newsletter", "value": "BASIC"},
                       "channel_reactions_failed")]


def test_an_edit_sends_the_reactions_only_when_they_change(monkeypatch):
    sent = []
    monkeypatch.setattr(ch, "edit_channel", lambda *a: sent.append("edit"))
    monkeypatch.setattr(ch, "set_channel_reactions", lambda conn, jid, value: sent.append(value))
    channel = ch.parse_channel(node("ALL"))
    dialogs.edit_with_reactions(None, channel, "Test", "", "ALL")
    dialogs.edit_with_reactions(None, channel, "Test", "", "")       # nothing chosen
    dialogs.edit_with_reactions(None, channel, "Test", "", "NONE")
    assert sent == ["edit", "edit", "edit", "NONE"]


def test_a_new_channel_keeps_whatsapps_default_unless_another_is_chosen(monkeypatch):
    sent = []
    monkeypatch.setattr(ch, "create_channel", lambda *a: "9@newsletter")
    monkeypatch.setattr(ch, "set_channel_reactions", lambda conn, jid, value: sent.append((jid, value)))
    assert dialogs.create_with_reactions(None, "N", "", "ALL") == "9@newsletter"
    assert dialogs.create_with_reactions(None, "N", "", "BASIC") == "9@newsletter"
    assert sent == [("9@newsletter", "BASIC")]


def test_a_setting_refused_after_creating_is_not_a_failed_creation(monkeypatch):
    """The channel exists: saying "could not create" made the user create a
    second one."""
    monkeypatch.setattr(ch, "create_channel", lambda *a: "9@newsletter")

    def refuse(*a):
        raise ValueError("channel_reactions_failed")
    monkeypatch.setattr(ch, "set_channel_reactions", refuse)
    monkeypatch.setattr(ch, "set_channel_admin_profiles", lambda *a: None)
    created = dialogs.create_with_reactions(None, "N", "", "NONE", admin_profiles=True)
    assert created == "9@newsletter" and created.settings_saved is False
    fine = dialogs.create_with_reactions(None, "N", "", "ALL", admin_profiles=True)
    assert fine == "9@newsletter" and fine.settings_saved is True
    monkeypatch.setattr(ch, "create_channel", lambda *a: "")
    assert dialogs.create_with_reactions(None, "N", "", "NONE") == ""


def test_the_details_say_the_setting():
    i18n = types.SimpleNamespace(t=lambda key: {
        "channels_reactions_line": "Reactions: {setting}", "channels_reactions_basic": "Default emojis only",
    }.get(key, ""))
    text = dialogs.channel_details(ch.parse_channel(node("BASIC")), i18n, True)
    assert "Reactions: Default emojis only" in text
    assert "Reactions" not in dialogs.channel_details(ch.parse_channel(node()), i18n, True)


def ap_node(enabled=None):
    n = node("ALL")
    if enabled is not None:
        n["thread_metadata"]["settings"]["admin_profiles_enabled"] = enabled
    return n


def test_show_admin_profile_is_read_when_the_engine_says():
    assert ch.parse_channel(ap_node(True)).admin_profiles is True
    assert ch.parse_channel(ap_node(False)).admin_profiles is False
    assert ch.parse_channel(ap_node()).admin_profiles is None


def test_show_admin_profile_is_sent_only_when_set_and_different(monkeypatch):
    sent = []
    monkeypatch.setattr(ch, "edit_channel", lambda *a: None)
    monkeypatch.setattr(ch, "set_channel_reactions", lambda *a: None)
    monkeypatch.setattr(ch, "set_channel_admin_profiles", lambda conn, jid, on: sent.append(on))
    known, unknown = ch.parse_channel(ap_node(False)), ch.parse_channel(ap_node())
    dialogs.edit_with_reactions(None, known, "T", "", "", None)      # left undetermined
    dialogs.edit_with_reactions(None, known, "T", "", "", False)     # unchanged
    dialogs.edit_with_reactions(None, known, "T", "", "", True)
    dialogs.edit_with_reactions(None, unknown, "T", "", "", False)   # unknown before: sent
    assert sent == [True, False]


def test_the_admin_profiles_route(monkeypatch):
    posted = []
    monkeypatch.setattr(ch, "_post", lambda conn, endpoint, payload, code, timeout=30:
                        posted.append((endpoint, payload)) or {"status": "success"})
    ch.set_channel_admin_profiles(None, "1@newsletter", True)
    assert posted == [("channels/admin-profiles", {"id": "1@newsletter", "enabled": True})]
