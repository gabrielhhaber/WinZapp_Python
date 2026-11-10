""""Continue where you left off" in the messages list (core/reading_position.py
and ui/conversation_panel/reading_position.py).

Settings > Interface > Conversations, "Posição focada automaticamente na lista
de mensagens": with "Continuar de onde você parou", opening a conversation
lands the list on the message the user was on when they left it, instead of
the unread separator or the last message. Rows that are not messages (the
unread separator, the "no messages" placeholder, the typing row past the end)
never count as a position of their own.
"""

from core import reading_position as rp
from tests.god_modules import conversations_method_source
from ui.conversations import ConversationsPanel
from ui.conversation_panel.reading_position import ReadingPositionMixin

JID_A = "a@s.whatsapp.net"
JID_B = "b@s.whatsapp.net"


def _msg(msg_id, ts=0):
    return {"key": {"id": msg_id}, "messageTimestamp": ts}


def _sep():
    return {"_type": "unread_separator", "count": 2}


def _placeholder():
    return {"_type": "empty_placeholder"}


ON = {"user_interface": {"message_list_open_position": "last_focused"}}


class TestMode:
    def test_default_is_the_old_behaviour(self):
        assert rp.mode({}) == rp.UNREAD_OR_LAST
        assert rp.mode({"user_interface": {}}) == rp.UNREAD_OR_LAST
        assert not rp.resumes_last_focused({})

    def test_unknown_value_falls_back(self):
        assert rp.mode({"user_interface": {"message_list_open_position": "x"}}) == rp.UNREAD_OR_LAST
        assert rp.mode(None) == rp.UNREAD_OR_LAST

    def test_last_focused(self):
        assert rp.mode(ON) == rp.LAST_FOCUSED
        assert rp.resumes_last_focused(ON)

    def test_options_are_in_the_settings_order(self):
        assert rp.MODES == ("unread_or_last", "last_focused")


class TestResumeMessageId:
    def test_a_message_row_is_itself(self):
        assert rp.resume_message_id([_msg("A"), _msg("B")], 0) == "A"

    def test_the_separator_resumes_at_the_first_unread_message(self):
        rows = [_msg("A"), _sep(), _msg("B"), _msg("C")]
        assert rp.resume_message_id(rows, 1) == "B"

    def test_a_separator_with_nothing_below_falls_back_above(self):
        assert rp.resume_message_id([_msg("A"), _sep()], 1) == "A"

    def test_past_the_end_is_the_newest_message(self):
        # The typing row is a row of the control, not of _sorted_messages.
        assert rp.resume_message_id([_msg("A"), _msg("B")], 2) == "B"

    def test_the_placeholder_and_nowhere_remember_nothing(self):
        assert rp.resume_message_id([_placeholder()], 0) == ""
        assert rp.resume_message_id([_msg("A")], -1) == ""
        assert rp.resume_message_id([], 0) == ""

    def test_a_row_without_an_id_is_not_a_position(self):
        assert rp.resume_message_id([{"key": {}}], 0) == ""


class TestFetchDepth:
    def test_depth_counts_records_as_new_or_newer(self):
        records = [_msg("A", 1), _msg("B", 2), _msg("R", 2), _msg("C", 3)]
        assert rp.records_depth(records, "B") == 3
        assert rp.records_depth(records, "C") == 1
        assert rp.records_depth(records, "missing") == 0

    def test_the_limit_widens_to_reach_the_message(self):
        assert rp.resume_fetch_limit(200, 500, 10) == 560
        assert rp.resume_fetch_limit(200, 30, 0) == 200

    def test_nothing_remembered_or_too_deep_keeps_the_base(self):
        assert rp.resume_fetch_limit(200, 0, 5) == 200
        assert rp.resume_fetch_limit(200, rp.MAX_RESUME_FETCH, 0) == 200


class _List:
    def __init__(self, focused=-1):
        self.focused = focused

    def GetFocusedItem(self):
        return self.focused


class _MainWindow:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else {}


class _Stub(ReadingPositionMixin):
    _is_separator = ConversationsPanel._is_separator
    _find_index_by_msg_id = ConversationsPanel._find_index_by_msg_id

    def __init__(self, rows, focused, jid=JID_A, settings=None):
        self._sorted_messages = rows
        self.messages_list = _List(focused)
        self.main_window = _MainWindow(settings)
        self.conversation = {
            "remoteJid": jid,
            "messages": {"messages": {"records": [r for r in rows if "key" in r]}},
        }


class TestRememberAndResume:
    def test_leaving_and_reopening_lands_on_the_same_message(self):
        panel = _Stub([_msg("A", 1), _msg("B", 2), _msg("C", 3)], focused=1, settings=ON)
        panel._remember_reading_position()

        # Another conversation is opened, then this one again: the rebuild
        # has a new message and an unread separator in front of the old ones.
        panel._arm_reading_resume(JID_A)
        panel._sorted_messages = [_sep(), _msg("A", 1), _msg("B", 2), _msg("C", 3), _msg("D", 4)]

        assert panel._take_reading_resume_row() == 2
        # Consumed: the next rebuild lands as usual.
        assert panel._take_reading_resume_row() == -1

    def test_off_nothing_is_armed_but_the_position_is_still_remembered(self):
        panel = _Stub([_msg("A", 1), _msg("B", 2)], focused=0)
        panel._remember_reading_position()
        panel._arm_reading_resume(JID_A)

        assert panel._take_reading_resume_row() == -1
        assert panel._reading_positions()[JID_A] == ("A", 2)

    def test_each_conversation_keeps_its_own(self):
        panel = _Stub([_msg("A", 1), _msg("B", 2)], focused=0, settings=ON)
        panel._remember_reading_position()
        panel.conversation = {"remoteJid": JID_B}
        panel._sorted_messages = [_msg("X", 1), _msg("Y", 2)]
        panel.messages_list.focused = 1
        panel._remember_reading_position()

        panel._arm_reading_resume(JID_A)
        assert panel._reading_resume_anchor() == "A"
        panel._arm_reading_resume(JID_B)
        assert panel._reading_resume_anchor() == "Y"

    def test_a_conversation_never_left_has_nothing_to_resume(self):
        panel = _Stub([_msg("A")], focused=0, settings=ON)
        panel._arm_reading_resume(JID_B)
        assert panel._take_reading_resume_row() == -1

    def test_a_message_gone_from_the_list_falls_back(self):
        panel = _Stub([_msg("A", 1), _msg("B", 2)], focused=0, settings=ON)
        panel._remember_reading_position()
        panel._arm_reading_resume(JID_A)
        panel._sorted_messages = [_msg("B", 2)]

        assert panel._take_reading_resume_row() == -1

    def test_sitting_on_the_placeholder_keeps_the_previous_position(self):
        panel = _Stub([_msg("A", 1)], focused=0, settings=ON)
        panel._remember_reading_position()
        panel._sorted_messages = [_placeholder()]
        panel._remember_reading_position()

        assert panel._reading_positions()[JID_A][0] == "A"

    def test_the_fetch_reaches_the_remembered_message(self):
        rows = [_msg(str(i), i) for i in range(300)]
        panel = _Stub(rows, focused=0, settings=ON)
        panel._remember_reading_position()
        panel._arm_reading_resume(JID_A)

        assert panel._reading_resume_fetch_limit(200, 4) == 300 + 4 + 50

    def test_already_open_keeps_the_row_the_user_is_on(self):
        panel = _Stub([_msg("A"), _sep(), _msg("B")], focused=0, settings=ON)
        assert panel._reading_resume_row_for_open_conversation() == 0
        panel.messages_list.focused = 1
        assert panel._reading_resume_row_for_open_conversation() == 2

    def test_already_open_with_the_setting_off_is_untouched(self):
        panel = _Stub([_msg("A"), _msg("B")], focused=0)
        assert panel._reading_resume_row_for_open_conversation() == -1


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.limits = []

    def get_messages(self, jid, limit):
        self.limits.append(limit)
        return list(reversed(self.rows))[:limit]

    def get_message_count(self, jid):
        return len(self.rows)


class _OpeningStub(_Stub):
    _load_conversation_page_from_db = ConversationsPanel._load_conversation_page_from_db
    _history_window_for_rebuild = ConversationsPanel._history_window_for_rebuild
    _reset_expanded_window = ConversationsPanel._reset_expanded_window


class TestOpeningReachesTheRememberedMessage:
    def _left_far_back(self):
        rows = [_msg(str(i), i) for i in range(600)]
        panel = _OpeningStub(rows, focused=100, settings=ON)
        panel.main_window.settings["user_interface"]["messages_page_size"] = 200
        panel.main_window.db = _Db(rows)
        panel._remember_reading_position()      # message "100", 500 records back
        panel._arm_reading_resume(JID_A)
        return panel, rows

    def test_the_database_fetch_is_widened(self):
        panel, _ = self._left_far_back()
        conversation = {"remoteJid": JID_A}
        panel._load_conversation_page_from_db(conversation)

        records = conversation["messages"]["messages"]["records"]
        assert panel.main_window.db.limits == [500 + 50]
        assert any(r["key"]["id"] == "100" for r in records)

    def test_without_a_resume_the_fetch_is_the_page_size(self):
        panel, _ = self._left_far_back()
        panel._disarm_reading_resume()
        panel._load_conversation_page_from_db({"remoteJid": JID_A})
        from core.utils import db_fetch_limit
        assert panel.main_window.db.limits == [db_fetch_limit(200, 0)]

    def test_the_window_keeps_the_message_in_view(self):
        panel, rows = self._left_far_back()
        panel._reset_expanded_window()
        panel._unread_sep_idx = -1
        offset, _sep = panel._history_window_for_rebuild(rows, 200)

        assert offset <= 100


class TestClosingRemembersIt:
    def test_close_records_the_focused_message(self):
        from tests.test_close_conversation_for_panel_switch import _Stub as _CloseStub
        stub = _CloseStub({"remoteJid": JID_A})
        stub._sorted_messages = [_msg("A"), _msg("B")]
        stub.messages_list = _List(1)
        ConversationsPanel._close_conversation_core(stub)

        assert stub.conversation is None
        assert stub._reading_positions()[JID_A][0] == "B"


class TestWiring:
    """navigate_to_conversation() and populate_messages() cannot run against a
    stub; their source must still call the pieces above."""

    def test_opening_remembers_the_old_one_and_arms_the_new_one(self):
        src = conversations_method_source("navigate_to_conversation")
        assert src.index("_remember_reading_position()") < src.index("self.conversation = conversation\n        self._restore_message_draft()")
        assert src.index("_arm_reading_resume(") < src.index("self.populate_messages()")
        assert "_disarm_reading_resume()" in src

    def test_the_opening_rebuild_lands_on_it(self):
        src = conversations_method_source("populate_messages")
        assert src.index("_take_reading_resume_row()") < src.index("self._unread_sep_idx >= 0:\n                    last")
