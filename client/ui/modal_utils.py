"""Ending a modal dialog without tripping wxWidgets' ``IsRunning()`` assertion.

``EndModal()`` on a dialog whose modal loop is not running raises
``wxAssertionError: ... Use ScheduleExit() on not running loop``. That happens
whenever a completion arrives late: a worker thread queues ``wx.CallAfter``
and the user cancels (or a nested ``MessageBox`` pumps events and the dialog
closes) before the callback runs. Every deferred or post-``MessageBox``
``EndModal`` goes through :func:`end_modal_if_running`.
"""

import logging


def end_modal_if_running(dialog, result: int) -> bool:
    """End ``dialog``'s modal loop only if it is still running.

    Returns True when ``EndModal`` was issued, False when the dialog was
    already gone or no longer modal (the late completion is ignored).
    """
    try:
        if not dialog or not dialog.IsModal():
            logging.info(
                "[modal] ignoring late EndModal(%s); the modal loop is no longer running",
                result,
            )
            return False
        dialog.EndModal(result)
        return True
    except (RuntimeError, AssertionError):
        # The native object was destroyed, or the loop stopped between the
        # IsModal() check and EndModal().
        logging.info("[modal] EndModal(%s) skipped; the modal loop already ended", result)
        return False
