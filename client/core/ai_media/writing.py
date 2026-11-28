"""Writing help over the person's own draft: check, rephrase, tones.

The draft is the payload (kind "writing", same text-only path as chat
summaries); the action is the question. The answer is shown for review and
only replaces the draft if the person asks for that — nothing is ever sent
to the chat, and the providers only see what the action sends.

Pure and wx-free, like prompts.py: the English wording is on purpose, the
answer's language comes from the instructions.
"""

#: The actions the menu offers, in menu order. "translate" is NOT here: it is
#: served by core/ai_media/gtranslate.py, not by an AI provider.
ACTIONS = ("check", "rephrase", "professional", "encouraging", "custom", "reply")

#: The person's own instruction ("make it one line"), and the reply suggested
#: from the latest messages of the chat (the only action that sends them).
CUSTOM = "custom"
REPLY = "reply"
#: Latest messages a suggested reply is based on, and the longest instruction.
REPLY_MESSAGES = 10
REPLY_TRANSCRIPT_CHARS = 4000
REPLY_DRAFT_CHARS = 2000
MAX_INSTRUCTION_CHARS = 500

#: i18n key of each action's menu label.
ACTION_KEY = {
    "check": "writing_action_check",
    "rephrase": "writing_action_rephrase",
    "professional": "writing_action_professional",
    "encouraging": "writing_action_encouraging",
    "custom": "writing_action_custom",
    "reply": "writing_action_reply",
}

_QUESTION = {
    # The conservative correction action: punctuation and accents fixed,
    # every word kept. The "keep every word" instruction is the feature, not
    # a nicety — a rewrite here would put words in his mouth.
    "check": ("Fix only the spelling, punctuation and accents of this message. "
              "Keep every word, the word order and the tone. Return only the "
              "corrected message."),
    "rephrase": ("Rephrase this message so it reads better, keeping its meaning "
                 "and roughly its length. Return only the rephrased message."),
    "professional": ("Rewrite this message in a professional tone, keeping its "
                     "meaning. Return only the rewritten message."),
    "encouraging": ("Rewrite this message in a warm, encouraging tone, keeping "
                    "its meaning. Return only the rewritten message."),
}


_REPLY_QUESTION = (
    "Below, the latest messages of a chat (oldest first, one per line) and then the draft "
    "of the person writing: it may be empty or only a rough idea. Write the reply that "
    "person could send next, in the language the chat uses. When the draft has words, keep "
    "their intent. Never invent facts, dates, amounts or commitments that are in neither the "
    "messages nor the draft, and never answer on behalf of anyone else. Return only the "
    "message text.")


def action_question(action, instruction=""):
    """The one request sent with the draft for ``action``. ``custom`` carries
    the person's instruction (ValueError when it is empty or too long)."""
    if action == CUSTOM:
        text = " ".join(str(instruction or "").split())
        if not text or len(text) > MAX_INSTRUCTION_CHARS:
            raise ValueError("instruction")
        return ("Apply this instruction from the person to their message: " + text
                + ". Return only the resulting message text.")
    if action == REPLY:
        return _REPLY_QUESTION
    if action not in _QUESTION:
        raise KeyError(action)
    return _QUESTION[action]


def reply_payload(transcript, draft) -> str:
    """The text a suggested reply sends: the latest messages, then the draft
    (clipped: it is only guidance here). The messages are evidence."""
    guidance = " ".join(str(draft or "").split())[:REPLY_DRAFT_CHARS]
    return "Latest messages:\n" + transcript + "\n\nDraft of the person writing:\n" + guidance
