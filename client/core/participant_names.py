"""A name to read aloud for a person the account may not have as a contact."""

_SUFFIXES = ("@lid", "@c.us", "@s.whatsapp.net", "@g.us")


def person_name(mw, jid):
    """The saved or known name of *jid*, else its formatted number, else the
    generic placeholder: never a raw identifier."""
    norm = mw._normalize_jid(jid)
    if mw._is_self_jid(norm):
        return mw.self_reference_label()
    name = mw._resolve_jid_name(norm, resolve_missing=False)
    if not isinstance(name, str) or not name.strip() or name.endswith(_SUFFIXES):
        return mw.i18n.t("unnamed_participant")
    return name.strip()
