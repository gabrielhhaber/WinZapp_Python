"""Pure helpers of the Status tab: post-result checks, the status content
label, media download and the save-as extension table.

Moved verbatim out of status_panel.py, which re-exports every name.
"""

import base64
import time
from core.utils import is_voice_message


def _post_was_rejected(body) -> bool:
    """True when a send-text-storie response actually means FAILURE.

    With the status.layer.js async patch, WPPConnect answers HTTP 201 even
    when WhatsApp Web rejected the status at protocol level — the rejection
    is carried inside the payload as ``sendMsgResult.messageSendResult``
    (e.g. ``"ERROR_UNKNOWN"``, with ``ack`` staying 0). A null/empty response
    is also a failure.
    """
    if not isinstance(body, dict):
        return True
    resp_data = body.get("response")
    if isinstance(resp_data, list) and resp_data:
        for item in resp_data:
            if isinstance(item, dict):
                s = (item.get("sendMsgResult") or {}).get("messageSendResult")
                if s and s not in ("SUCCESS", "OK"):
                    return True
        return False
    return resp_data is None


def _download_status_media(main_window, status: dict, attempts: int = 4) -> bytes:
    """Wait for pending status media instead of misreporting it as corrupt."""
    last_error = None
    for attempt in range(attempts):
        try:
            encoded = main_window.get_base64_from_media(status)
            if encoded:
                return base64.b64decode(encoded)
        except Exception as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(1.0)
    raise ValueError(str(last_error or "empty media response"))


def _status_content_label(msg_type: str, msg_obj: dict, i18n, settings: dict = None) -> str:
    """Human-readable content label for one status update.

    Shared by MyStatusDialog, StatusPanel's list-row preview, and
    StatusPanel's own open viewer — a status can be an audio/document/
    sticker/contact update just like a regular message, not only text/
    image/video. Each of those three call sites used to fall through to
    the raw messageType string itself (e.g. literal "audioMessage") for
    anything past text/image/video instead of a translated label.
    """
    if msg_type == "conversation":
        return msg_obj.get("conversation", "")
    if msg_type == "extendedTextMessage":
        return (msg_obj.get("extendedTextMessage") or {}).get("text", "")
    if msg_type == "imageMessage":
        caption = ((msg_obj.get("imageMessage") or {}).get("caption") or "").strip()
        return f"{i18n.t('photo')}: {caption}" if caption else i18n.t("photo")
    if msg_type == "videoMessage":
        caption = ((msg_obj.get("videoMessage") or {}).get("caption") or "").strip()
        return f"{i18n.t('video')}: {caption}" if caption else i18n.t("video")
    if msg_type in ("audioMessage", "audio", "ptt"):
        vm_mode = (settings.get("user_interface", {}) if isinstance(settings, dict) else {}).get("voice_message_mode", "voice_message")
        if vm_mode == "voice_message":
            is_ptt = is_voice_message(msg_obj) or bool(isinstance(msg_obj, dict) and is_voice_message({"messageType": "audioMessage", "message": msg_obj}))
            return i18n.t("message_type_voice_message") if is_ptt else i18n.t("message_type_audio")
        return i18n.t("message_type_audio")
    if msg_type == "documentMessage":
        doc = msg_obj.get("documentMessage") or {}
        filename = doc.get("fileName") or doc.get("title") or ""
        return f"{i18n.t('document')}: {filename}" if filename else i18n.t("document")
    if msg_type == "stickerMessage":
        return i18n.t("sticker")
    if msg_type == "contactMessage":
        contact = msg_obj.get("contactMessage") or {}
        name  = contact.get("displayName") or ""
        vcard = contact.get("vcard") or ""
        # Same vCard-leak bug as MainWindow._get_message_content /
        # ConversationsPanel._get_message_content (issue #22): displayName
        # is sometimes empty, or is itself the raw vCard blob — parse the
        # FN: line instead of ever putting BEGIN:VCARD...END:VCARD on screen.
        if not name or "BEGIN:VCARD" in name:
            vcard_to_parse = name if "BEGIN:VCARD" in name else vcard
            parsed_name = ""
            for line in vcard_to_parse.splitlines():
                if line.startswith("FN:"):
                    parsed_name = line[3:].strip()
                    break
            name = parsed_name or i18n.t("unknown_contact")
        return i18n.t("contact_message").format(name=name)
    # Everything else a contact can post (location, live location, poll,
    # contacts array, commerce, interactive, view-once...) already has a
    # label in the notification formatter; before, each of them showed as
    # "Incompatible message" in the Status tab.
    try:
        from core.notification_manager import format_notification_body
        return format_notification_body(
            {"messageType": msg_type, "message": msg_obj}, None, i18n)
    except Exception:
        return i18n.t("notif_unsupported")


# Shared by both status media-save entry points — the classic "Salvar
# mídia" button/shortcut (_status_media_save_info(), used by
# StatusPanel._on_save_status_media() when Settings > Interface do
# usuário > "Mostrar os status em player separado" is unchecked) and the
# unified MediaViewerDialog's own Save As (_status_to_media_viewer_item()).
# Both used to compute the extension independently — a bare
# mimetype.split("/")[-1] here vs. a canonicalizing table there — so the
# very same image/jpeg status photo saved as status.jpeg from one button
# and status.jpg from the other. One table now backs both.
_STATUS_MIME_SUBTYPE_TO_EXT = {
    "jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp",
    "gif": ".gif", "mp4": ".mp4", "webm": ".webm",
    "ogg": ".ogg", "opus": ".opus", "mpeg": ".mp3", "mp3": ".mp3",
    "mp4a-latm": ".m4a", "x-m4a": ".m4a", "aac": ".aac",
    "wav": ".wav", "x-wav": ".wav", "flac": ".flac",
}


def _status_media_extension(mimetype: str, default_ext: str) -> str:
    """Canonical file extension for a status media mimetype, falling back
    to *default_ext* (with the leading dot) when the mimetype is missing
    or its subtype isn't in the table above."""
    mime = str(mimetype or "").split(";")[0].strip().lower()
    if "/" not in mime:
        return default_ext
    subtype = mime.split("/", 1)[1]
    return _STATUS_MIME_SUBTYPE_TO_EXT.get(subtype, "." + subtype.split("+")[0])


def _status_media_save_info(msg_type: str, msg_obj: dict, i18n):
    """Returns (ext, wildcard) for the "Save media as..." dialog, or None
    if *msg_type* isn't a savable media status. Shared by
    StatusPanel._on_save_status_media() so the extension/wildcard logic
    for each media type lives in one place."""
    if msg_type == "imageMessage":
        mimetype = (msg_obj.get("imageMessage") or {}).get("mimetype", "image/jpeg")
        ext = _status_media_extension(mimetype, ".jpg")
        return ext, f"{i18n.t('photo')} (*{ext})|*{ext}|*.*|*.*"
    if msg_type == "videoMessage":
        mimetype = (msg_obj.get("videoMessage") or {}).get("mimetype", "video/mp4")
        ext = _status_media_extension(mimetype, ".mp4")
        return ext, f"{i18n.t('video')} (*{ext})|*{ext}|*.*|*.*"
    if msg_type == "audioMessage":
        mimetype = (msg_obj.get("audioMessage") or {}).get("mimetype", "audio/ogg")
        ext = _status_media_extension(mimetype, ".ogg")
        return ext, f"{i18n.t('message_type_audio')} (*{ext})|*{ext}|*.*|*.*"
    return None


#: Uploads go through Node to WhatsApp before anything answers, so the read
#: timeout has to cover the whole upload. The figures are the download's
#: (main_window/message_rules.media_fetch_timeout()): pessimistic bytes per
#: second, capped at 30 minutes.
_STATUS_UPLOAD_BYTES_PER_SECOND = 200 * 1024
_STATUS_UPLOAD_TIMEOUT_CEILING = 30 * 60

#: What a post that timed out is: it may well have gone out, so it is never
#: reported (or retried) as a failure.
STATUS_POST_UNCONFIRMED = "unconfirmed"


def status_upload_timeout(size_bytes, base: int = 60) -> int:
    """Seconds to wait for a status post of a *size_bytes* file. A flat 60 s
    gave up on a video still being uploaded: the user was told it failed,
    posted again, and the status went out twice. The payload is base64 (a
    third larger). `base` when the size is unknown."""
    try:
        size = int(size_bytes)
    except (TypeError, ValueError):
        return base
    if size <= 0:
        return base
    sent = size * 4 / 3
    return int(min(_STATUS_UPLOAD_TIMEOUT_CEILING, base + sent / _STATUS_UPLOAD_BYTES_PER_SECOND))
