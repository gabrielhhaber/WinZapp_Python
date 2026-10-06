"""A forward that worked says so.

Forwarding runs in the background and its copy lands in another chat, so a
successful forward gave no feedback at all: only a failure was announced, and
the user could not tell "sent" from "nothing happened yet". It now ends with a
message box — one for the whole batch, never one per message or per target.

The worker used to be a closure inside the picker dialog (_on_menu_forward),
out of reach of a test; it is ConversationsPanel._forward_batch() now, and the
choice of what to say is the pure forward_outcome().
"""

import wx

from ui.conversation_panel import forwarding
from ui.conversation_panel.forwarding import forward_outcome
from ui.conversations import ConversationsPanel


class _I18n:
    def t(self, key):
        return {"forward_failed_multiple": "failed {names}",
                "forward_done_multiple": "done {count}"}.get(key, key)


class TestWhatIsSaid:
    def test_one_message(self):
        assert forward_outcome(_I18n(), 1, [], 1) == ("done", "forward_done")

    def test_one_message_to_several_chats_is_still_one_message(self):
        assert forward_outcome(_I18n(), 1, [], 3) == ("done", "forward_done")

    def test_several_messages_say_how_many(self):
        assert forward_outcome(_I18n(), 4, [], 2) == ("done", "done 4")

    def test_a_failure_to_the_only_target(self):
        assert forward_outcome(_I18n(), 1, ["Alice"], 1) == ("failed", "forward_failed")

    def test_a_failure_among_several_targets_names_them(self):
        assert forward_outcome(_I18n(), 2, {"Bob", "Alice"}, 3) == ("failed", "failed Alice, Bob")

    def test_any_failure_wins_over_the_confirmation(self):
        """Two of three went through: the user must hear about the third,
        not "done"."""
        assert forward_outcome(_I18n(), 3, ["Bob"], 2)[0] == "failed"

    def test_nothing_attempted_says_nothing(self):
        assert forward_outcome(_I18n(), 0, [], 1) == (None, "")


class _Sound:
    def __init__(self):
        self.played = 0

    def play(self):
        self.played += 1


class _MainWindow:
    def __init__(self):
        self.i18n = _I18n()
        self.error_sound = _Sound()
        self.spoken = []

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Panel:
    _forward_batch = ConversationsPanel._forward_batch
    _FORWARD_GAP_SECONDS = 0

    def __init__(self, failing=()):
        self.main_window = _MainWindow()
        self.conversation = {"remoteJid": "open@g.us"}
        self.failing = set(failing)
        self.sent = []

    def _forward_message_to_targets(self, msg, targets, keep_caption=False,
                                    source_jid_override=""):
        self.sent.append((msg["key"]["id"], source_jid_override, keep_caption))
        return [name for jid, name in targets if jid in self.failing]


def _msg(mid, **message):
    return {"key": {"id": mid, "remoteJid": "source@g.us"}, "message": message}


TARGETS = [("a@s.whatsapp.net", "Alice"), ("b@s.whatsapp.net", "Bob")]


class TestTheBatch:
    def _run(self, monkeypatch, panel, msgs, targets=TARGETS, keep_captions=False):
        boxes = []

        def _box(main_window, text, title, style, announce=None):
            boxes.append((text, title, style, main_window))
            self.announce = announce
            return wx.OK

        monkeypatch.setattr(forwarding.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        monkeypatch.setattr(forwarding, "message_box", _box)
        panel._forward_batch(msgs, targets, keep_captions)
        return boxes

    def test_success_ends_with_one_box_on_the_main_window(self, monkeypatch):
        panel = _Panel()

        boxes = self._run(monkeypatch, panel, [_msg("A"), _msg("B")])

        assert len(panel.sent) == 2
        (text, title, style, parent), = boxes
        assert (text, title) == ("done 2", "forward_message")
        assert style & wx.ICON_INFORMATION
        assert parent is panel.main_window
        assert panel.main_window.spoken == [] and panel.main_window.error_sound.played == 0

    def test_with_winzapp_in_the_tray_the_box_can_still_speak(self, monkeypatch):
        """The box goes through message_box(), which brings it forward and
        speaks it when the main window is hidden; a bare wx.MessageBox there
        opens with no focus and says nothing."""
        panel = _Panel()

        self._run(monkeypatch, panel, [_msg("A")])
        self.announce()

        assert panel.main_window.spoken == ["forward_done"]

    def test_a_failure_is_spoken_and_shows_no_box(self, monkeypatch):
        panel = _Panel(failing={"b@s.whatsapp.net"})

        boxes = self._run(monkeypatch, panel, [_msg("A")])

        assert boxes == []
        assert panel.main_window.spoken == ["failed Bob"]
        assert panel.main_window.error_sound.played == 1

    def test_a_message_without_an_id_is_not_counted(self, monkeypatch):
        panel = _Panel()
        broken = {"key": {"remoteJid": "source@g.us"}, "message": {}}

        boxes = self._run(monkeypatch, panel, [broken, _msg("A")])

        assert [s[0] for s in panel.sent] == ["A"]
        assert boxes[0][0] == "forward_done"

    def test_nothing_forwardable_shows_nothing(self, monkeypatch):
        panel = _Panel()
        broken = {"key": {"remoteJid": "source@g.us"}, "message": {}}

        assert self._run(monkeypatch, panel, [broken]) == []
        assert panel.main_window.spoken == []

    def test_the_caption_path_is_still_decided_per_message(self, monkeypatch):
        panel = _Panel()
        with_caption = _msg("PIC", imageMessage={"caption": "look"})

        self._run(monkeypatch, panel, [with_caption, _msg("TXT", conversation="hi")],
                  keep_captions=True)

        assert [(mid, keep) for mid, _src, keep in panel.sent] == [("PIC", True), ("TXT", False)]
