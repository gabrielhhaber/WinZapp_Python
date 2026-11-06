"""Opt-in storage evaluations; ordinary scans never invoke automatic rules."""
import time

from core.storage_manager.schedule import Schedule


class StorageRuntime:
    def __init__(self, window, service, context):
        self.window, self.service, self.context = window, service, context
        self.schedule = Schedule()
        self.closed = False
        self.last_notice = None
        self.last_notice_at = None

    def tick(self, event=None):
        service = self.service
        if (self.closed or not service.valid() or getattr(self.window, "_storage_dialog_open", False)
                or service.preferences.paused_until > time.time()):
            return
        if not self.schedule.due(service.preferences, time.monotonic(), busy=service.jobs.busy):
            return
        self.context.capture_chats()
        service.scan(self.scanned)

    def scanned(self, snapshot, error):
        if (self.closed or error or not self.service.valid() or snapshot is None or not snapshot.complete
                or self.service.preferences.paused_until > time.time()):
            return
        service = self.service
        count = getattr(service, "notification_count", 0)
        notice = (service.warning(), service.pause_downloads, count)
        now = time.monotonic()
        repeat = service.preferences.warning_repeat_minutes * 60
        if any(notice) and (self.last_notice_at is None or (repeat and now - self.last_notice_at >= repeat)):
            from ui.dialogs.storage_presentation import size_text
            self.window.speak_output.output(self.window.i18n.t("storage_scheduled_notice").format(
                count=count, total=size_text(snapshot.total_bytes, self.window.i18n)))
            self.last_notice_at = now
        if not any(notice):
            self.last_notice_at = None
        self.last_notice = notice
        keys = service.automatic_selection(cached=True)
        if keys:
            # Only an enabled cache-only automatic rule can enter this path.
            # A complete newly scheduled snapshot is required every time.
            from core.storage_manager.transactions import PlanTooLarge
            keys = keys[:500]
            while keys:
                try:
                    plan = service.preview(keys, "remove", automatic=True)
                    service.apply(plan, self.applied)
                    break
                except PlanTooLarge:
                    keys = keys[:len(keys) // 2]
                except (OSError, ValueError):
                    self.applied(None, "Unavailable")
                    break

    def applied(self, result, error):
        if self.closed or not self.service.valid():
            return
        key = "storage_scheduled_failed" if error else "storage_scheduled_result"
        values = {} if error else dict(completed=result.completed, failed=result.failed, skipped=result.skipped)
        self.window.speak_output.output(self.window.i18n.t(key).format(**values))

    def close(self):
        self.closed = True
        self.service.jobs.close()


def start(window):
    """After UI initialization; no scan until saved choices request one."""
    import wx
    from .storage_management import service_for
    service = service_for(window)
    runtime = StorageRuntime(window, service, window._storage_context)
    window._storage_runtime = runtime
    timer = window._storage_timer = wx.Timer(window)
    window.Bind(wx.EVT_TIMER, runtime.tick, timer)
    timer.Start(30000)
    wx.CallAfter(runtime.tick)


def stop(window):
    runtime = getattr(window, "_storage_runtime", None)
    if runtime is not None:
        runtime.close()
    timer = getattr(window, "_storage_timer", None)
    if timer is not None:
        import wx
        wx.CallAfter(timer.Stop)
