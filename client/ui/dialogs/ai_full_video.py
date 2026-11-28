"""AIResultDialog's "Understand full video": one text that joins what is seen (the
mosaic of frames, as the plain description gets it) with what is said (the
video's sound, transcribed on this computer).

The steps, in this order: the transcription already saved with the video, or a
local one behind the progress window with Cancel (ui/conversation_panel/
ai_full_video.py supplies both); then a consent of its own that names the
pictures *and* the transcribed text, and the providers that would get them;
then the request. The sound never leaves the computer. A failed or missing
transcription is said plainly and the person may go on with the pictures only,
as the ordinary description: the answer never pretends to cover what was not
sent.

The consent is asked once per window and never remembered: the ordinary
description's consent (remembered per provider) says nothing about speech text,
and a summary deliberately does not send a video's speech. The answer is not
put in the message line: the line's description and transcription are other
results that this text must not overwrite (core/transcription/stored.py).

Methods run with ``self`` bound to the AIResultDialog.
"""
import wx

from app_paths import global_dir
from core.ai_credentials import CredentialError, CredentialStore
from core.ai_media import config as ai_config
from core.ai_media import full_video
from core.ai_media.errors import DescriptionError


def full_video_consent_text(i18n, providers, locked, config, truncated=False):
    """What the person is told before the pictures and the speech text leave
    the machine. ``providers`` are the ones that would receive them."""
    names = ", ".join(ai_config.PROVIDERS[p].name for p in providers)
    message = i18n.t("ai_full_video_consent").format(providers=names)
    message += "\n\n" + i18n.t(
        "ai_video_frame_notice" if config.get("video_frames", "several") == "one"
        else "ai_video_frames_notice")
    if truncated:
        message += "\n\n" + i18n.t("ai_full_video_cut_notice").format(
            chars=full_video.MAX_SPEECH_CHARS)
    for key in ai_config.notice_keys(providers, config):
        message += "\n\n" + i18n.t(key)
    if locked:
        message += "\n\n" + i18n.t("ai_locked_consent")
    return message


def answer_notes(t, first, speech, images_only):
    """What is added, after the answer's status, to the first answer of a
    request: that it covers only the pictures, and that the speech text was cut.
    "" for a follow-up (``first`` False) and for any other answer."""
    if not first:
        return ""
    notes = []
    if images_only:
        notes.append(t("ai_full_video_images_only"))
    if speech and speech.get("truncated"):
        notes.append(t("ai_full_video_cut_status").format(chars=full_video.MAX_SPEECH_CHARS))
    return " " + " ".join(notes) if notes else ""


class AIFullVideoMixin:
    def _build_full_video_button(self, row):
        self.full_video = self._button(row, "ai_full_video", self._full_video)

    def _full_video_say(self, key):
        text = self.i18n.t(key)
        self.status.ChangeValue(text)
        self._speak(text)

    def _full_video_cancel(self):
        """The window is closing while the transcription runs: stop it; the
        flow below then finishes the close."""
        if self._full_hooks is not None:
            self._full_hooks["cancel"]()

    def _full_video(self, event=None):
        if (not self._valid() or self.session.active is not None or self._full_running
                or self._full_hooks is None):
            return
        self._full_running = True
        try:
            self._full_video_flow()
        finally:
            self._full_running = False
            # EndModal only after every window this flow opened has returned.
            if self.session.closed and self:
                self._finish_close()

    def _full_video_speech(self):
        """``(status, text, reason)`` of the speech step: the saved transcription,
        what an earlier click here found, or a local run."""
        hooks = self._full_hooks
        saved = hooks["saved_text"]()
        if saved:
            return full_video.SPEECH_OK, saved, ""
        if self._full_cache is not None:
            return self._full_cache
        found = hooks["transcribe"](self)
        answer = (found["status"], found["text"], found["reason"])
        if answer[0] in (full_video.SPEECH_OK, full_video.SPEECH_NONE):
            self._full_cache = answer
        return answer

    def _full_video_flow(self):
        try:
            if not self._chain(CredentialStore(global_dir())):
                raise DescriptionError("providers")
        except (DescriptionError, CredentialError) as exc:
            self._show_error(exc.key if isinstance(exc, DescriptionError) else str(exc))
            return
        status, text, reason = self._full_video_speech()
        if not self._valid():
            return
        step = full_video.next_step(status)
        payload = full_video.speech_payload(text) if step == full_video.NEXT_SEND else None
        if step == full_video.NEXT_SEND and payload is None:
            step = full_video.NEXT_IMAGES_ONLY
        if step == full_video.NEXT_STOP:
            self._full_video_say("ai_full_video_busy" if status == full_video.SPEECH_BUSY
                                 else "ai_full_video_stopped")
        elif step == full_video.NEXT_IMAGES_ONLY:
            self._full_video_say("ai_full_video_no_speech")
            self._regenerate(images_only=True)
        elif step == full_video.NEXT_ASK:
            # The reason first, then the way on: the pictures alone, said as such.
            go_on = wx.MessageBox(
                self.i18n.t("ai_full_video_failed").format(reason=reason),
                self.i18n.t("ai_full_video").replace("&", ""),
                wx.YES_NO | wx.ICON_QUESTION, self) == wx.YES
            if not self._valid():
                return
            if go_on:
                self._regenerate(images_only=True)
            else:
                self._full_video_say("ai_full_video_declined")
        else:
            self._start(full_video.QUESTION, regenerate=True, speech=payload)

    def _full_video_consent(self, providers, speech):
        """Consent to send the pictures and the speech text to every provider
        that would receive them (a local server on this computer asks nothing).
        Once per window, never remembered, never covered by the description's
        own consent; True when the request may go."""
        from ui.dialogs.ai_result_dialog import AIConsentDialog
        outbound = ai_config.outbound(self.config, providers)
        if not outbound or set(outbound) <= self._full_consented:
            return True
        text = full_video_consent_text(self.i18n, outbound, self.session.locked, self.config,
                                       bool(speech and speech.get("truncated")))
        dialog = AIConsentDialog(self, self.i18n, outbound, "video", self.session.locked, text=text)
        dialog.remember.SetValue(False)
        dialog.remember.Enable(False)
        dialog.remember.Hide()
        self._consent_dialog = dialog
        try:
            accepted = dialog.ShowModal() == wx.ID_OK and self._valid()
        finally:
            self._consent_dialog = None
            dialog.Destroy()
            # The flow that asked finishes a close (see _full_video); a follow-up does it here.
            if self.session.closed and self and not self._full_running:
                self._finish_close()
        if accepted:
            self._full_consented |= set(outbound)
        return accepted
