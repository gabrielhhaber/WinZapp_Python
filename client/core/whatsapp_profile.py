"""File > WhatsApp settings > Profile: what the page sends, decided without wx.

The calls to the local server live in main_window/whatsapp_profile.py and the
page itself in ui/dialogs/whatsapp_profile_panel.py. Everything that is a
decision (which fields go, which photo is acceptable, which failures to list)
is here, so it is tested without opening a window.

A field left empty means "keep it as it is": the page never clears the name,
the About text or the photo, only replaces them.
"""

import os
from dataclasses import dataclass

#: Photo types WPPConnect Server's ``set-profile-pic`` takes, by extension, with
#: the content type the upload declares. Anything else is refused before it is
#: sent, so a wrong pick is explained instead of failing on the server.
PHOTO_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

#: The order the page applies and reports its three fields in.
FIELDS = ("name", "status", "photo")


@dataclass(frozen=True)
class ProfileChanges:
    """What the Profile page will send. An empty value means "keep as is"."""

    name: str = ""
    status: str = ""
    photo: str = ""

    @property
    def empty(self) -> bool:
        return not (self.name or self.status or self.photo)

    def fields(self) -> list:
        """(field, value) for what is actually sent, in the page's order."""
        return [(field, getattr(self, field)) for field in FIELDS if getattr(self, field)]


def profile_changes(name, status, photo) -> ProfileChanges:
    """The changes to send from the raw values of the page's controls.

    Text is stripped, so a field holding only spaces counts as left empty
    instead of asking WhatsApp for a blank name.
    """
    return ProfileChanges(
        name=(name or "").strip(),
        status=(status or "").strip(),
        photo=photo or "",
    )


def photo_content_type(path: str) -> str:
    """The content type to upload *path* as, or "" when it is not a photo the
    server takes (judged by extension, case-insensitively)."""
    return PHOTO_TYPES.get(os.path.splitext(path or "")[1].lower(), "")


def failure_lines(errors: dict, labels: dict) -> list:
    """One "Label: reason" line per field that failed, in the page's order.

    *errors* maps a field to the reason the server gave, *labels* a field to
    the text the page shows for it. A field with no label is named by its key,
    so a failure is never dropped for want of a translation.
    """
    return [
        f"{labels.get(field, field)}: {errors[field]}"
        for field in FIELDS
        if field in errors
    ]
