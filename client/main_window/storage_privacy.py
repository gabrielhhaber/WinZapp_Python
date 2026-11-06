"""Revoke cached storage visibility at the chat-lock owner's transition."""


def invalidate(window, *, dispatch=None, is_main=None, only_if_hidden=False):
    service = getattr(window, "_storage_service", None)
    if service is None:
        return
    context = getattr(window, "_storage_context", None)
    if (only_if_hidden and context is not None
            and not context.has_hidden_labels(getattr(service.snapshot, "named_chats", ()))):
        return
    if context is not None:
        context.forget_labels()
    views = service.invalidate_visibility()
    if not views:
        return
    if dispatch is None or is_main is None:
        import wx
        dispatch, is_main = wx.CallAfter, wx.IsMainThread
    def clear():
        for view in views:
            try:
                view.clear_private_state()
            except RuntimeError:
                pass  # A view may have been destroyed before UI dispatch.
    if is_main():
        clear()
    else:
        dispatch(clear)
