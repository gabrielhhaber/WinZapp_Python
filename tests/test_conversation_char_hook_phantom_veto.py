"""Issue #71 follow-up: _on_message_field_char()'s veto only stops the
phantom U+00FF character ('ÿ') when the message field itself already has
keyboard focus at the moment the bogus WM_CHAR arrives. In the common case
of browsing a conversation with a screen reader — focus on the conversations
list or the messages list, not the compose box — the same character instead
reaches ConversationsPanel._on_conversation_char_hook(), a panel-level
EVT_CHAR_HOOK used to redirect ordinary typing ("type anywhere to reply")
into the message field regardless of which child control has focus.

That redirect's own filter, _should_redirect_char_to_message(), only rejects
non-alphanumeric characters — and chr(0xFF).isalnum() is True in Python — so
the phantom character sailed through, moved focus to the message field with
SetFocus(), and was written into it with WriteText(). WriteText() never
raises EVT_CHAR, so _on_message_field_char()'s veto never even saw it. This
is exactly the report: pressing Windows+NVDA+Left/Right (or, reportedly,
Alt+Tab) while a list has focus left a literal 'ÿ' sitting in the message
field once focus returned to the conversation.

ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so _on_conversation_char_hook() is exercised as a plain function
bound onto a stub carrying only what each test path touches — same approach
as tests/test_message_field_phantom_char.py.
"""

from ui.conversations import ConversationsPanel


class _FakeCharHookEvent:
    def __init__(self, unicode_key, key_code=None):
        self._unicode_key = unicode_key
        self._key_code = key_code if key_code is not None else unicode_key
        self.skipped = False

    def GetUnicodeKey(self):
        return self._unicode_key

    def GetKeyCode(self):
        return self._key_code

    def Skip(self):
        self.skipped = True


class TestPhantomCharIsVetoedRegardlessOfFocus:
    def test_phantom_character_is_consumed_before_any_redirect_logic_runs(self):
        """No _mention_panel / _should_redirect_char_to_message / message_field
        attributes exist on this stub at all — if the veto did not return
        immediately, touching any of them would raise AttributeError instead
        of the test merely failing an assertion."""
        stub = type("Stub", (), {
            "_on_conversation_char_hook": ConversationsPanel._on_conversation_char_hook,
            "_is_phantom_nvda_char": staticmethod(ConversationsPanel._is_phantom_nvda_char),
        })()
        event = _FakeCharHookEvent(0xFF)

        stub._on_conversation_char_hook(event)  # must not raise

        assert event.skipped is False

    def test_phantom_character_is_never_redirected_into_the_message_field(self, monkeypatch):
        """Belt-and-suspenders: even if the top-of-function veto were ever
        removed, _should_redirect_char_to_message() itself must not treat
        U+00FF as an ordinary alphanumeric character to forward."""
        import ui.conversations as conversations_module

        class _AlwaysShownEnabled:
            def IsShown(self):
                return True

            def IsEnabled(self):
                return True

        class _Stub:
            _should_redirect_char_to_message = ConversationsPanel._should_redirect_char_to_message
            _is_phantom_nvda_char = staticmethod(ConversationsPanel._is_phantom_nvda_char)

            def __init__(self):
                self.conversation = object()
                self.conversation_panel = _AlwaysShownEnabled()
                self.message_field = _AlwaysShownEnabled()
                self._is_recording = False

        # focus is on some non-TextCtrl control (e.g. the conversations
        # list) — the case this bug actually reproduces under.
        monkeypatch.setattr(conversations_module.wx.Window, "FindFocus", staticmethod(lambda: None))

        event = _FakeCharHookEvent(0xFF)
        event.ControlDown = lambda: False
        event.AltDown = lambda: False

        assert _Stub()._should_redirect_char_to_message(event) is False


class TestRealCharacterStillFallsThroughUnaffected:
    def test_a_real_typed_character_reaches_the_redirect_check(self):
        calls = []

        stub = type("Stub", (), {
            "_on_conversation_char_hook": ConversationsPanel._on_conversation_char_hook,
            "_is_phantom_nvda_char": staticmethod(ConversationsPanel._is_phantom_nvda_char),
            "_should_redirect_char_to_message": lambda self, event: calls.append(event) or False,
        })()
        event = _FakeCharHookEvent(ord("y"))

        stub._on_conversation_char_hook(event)

        assert calls == [event]
        assert event.skipped is True  # not redirected -> falls through normally
