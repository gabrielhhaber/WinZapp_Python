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
ACTIONS = ("check", "rephrase", "professional", "encouraging")

#: i18n key of each action's menu label.
ACTION_KEY = {
    "check": "writing_action_check",
    "rephrase": "writing_action_rephrase",
    "professional": "writing_action_professional",
    "encouraging": "writing_action_encouraging",
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


def action_question(action):
    """The one request sent with the draft for ``action``."""
    if action not in _QUESTION:
        raise KeyError(action)
    return _QUESTION[action]
