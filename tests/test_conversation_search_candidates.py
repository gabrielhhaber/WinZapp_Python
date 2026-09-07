"""A global conversation search that cannot see the Archived tab.

WhatsApp searches archived conversations too; WinZapp's list deliberately does
not contain them, so searching only ever matched what was already on screen and
an archived chat was unreachable by name. _conversation_search_candidates()
merges the two panels for the duration of a search — and only for a search: an
empty search box must leave the ordinary list byte-for-byte as it was, or every
archived chat appears in the normal conversations list.

The third return value is what makes the merged rows distinguishable. A
wx.ListCtrl row is read out as one string, so "Ana" pulled in from Arquivadas
sounded exactly like "Ana" from the active list; _build_chat_item_text()
suffixes exactly the JIDs named here.

MainWindow is a wx.Frame and cannot be instantiated without a running app, so
this is called as the @staticmethod it is.
"""

from main import MainWindow


# Fictional identifiers, same shape as the real ones.
ANA = "551199990001@s.whatsapp.net"
BRUNO = "551199990002@s.whatsapp.net"
CARLA = "551199990003@s.whatsapp.net"


def _chat(jid):
    return {"remoteJid": jid}


def _candidates(main_chats, main_names, arch_chats, arch_names, include_archived):
    return MainWindow._conversation_search_candidates(
        main_chats, main_names, arch_chats, arch_names, include_archived
    )


class TestAnEmptySearchLeavesTheNormalListAlone:
    def test_archived_chats_are_not_merged_in(self):
        chats, names, archived = _candidates(
            [_chat(ANA)], ["Ana"], [_chat(CARLA)], ["Carla"], False
        )

        assert chats == [_chat(ANA)]
        assert names == ["Ana"]
        assert archived == set()

    def test_the_returned_lists_are_copies(self):
        """add_chats_to_ui() passes the panel's own backing lists in."""
        original = [_chat(ANA)]
        chats, names, _ = _candidates(original, ["Ana"], [], [], False)

        chats.append(_chat(BRUNO))
        assert original == [_chat(ANA)]


class TestASearchReachesTheArchivedTab:
    def test_archived_chats_are_appended_with_their_names(self):
        chats, names, archived = _candidates(
            [_chat(ANA)], ["Ana"], [_chat(CARLA)], ["Carla"], True
        )

        assert chats == [_chat(ANA), _chat(CARLA)]
        assert names == ["Ana", "Carla"]
        assert archived == {CARLA}

    def test_a_jid_present_in_both_lists_is_not_duplicated(self):
        chats, names, archived = _candidates(
            [_chat(ANA), _chat(BRUNO)],
            ["Ana", "Bruno"],
            [_chat(BRUNO), _chat(CARLA)],
            ["Bruno arquivado", "Carla"],
            True,
        )

        assert chats == [_chat(ANA), _chat(BRUNO), _chat(CARLA)]
        # The active copy wins, so its name — and its lack of a suffix — is
        # what the row keeps.
        assert names == ["Ana", "Bruno", "Carla"]
        assert archived == {CARLA}

    def test_only_the_merged_jids_are_reported_as_archived(self):
        """The suffix must never land on a row the main list already had."""
        _, _, archived = _candidates(
            [_chat(ANA)], ["Ana"], [_chat(ANA)], ["Ana"], True
        )

        assert archived == set()

    def test_a_short_name_list_does_not_misalign_the_rows(self):
        """The two lists are read from separate panel attributes, so they can
        momentarily disagree in length mid-rebuild; the chat/name pairing must
        survive it rather than raising or shifting every later row."""
        chats, names, archived = _candidates(
            [], [], [_chat(BRUNO), _chat(CARLA)], ["Bruno"], True
        )

        assert chats == [_chat(BRUNO), _chat(CARLA)]
        assert names == ["Bruno", ""]
        assert archived == {BRUNO, CARLA}

    def test_an_archived_chat_with_no_jid_is_skipped(self):
        """The suffix rides on the JID set, so a row with no JID could only be
        merged in unmarked — read aloud as an ordinary active conversation,
        which is the exact confusion the third return value exists to stop."""
        chats, names, archived = _candidates(
            [_chat(ANA)], ["Ana"], [{"remoteJid": ""}, _chat(CARLA)], ["", "Carla"], True
        )

        assert chats == [_chat(ANA), _chat(CARLA)]
        assert names == ["Ana", "Carla"]
        assert archived == {CARLA}

    def test_non_dict_entries_are_skipped(self):
        chats, names, archived = _candidates(
            [_chat(ANA)], ["Ana"], [None, _chat(CARLA)], ["", "Carla"], True
        )

        assert chats == [_chat(ANA), _chat(CARLA)]
        assert names == ["Ana", "Carla"]
        assert archived == {CARLA}
