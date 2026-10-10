"""Groups a contact and the user are both in, for the chat-info dialog.

The data is the server's own ``GET /api/<session>/common-groups/<wid>``, which
runs wa-js's ``WPP.contact.getCommonGroups``. Nothing about WPPConnect changes;
this is only the client reading a route that was already there.

Three properties of that call shape this module:

* The reply is the bare array wa-js returns (no ``{"status", "response"}``
  envelope), and its items are WhatsApp ``Wid`` objects. Whether they arrive
  as strings or as objects carrying ``_serialized`` is not something to bet on,
  so both are read, and an envelope is tolerated too.
* wa-js answers ``[]`` both for "no groups in common" and for "that contact is
  not loaded in the page", so an empty list is never proof of anything. The
  other JID form of the same person (phone <-> @lid) is therefore tried before
  settling on "none".
* "Could not ask" must stay distinguishable from "asked, nothing there": the
  first returns None, the second [].
"""

import logging

from core.api_client import api_get

_GROUP_SUFFIX = "@g.us"
_TIMEOUT = 15


def group_jids_from_reply(payload):
    """The group JIDs in a ``common-groups`` reply, or None if it is not one.

    None means the reply was not the list (or envelope around a list) this
    function understands; [] means it was a list with no group in it.
    """
    if isinstance(payload, dict):
        payload = payload.get("response")
    if not isinstance(payload, list):
        return None

    found = []
    for item in payload:
        if isinstance(item, dict):
            jid = item.get("_serialized")
            if not isinstance(jid, str):
                user, server = item.get("user"), item.get("server")
                jid = f"{user}@{server}" if isinstance(user, str) and isinstance(server, str) else ""
        else:
            jid = item
        if isinstance(jid, str) and jid.endswith(_GROUP_SUFFIX) and jid not in found:
            found.append(jid)
    return found


def wid_candidates(jid, lid_to_phone, phone_to_lid) -> list:
    """The JID forms worth asking about for one person, most direct first.

    A chat can be indexed by phone or by @lid while wa-js has the contact
    under the other one, so the known counterpart is asked second.
    """
    if not isinstance(jid, str) or not jid or jid.endswith(_GROUP_SUFFIX):
        return []
    forms = [jid]
    for mapping in (lid_to_phone, phone_to_lid):
        other = (mapping or {}).get(jid)
        if isinstance(other, str) and other and other not in forms:
            forms.append(other)
    return forms


def fetch_common_groups(owner, jid):
    """Group JIDs shared with *jid*, or None when the server could not say.

    *owner* is the main window (server address, token and the @lid maps).
    Blocking: call it from a background thread.
    """
    forms = wid_candidates(
        jid,
        getattr(owner, "_lid_to_phone", {}),
        getattr(owner, "_phone_to_lid", {}),
    )
    headers = {"Authorization": f"Bearer {owner.token}"}
    answered = False
    for wid in forms:
        url = f"{owner.wpp_server}:{owner.wpp_port}/api/{owner.token}/common-groups/{wid}"
        try:
            response = api_get(url, headers=headers, timeout=_TIMEOUT)
            status = response.status_code
            groups = group_jids_from_reply(response.json()) if status == 200 else None
        except Exception as exc:
            logging.warning("[common_groups] request failed: %s", type(exc).__name__)
            continue
        if groups is None:
            logging.info("[common_groups] unusable reply (status=%s)", status)
            continue
        answered = True
        if groups:
            return groups
    return [] if answered else None


def common_group_rows(jids, name_for, fallback_name) -> list:
    """``(name, jid)`` pairs sorted by name, for the list control.

    *name_for* maps a group JID to its name and may return "" or raise;
    *fallback_name* stands in for a group that has none, so a raw JID is never
    shown or read out.
    """
    rows = []
    for jid in jids:
        try:
            name = (name_for(jid) or "").strip()
        except Exception:
            name = ""
        rows.append((name or fallback_name, jid))
    rows.sort(key=lambda row: (row[0].casefold(), row[1]))
    return rows
