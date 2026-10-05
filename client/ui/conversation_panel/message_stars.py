"""Sequential, verified star actions and attended migration of a chat's stars."""

import logging
import threading
import wx

from core.dialog_foreground import message_box
from core.message_stars import (
    STAR_FIELDS, confirmed_star_state, is_local_star, local_unstar_state, merge_star_state,
)


def star_outcome_text(i18n, results, star):
    """One short line for a single star toggle; nothing if it was cancelled."""
    if not results:
        return ""
    _message, _mid, outcome, state = results[0]
    if outcome == "confirmed":
        return i18n.t("star_added" if star else "star_removed")
    if state:
        return i18n.t("star_local_removed")
    return i18n.t({"not_sent": "star_not_sent", "refused": "star_refused"}.get(outcome, "star_unverified"))


class StarActionsMixin:
    def _star_job_valid(self, jid, job, *, allow_cancelled=False):
        mw = self.main_window
        if getattr(mw, "_shutting_down", False) or getattr(mw, "_star_sync_job", None) is not job:
            return False
        if job.get("invalidated") or (job.get("cancelled") and not allow_cancelled):
            return False
        locked = getattr(mw, "is_chat_locked", lambda _j: False)(jid)
        return not locked or bool(getattr(mw, "_chat_lock_unlocked", False))

    def _sync_message_stars(self, jid, messages, star):
        mw = self.main_window
        if getattr(mw, "_star_sync_job", None) is not None:
            mw.output(mw.i18n.t("star_sync_running"), interrupt=True)
            return
        if not jid or not messages:
            return
        job = {"cancelled": False, "jid": jid, "star": bool(star)}
        mw._star_sync_job = job
        items = [(m, dict(m.get("key") or {})) for m in messages]
        if len(items) > 1:
            # One message gets one short outcome instead (_star_outcome_text).
            mw.output(mw.i18n.t("star_sync_running"), interrupt=True)
        threading.Thread(target=self._star_batch_worker,
                         args=(jid, items, bool(star), job), daemon=True).start()

    def _star_batch_worker(self, jid, items, star, job):
        mw = self.main_window
        results = []
        try:
            for message, key in items:
                if not self._star_job_valid(jid, job):
                    break
                try:
                    if message.get("_local_pending") or message.get("_send_unconfirmed") or not key.get("id"):
                        outcome = "not_sent"
                    else:
                        outcome = mw.star_message(jid, key, star)
                    if outcome == "confirmed":
                        state = confirmed_star_state(star)
                    else:
                        # An old local star must stay removable when WhatsApp cannot answer.
                        state = {} if star else local_unstar_state(message)
                    if state and self._star_job_valid(jid, job, allow_cancelled=True):
                        # Only flags change, even if an edit or sync replaced this row.
                        mw.db.update_message_star_state(jid, key["id"], state)
                    results.append((message, key.get("id"), outcome, state))
                except Exception:
                    logging.exception("[star] could not finish a star operation")
                    results.append((message, key.get("id"), "unknown", {}))
        except Exception:
            logging.exception("[star] star job stopped early")
        finally:
            # Always hand back, or _star_sync_job stays set and blocks every later star action.
            wx.CallAfter(self._finish_star_batch, jid, job, results, len(items))

    def _finish_star_batch(self, jid, job, results, total):
        mw = self.main_window
        try:
            valid = self._star_job_valid(jid, job, allow_cancelled=True)
        finally:
            if getattr(mw, "_star_sync_job", None) is job:
                mw._star_sync_job = None
        if not valid:
            return
        chat = mw.chats.get(jid, {})
        records = chat.get("messages", {}).get("messages", {}).get("records", [])
        by_id = {(m.get("key") or {}).get("id"): m for m in records}
        changed = []
        for original, mid, outcome, state in results:
            if not state:
                continue
            for message in (original, by_id.get(mid)):
                if message is not None:
                    merged = merge_star_state({**message, **state}, message)
                    message.update({k: merged[k] for k in STAR_FIELDS if k in merged})
            changed.append(mid)
        if changed:
            # Memory carries the new flags only from here; a save_data() full
            # rewrite during the batch may have put the old ones back on disk.
            states = [(mid, state) for _m, mid, _o, state in results if state]
            mw._msg_bg_executor.submit(self._rewrite_star_states, jid, states)
        if (self.conversation or {}).get("remoteJid") == jid and changed:
            self._repaint_or_repopulate(changed)
        if total == 1:
            text = star_outcome_text(mw.i18n, results, job.get("star", True))
            if text:
                mw.output(text, interrupt=True)
            return
        counts = {kind: sum(r[2] == kind for r in results)
                  for kind in ("confirmed", "refused", "unknown")}
        counts["refused"] += sum(r[2] == "not_sent" for r in results)
        text = mw.i18n.t("star_sync_result").format(
            **counts, skipped=total - len(results), chat=mw.chat_display_name(jid))
        if counts["unknown"]:
            text += " " + mw.i18n.t("star_sync_unknown")
        mw.output(text, interrupt=True)

    def _rewrite_star_states(self, jid, states):
        for mid, state in states:
            try:
                self.main_window.db.update_message_star_state(jid, mid, state)
            except Exception:
                logging.exception("[star] could not rewrite a star flag")

    def _on_sync_local_stars(self, jid):
        """Read local history in pages; the only write is after confirmation."""
        mw = self.main_window
        if getattr(mw, "_star_sync_job", None) is not None:
            mw.output(mw.i18n.t("star_sync_running"), interrupt=True)
            return
        job = {"cancelled": False, "jid": jid}
        mw._star_sync_job = job
        if not self._star_job_valid(jid, job):
            mw._star_sync_job = None
            return
        mw.output(mw.i18n.t("star_sync_scanning"), interrupt=True)
        threading.Thread(target=self._collect_local_stars, args=(jid, job), daemon=True).start()

    def _on_cancel_star_sync(self):
        job = getattr(self.main_window, "_star_sync_job", None)
        if job is not None:
            job["cancelled"] = True
            self.main_window.output(self.main_window.i18n.t("star_sync_stopping"), interrupt=True)

    def _collect_local_stars(self, jid, job):
        messages, seen, offset = [], set(), 0
        try:
            while self._star_job_valid(jid, job):
                page = self.main_window.db.get_messages(jid, limit=500, offset=offset)
                for message in page:
                    mid = (message.get("key") or {}).get("id")
                    if (mid and mid not in seen and is_local_star(message)
                            and not self._is_system_event(message)):
                        messages.append(message)
                        seen.add(mid)
                if len(page) < 500:
                    break
                offset += len(page)
            wx.CallAfter(self._confirm_local_stars, jid, job, messages)
        except Exception:
            logging.exception("[star] local star scan failed")
            wx.CallAfter(self._local_star_scan_failed, jid, job)

    def _local_star_scan_failed(self, jid, job):
        valid = self._star_job_valid(jid, job)
        if getattr(self.main_window, "_star_sync_job", None) is job:
            self.main_window._star_sync_job = None
        if valid:
            self.main_window.output(self.main_window.i18n.t("star_sync_scan_failed"), interrupt=True)

    def _confirm_local_stars(self, jid, job, messages):
        mw = self.main_window
        valid = self._star_job_valid(jid, job)
        if not valid:
            if getattr(mw, "_star_sync_job", None) is job:
                mw._star_sync_job = None
            return
        if not messages:
            mw._star_sync_job = None
            mw.output(mw.i18n.t("star_sync_none"), interrupt=True)
            return
        text = mw.i18n.t("star_sync_confirm").format(
            count=len(messages), chat=mw.chat_display_name(jid))
        answer = message_box(mw, text, mw.i18n.t("star_sync_local"),
                             wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                             announce=lambda: mw.output(text, interrupt=True))
        # Lock/shutdown may have happened while the confirmation was open.
        valid = self._star_job_valid(jid, job)
        mw._star_sync_job = None
        if answer == wx.YES and valid:
            self._sync_message_stars(jid, messages, True)
