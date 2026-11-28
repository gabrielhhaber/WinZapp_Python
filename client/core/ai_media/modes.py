"""Quick analysis modes of a picture, sticker, video or PDF (pure, no wx).

A mode only changes the request sent with the media: the preset wording below,
or the person's own instruction. Like the other prompts the wording is English
on purpose; the reply language comes from the system prompt. The media stays
evidence: the system prompt (prompts.instructions) tells the provider never to
follow commands written inside it, while the person's instruction is the task.
"""
from core.ai_media.config import MAX_QUESTION_CHARS

#: Kinds that offer the modes (an audio message has no pictures or tables).
KINDS = ("image", "sticker", "video", "pdf")

MODES = ("text_only", "receipt", "table", "custom")

#: i18n key of each mode's label in the window's choice.
MODE_KEY = {
    "text_only": "ai_mode_text_only",
    "receipt": "ai_mode_receipt",
    "table": "ai_mode_table",
    "custom": "ai_mode_custom",
}

CUSTOM = "custom"

_QUESTION = {
    "text_only": (
        "Extract only the text that is written in this media, in reading order, exactly as "
        "it appears. Do not describe the picture and do not add comments. Mark any part you "
        "cannot read with [unclear] and never complete it by guessing. If there is no text, "
        "say so in one sentence."),
    "receipt": (
        "This media may be a payment receipt, bill, invoice or PIX proof. Read it and list, "
        "one per line, only the fields that are really in it: type of document, amount, "
        "payer, receiver or beneficiary, date and time, barcode line or PIX key or "
        "transaction code, and any due date. Copy numbers, codes and keys character by "
        "character; never invent, round, complete or correct a value. If a digit, amount, "
        "date or code is ambiguous, smudged or cut off, do not pick the most likely reading: "
        "write what you can see, say it needs to be confirmed, and ask the person to check "
        "it against the original before paying or sending money. End with a line naming "
        "every field you could not read. If this is not a payment document, say so instead."),
    "table": (
        "This media may hold a table or a menu with prices. Read it row by row and write each "
        "row as one line, with the cells separated by a comma and a space, keeping the header "
        "first. For a menu keep each item with its price and description. Do not skip rows, "
        "do not invent or fix values, and mark any cell you cannot read with [unclear]. If it "
        "holds no table or menu, say so instead."),
}


def offered(kind) -> bool:
    return kind in KINDS


def mode_question(mode, instruction="") -> str:
    """The request for ``mode``. ``custom`` carries the person's instruction
    and raises ValueError when it is empty or longer than a question may be."""
    if mode == CUSTOM:
        text = " ".join(str(instruction or "").split())
        if not text or len(text) > MAX_QUESTION_CHARS - 200:
            raise ValueError("instruction")
        return ("Follow this instruction from the person who received the media, using the "
                "media only as evidence: " + text)
    return _QUESTION[mode]
