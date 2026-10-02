"""Readable transcript without changing the provider's role-tagged history."""


def spoken_answer(text):
    """Join layout whitespace for one utterance; preserve words/punctuation."""
    return " ".join(text.split())


def render_history(history, *, description_first, question_label, answer_label):
    blocks = []
    for index, (role, text) in enumerate(history):
        if description_first and index == 0:
            continue  # The application's initial instruction is not a user question.
        if description_first and index == 1:
            blocks.append(text)
        else:
            label = question_label if role == "user" else answer_label
            blocks.append(f"{label}\n{text}")
    return "\n\n".join(blocks)
