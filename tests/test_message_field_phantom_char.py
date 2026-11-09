"""Issue #71: Windows+NVDA+Left/Right (and reportedly some other NVDA
object-navigation gestures) leaked a literal 'ÿ' (U+00FF) character into
whatever wx.TextCtrl was focused, even though no text key was pressed and the
same gestures type nothing in other applications. ConversationsPanel now
vetoes exactly that character in the message field's EVT_CHAR handler rather
than trying to special-case NVDA's own modifier state, which wx never sees.

ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so the static veto check is exercised directly against a fake event —
same approach as the rest of this suite for small islands of pure logic.
"""

from ui.conversations import ConversationsPanel


class _FakeEvent:
    def __init__(self, unicode_key):
        self._unicode_key = unicode_key
        self.skipped = False

    def GetUnicodeKey(self):
        return self._unicode_key

    def Skip(self):
        self.skipped = True


class TestPhantomNvdaCharVeto:
    def test_u00ff_is_recognized_as_the_phantom_character(self):
        assert ConversationsPanel._is_phantom_nvda_char(_FakeEvent(0xFF)) is True

    def test_a_real_typed_character_is_not_vetoed(self):
        assert ConversationsPanel._is_phantom_nvda_char(_FakeEvent(ord("a"))) is False

    def test_backspace_and_other_control_codes_are_not_vetoed(self):
        assert ConversationsPanel._is_phantom_nvda_char(_FakeEvent(8)) is False


class TestMessageFieldCharHandler:
    """_on_message_field_char() itself, bound onto a stub instance."""

    def test_phantom_character_is_consumed_not_skipped(self):
        stub = type("Stub", (), {
            "_on_message_field_char": ConversationsPanel._on_message_field_char,
            "_is_phantom_nvda_char": staticmethod(ConversationsPanel._is_phantom_nvda_char),
        })()
        event = _FakeEvent(0xFF)

        stub._on_message_field_char(event)

        assert event.skipped is False

    def test_real_character_is_skipped_through_to_the_control(self):
        stub = type("Stub", (), {
            "_on_message_field_char": ConversationsPanel._on_message_field_char,
            "_is_phantom_nvda_char": staticmethod(ConversationsPanel._is_phantom_nvda_char),
            # A real character may end an emoticon; this one does not.
            "_convert_emoticon_before_caret": lambda self, char: False,
        })()
        event = _FakeEvent(ord("y"))

        stub._on_message_field_char(event)

        assert event.skipped is True


class _RawEvent(_FakeEvent):
    """With the lParam flags wx passes on: bits 16-23 are the scan code."""

    def __init__(self, unicode_key, scan):
        super().__init__(unicode_key)
        self.scan = scan

    def GetRawKeyFlags(self):
        return (self.scan << 16) | 1


class TestOnlyTheInjectedKeyIsVetoed:
    def test_the_injected_vk_0xff_has_no_scan_code_and_is_vetoed(self):
        assert ConversationsPanel._is_phantom_nvda_char(_RawEvent(0xFF, 0)) is True

    def test_a_real_y_with_diaeresis_from_a_physical_key_can_be_typed(self):
        # a dead "¨" then "y": the y key's scan code (0x15)
        assert ConversationsPanel._is_phantom_nvda_char(_RawEvent(0xFF, 0x15)) is False
        stub = type("Stub", (), {
            "_on_message_field_char": ConversationsPanel._on_message_field_char,
            "_is_phantom_nvda_char": staticmethod(ConversationsPanel._is_phantom_nvda_char),
            "_convert_emoticon_before_caret": lambda self, char: False,
        })()
        event = _RawEvent(0xFF, 0x15)
        stub._on_message_field_char(event)
        assert event.skipped is True
