"""RAM-only dialog session, immutable identity and generation-gated callbacks."""
from .config import MAX_QUESTION_CHARS, MAX_TURNS, MAX_REQUESTS
from .errors import DescriptionError
from .service import RequestToken


class PhotoSession:
    def __init__(self, account, chat, message, locked=False):
        self.identity = (account, chat, message)
        self.locked = locked
        self.image = None
        self.history = []
        self.requests = 0
        self.generation = 0
        self.active = None
        self.closed = False

    def begin(self, question):
        if self.closed or self.active is not None:
            raise DescriptionError("busy")
        if not question.strip() or len(question) > MAX_QUESTION_CHARS:
            raise DescriptionError("question")
        if self.requests >= MAX_REQUESTS or len(self.history) >= MAX_TURNS * 2:
            raise DescriptionError("limit")
        self.requests += 1
        self.generation += 1
        self.active = RequestToken()
        return self.generation, self.active

    def accept(self, generation, question, answer):
        if self.closed or generation != self.generation or self.active is None:
            return False
        self.active = None
        if answer:
            self.history.extend((("user", question), ("assistant", answer)))
        return True

    def cancel(self):
        self.generation += 1
        if self.active is not None:
            self.active.cancel()
        self.active = None

    def close(self):
        self.closed = True
        self.cancel()
        self.image = None
        self.history.clear()
