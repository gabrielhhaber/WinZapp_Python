"""The row the conversation list shows when what narrows it leaves nothing.

A filter (Unread, Groups, Individual), a WhatsApp list or a search that
matches no conversation used to leave the list with no row at all: a screen
reader then says nothing on reaching it, which sounds the same as the app not
having loaded. The message list already answers an empty conversation with
one row that says so (list_refresh.py's "empty_placeholder"); this is that row
for the conversation list. It is not a conversation: the list's backing
chats_list stays empty, so every action that maps a row to a chat (each one
checks `0 <= index < len(chats_list)`) finds nothing there.

An unfiltered empty list shows no row: before the first sync that is not yet
a fact about the account.
"""

_FILTER_KEYS = {
    "unread": "chat_list_no_unread",
    "groups": "chat_list_no_groups",
    "individual": "chat_list_no_individual",
}


def empty_chat_list_key(search, conv_filter, wa_list_id) -> str:
    """The i18n key of the row an empty, narrowed list shows, or ""."""
    if search:
        return "chat_list_no_search_results"
    if wa_list_id:
        return "chat_list_empty_list"
    return _FILTER_KEYS.get(conv_filter or "all", "")
