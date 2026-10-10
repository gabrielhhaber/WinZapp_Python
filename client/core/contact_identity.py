"""Identity lines for the chat-info dialog, read from the ``/contact/`` reply.

WPPConnect serialises the WhatsApp contact model into that reply, so a few
fields the dialog never showed are already there: the name the person chose
for themselves (``pushname``), the name WhatsApp verified for a business
(``verifiedName``) and whether the account is a business (``isBusiness``).
Nothing here asks the server for anything new; every field is optional and a
missing or oddly typed one simply produces no line.
"""


def _clean_text(value) -> str:
    """A displayable string, or "" for anything that is not real text."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _same_name(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def contact_identity_lines(cdata, shown_name, i18n) -> list:
    """Extra "label: value" lines for a personal chat, in reading order.

    *cdata* is the ``response`` object of ``/contact/<jid>`` (anything else
    yields no lines). *shown_name* is the name the dialog already displays, so
    a profile name or verified name that merely repeats it is not read out a
    second time. *i18n* is the window's translator; it is passed whole, not just its
    ``t`` method, because the tooling that keeps the catalogs in step only
    recognises calls made on an object named i18n.
    """
    if not isinstance(cdata, dict):
        return []
    shown = _clean_text(shown_name)
    lines = []
    seen = [shown] if shown else []

    def unseen(text: str) -> bool:
        return bool(text) and not any(_same_name(text, other) for other in seen)

    profile_name = _clean_text(cdata.get("pushname"))
    if unseen(profile_name):
        lines.append(f"{i18n.t('contact_profile_name_label')}: {profile_name}")
        seen.append(profile_name)

    verified_name = _clean_text(cdata.get("verifiedName"))
    if unseen(verified_name):
        lines.append(f"{i18n.t('contact_verified_name_label')}: {verified_name}")
        seen.append(verified_name)

    # Strictly True: a string such as "false" must not read as a business.
    if cdata.get("isBusiness") is True:
        lines.append(i18n.t("contact_business_account"))

    return lines
