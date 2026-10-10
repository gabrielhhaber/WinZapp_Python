"""Restart and validate an updated API, keeping its predecessor until then."""

import logging
import threading
import time

import wx

from core import api_staging
from core.api_install_timing import timed_call
from core.wpp_update_health import wait_for_api, wait_for_port_closed


def restart_api_after_update(window, api_dir: str, backup: str, on_done) -> None:
    """Called on wx's thread; HTTP probes, stop, rollback and cleanup are workers.

    on_done receives True only for a working NEW API. A restored predecessor
    still reports False, so the update checker remembers the failed release.
    """
    def failure(key, details=""):
        window._wpp_update_error_key = key
        window._wpp_update_error_details = details

    def start_and_probe(restoring=False):
        try:
            started = timed_call("api_startup", window.ensure_wpp_running) is not False
        except (Exception, SystemExit) as exc:
            logging.exception("[wpp_update] API restart failed")
            if not restoring:
                failure("wpp_update_start_failed", str(exc))
            started = False

        def probe():
            healthy = False
            cleanup_path = ""
            try:
                # With the main window open, ensure_wpp_running() queues the
                # spawn and returns before Node even exists. Give that start
                # its normal 300-second budget before the short HTTP check,
                # and capture the process identity only after the spawn.
                listening = started
                wait_until_listening = getattr(window, "_wait_until_api_listening", None)
                if listening and wait_until_listening is not None:
                    listening = timed_call("api_startup_wait", wait_until_listening) is True
                if started and not listening:
                    logging.warning("[wpp_update] API did not finish starting; validation skipped")
                identity = {}
                instance = getattr(window, "_node_instance_id", None)
                pid = getattr(getattr(window, "wpp_process", None), "pid", None)
                if instance:
                    identity["instance_id"] = instance
                if pid:
                    identity["pid"] = pid
                healthy = listening and timed_call("api_http_validation", wait_for_api,
                    window.wpp_server, window.wpp_port, identity=identity,
                    cancelled=lambda: getattr(window, "_shutting_down", False))
                if not healthy and not restoring and not getattr(window, "_wpp_update_error_key", ""):
                    failure("wpp_update_health_failed" if listening else "wpp_update_start_failed")
                if healthy and not restoring and backup:
                    try:
                        cleanup_path = timed_call("previous_api_detach", api_staging.detach_previous_api,
                                                  api_dir, backup)
                    except Exception:
                        logging.exception("[wpp_update] Previous API retained; cleanup deferred")
            except Exception as exc:
                logging.exception("[wpp_update] API validation failed")
                if not restoring:
                    failure("wpp_update_health_failed", str(exc))
            wx.CallAfter(probed, healthy, restoring, cleanup_path)

        threading.Thread(target=probe, daemon=True, name="wpp-update-validation").start()

    def probed(healthy, restoring, cleanup_path=""):
        if healthy or restoring or not backup or getattr(window, "_shutting_down", False):
            if restoring:
                logging.info("[wpp_update] Previous API restored; health check: %s", healthy)
            try:
                on_done(bool(healthy and not restoring))
            finally:
                if cleanup_path:
                    def cleanup():
                        deadline = time.monotonic() + 90.0
                        while getattr(window, "_wpp_reconnect_started", None) is not None:
                            if getattr(window, "_shutting_down", False):
                                return  # Startup will sweep the detached tree.
                            if time.monotonic() >= deadline:
                                break
                            time.sleep(0.25)
                        timed_call("previous_api_cleanup", api_staging.discard, cleanup_path)
                    threading.Thread(target=cleanup,
                        daemon=True, name="wpp-update-cleanup").start()
            return

        def rollback():
            try:
                window._stop_wpp_server()
                window.wpp_process = None
                released = window.wait_for_profile_release(
                    (getattr(window, "token", "") or "").split(":")[0], timeout=10.0)
                if released is False or not wait_for_port_closed(getattr(window, "_is_wpp_running", lambda: False)):
                    raise TimeoutError("failed API or Chrome profile is still in use")
                failed = timed_call("api_rollback", api_staging.restore_previous_api, api_dir, backup)
                timed_call("failed_api_cleanup", api_staging.discard, failed)
            except Exception as exc:
                logging.exception("[wpp_update] Could not restore previous API; API trees retained")
                previous_key = getattr(window, "_wpp_update_error_key", "")
                previous_details = getattr(window, "_wpp_update_error_details", "")
                i18n = getattr(window, "i18n", None)
                previous = i18n.t(previous_key) if previous_key and i18n else ""
                failure("wpp_update_restore_failed", "\n\n".join(
                    part for part in (previous, previous_details, str(exc)) if part))
                wx.CallAfter(on_done, False)
                return
            wx.CallAfter(start_and_probe, True)

        threading.Thread(target=rollback, daemon=True, name="wpp-update-rollback").start()

    start_and_probe()
