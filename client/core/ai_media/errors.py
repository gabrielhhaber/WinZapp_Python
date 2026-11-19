class ErrorKey(str):
    """A failure's translation key. ``attempts`` is empty except after a chain
    of providers all failed: then it lists (provider, category) for each, so
    the window can say who failed how while every caller keeps treating the
    value as a plain key."""
    attempts = ()


class DescriptionError(RuntimeError):
    """Safe translation key only; no response bodies, keys or user content."""
    def __init__(self, category):
        self.category = category
        super().__init__(f"ai_error_{category}")

    @property
    def key(self):
        return ErrorKey(str(self))


def status_error(status):
    if status in (401, 403):
        return DescriptionError("authentication")
    if status == 402:
        # Payment Required: OpenRouter's answer to an account without the
        # credit a request needs. Read as "request" it said "check the model
        # identifier and settings", on a reported log with three 402s in a row.
        return DescriptionError("credits")
    if status == 429:
        return DescriptionError("quota")
    if status >= 500:
        return DescriptionError("server")
    return DescriptionError("request")
