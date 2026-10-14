"""WhatsAppProfileMixin — part of MainWindow (see main_window/__init__.py).

The paired account's own profile (display name, About text, photo), as
File > WhatsApp settings > Profile (ui/dialogs/whatsapp_profile_panel.py) sets
it. It lives on WhatsApp, not in WinZapp's settings, and is reached through
WPPConnect Server's own routes: ``change-username``, ``profile-status`` and
``set-profile-pic``. Unlike privacy, nothing has to be added to api_patches for
them.

Every method is safe to call from a worker thread (no wx object) and returns
instead of raising: None on success, else an error string, so the page can list
each failed field on its own. The text a person typed never reaches the log.

Methods run with ``self`` bound to the MainWindow instance.
"""

import json
import logging
import os

from core import whatsapp_profile
from core.api_client import api_post


class WhatsAppProfileMixin:
    """Display name, About text and photo of the paired WhatsApp account."""

    #: Seconds. A text change is quick; a photo upload gets longer.
    _PROFILE_TIMEOUT = 15
    _PROFILE_PHOTO_TIMEOUT = 30

    def _profile_url(self, route: str) -> str:
        return f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/{route}"

    @staticmethod
    def _profile_error(response) -> str:
        """What a profile route reports, not just its generic top-level
        message ("Error on set profile name."): the actual failure, a wa-js or
        WPPConnect exception, is in the response's own "error" field, and
        dropping it made every failure read the same."""
        try:
            body = response.json()
        except Exception:
            return f"HTTP {response.status_code}"
        if not isinstance(body, dict):
            return f"HTTP {response.status_code}"
        message = body.get("message") or ""
        error = body.get("error")
        if error and error != message:
            detail = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
            return f"{message} ({detail})" if message else detail
        return message or f"HTTP {response.status_code}"

    def _profile_json_post(self, route: str, payload: dict) -> "str | None":
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            response = api_post(
                self._profile_url(route), json=payload, headers=headers,
                timeout=self._PROFILE_TIMEOUT)
        except Exception as exc:
            return type(exc).__name__
        if response.status_code in (200, 201):
            return None
        logging.warning("[profile] %s not applied: HTTP %s", route, response.status_code)
        return self._profile_error(response)

    def set_profile_name(self, name: str) -> "str | None":
        """Sets the account's displayed WhatsApp name. Error string, or None."""
        return self._profile_json_post("change-username", {"name": name})

    def set_profile_status(self, status: str) -> "str | None":
        """Sets the account's About text. Error string, or None."""
        return self._profile_json_post("profile-status", {"status": status})

    def set_profile_pic(self, image_path: str) -> "str | None":
        """Uploads *image_path* as the account's profile photo. Error string,
        or None.

        A photo is always small, so a plain ``files=`` upload is enough (no
        streaming body, unlike a message attachment). The content type follows
        the file instead of always saying JPEG.
        """
        content_type = whatsapp_profile.photo_content_type(image_path)
        if not content_type:
            return "unsupported photo type"
        # No Content-Type here: multipart sets its own, with the boundary.
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            with open(image_path, "rb") as handle:
                files = {"file": (os.path.basename(image_path), handle, content_type)}
                response = api_post(
                    self._profile_url("set-profile-pic"), files=files, headers=headers,
                    timeout=self._PROFILE_PHOTO_TIMEOUT)
        except Exception as exc:
            return type(exc).__name__
        if response.status_code in (200, 201):
            return None
        logging.warning("[profile] photo not applied: HTTP %s", response.status_code)
        return self._profile_error(response)

    def apply_profile_changes(self, changes) -> dict:
        """Sends every field of *changes* (core.whatsapp_profile.ProfileChanges)
        and returns ``{field: error}`` for the ones that failed, empty when all
        went through. Each field is independent: a refused name does not stop
        the About text or the photo."""
        senders = {
            "name": self.set_profile_name,
            "status": self.set_profile_status,
            "photo": self.set_profile_pic,
        }
        errors = {}
        for field, value in changes.fields():
            error = senders[field](value)
            if error:
                errors[field] = error
        return errors
