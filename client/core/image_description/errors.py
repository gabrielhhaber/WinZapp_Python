class DescriptionError(RuntimeError):
    """Safe translation key only; no response bodies, keys or user content."""
    def __init__(self, category):
        self.category = category
        super().__init__(f"ai_error_{category}")


def status_error(status):
    if status in (401, 403):
        return DescriptionError("authentication")
    if status == 429:
        return DescriptionError("quota")
    if status >= 500:
        return DescriptionError("server")
    return DescriptionError("request")
