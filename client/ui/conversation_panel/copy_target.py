"""Which copy handler Ctrl+C runs for a message.

One rule for the conversation list and the group's Media tab: a photo, video,
document or audio message copies the *file* to the clipboard, a contact card
copies its number, everything else copies the text. The Media tab used to call
the text handler for every row, so a document there had no text and answered
"could not copy this message" while the same document copied fine in the list.
"""

FILE_COPY_TYPES = ("imageMessage", "videoMessage", "documentMessage", "audioMessage")


def copy_method_for_message(msg: dict) -> str:
    """Name of the ConversationsPanel handler Ctrl+C runs on *msg*."""
    msg_type = (msg or {}).get("messageType", "")
    if msg_type in FILE_COPY_TYPES:
        return "_on_menu_copy_file"
    if msg_type == "contactMessage":
        return "_on_contact_copy_number"
    return "_on_menu_copy_message"
