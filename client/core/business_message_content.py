"""Readable business content and cached actions, without I/O or DB migration."""
from core.business_buttons import business_buttons, business_button_label, safe_business_url, MAX_BUTTONS

TYPES = frozenset({"templateMessage", "interactiveMessage", "buttonsMessage", "listMessage"})


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _joined(*parts):
    return "\n".join(value for part in parts if (value := _text(part)))


def _variant(payload):
    """The populated hydrated variant of a stored protobuf-shaped template."""
    node = _dict(payload)
    for _ in range(6):
        if _text(node.get("hydratedContentText")) or node.get("hydratedButtons"):
            return node
        for key in ("hydratedTemplate", "hydratedFourRowTemplate"):
            candidate = _dict(node.get(key))
            if any(candidate.get(k) for k in ("hydratedContentText", "hydratedTitleText",
                                              "hydratedFooterText", "hydratedButtons")):
                return candidate
        if isinstance(node.get("interactiveMessageTemplate"), dict):
            return node["interactiveMessageTemplate"]
        next_node = (_dict(node.get("highlyStructuredMessage")).get("hydratedHsm")
                     or node.get("hydratedHsm") or node.get("templateMessage")
                     or _dict(_dict(node.get("fourRowTemplate")).get("content")).get("hydratedHsm"))
        if not isinstance(next_node, dict):
            return node
        node = next_node
    return {}


def _parts(payload):
    node = _variant(payload)
    return _joined(node.get("hydratedTitleText") or _dict(node.get("header")).get("title"),
                   node.get("hydratedContentText") or node.get("contentText") or node.get("text")
                   or _dict(node.get("body")).get("text"),
                   node.get("hydratedFooterText") or node.get("footerText")
                   or _dict(node.get("footer")).get("text"))


def prepare_business_message(wpp):
    """Accept WPP's flat fields and stored protobuf-shaped template variants.

    Only explicit business types are adapted. Identity, time, history and
    commerce fields survive unchanged; no account lookup or network hydration.
    """
    if not isinstance(wpp, dict) or wpp.get("type") not in ("template", "interactive", "buttons", "list"):
        return wpp
    result = dict(wpp)
    node = _variant(wpp.get("templateMessage") or wpp)
    if wpp.get("type") == "template":
        result["body"] = (_joined(node.get("hydratedTitleText"), _text(wpp.get("body")),
                                  node.get("hydratedFooterText"))
                          if isinstance(wpp.get("body"), str) and wpp.get("body") else _parts(node))
        if not result.get("hydratedButtons") and isinstance(node.get("hydratedButtons"), list):
            result["hydratedButtons"] = node["hydratedButtons"]
        native = _dict(node.get("nativeFlowMessage")).get("buttons")
        if isinstance(native, list):
            result["type"] = "interactive"
            result["interactivePayload"] = {"buttons": [
                {**_dict(button), "buttonParamsJson": _dict(button).get("buttonParamsJson")
                 or _dict(button).get("buttonParamsJSON")} for button in native]}
    return result


def _action_payload(msg):
    content = _dict(_dict(msg.get("message")).get(msg.get("messageType")))
    node = _variant(content)
    payload = {"hydratedButtons": node.get("hydratedButtons"),
               "dynamicReplyButtons": node.get("buttons"),
               "interactivePayload": node.get("interactivePayload") or node.get("nativeFlowMessage"),
               "list": content if msg.get("messageType") == "listMessage" else None}
    native = _dict(payload["interactivePayload"]).get("buttons")
    if isinstance(native, list):
        payload["interactivePayload"] = {"buttons": [
            {**_dict(button), "buttonParamsJson": _dict(button).get("buttonParamsJson")
             or _dict(button).get("buttonParamsJSON")} for button in native]}
    return payload


def message_business_buttons(msg):
    """Read cached actions, or derive them from content already on disk.

    Validate the stored action again at this trust boundary, without repairing
    its original index or guessing a destination for a missing URL.
    """
    if not isinstance(msg, dict):
        return []
    stored = msg.get("businessButtons")
    payload = _action_payload(msg)
    derived = business_buttons(payload)
    if not isinstance(stored, list) or not stored:
        return derived
    content = _dict(_dict(msg.get("message")).get(msg.get("messageType")))
    node = _variant(content)
    retained = set()
    for source, keys in (("template", ("hydratedButtons",)), ("dynamic", ("buttons",)),
                         ("native", ("interactivePayload", "nativeFlowMessage")), ("list", ("sections",))):
        if any(key in node for key in keys):
            retained.add(source)
    # A cached index may have been produced by the old filtering parser.
    # Retained original slots are authoritative; never send its stale index.
    found = list(derived)
    for value in stored[:MAX_BUTTONS]:
        action = _dict(value)
        kind, source, index = action.get("kind"), action.get("source"), action.get("index")
        if source in retained:
            continue
        if (kind not in ("reply", "url", "copy", "call")
                or source not in ("dynamic", "native", "template", "list")
                or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_BUTTONS
                or not _text(action.get("label"))):
            continue
        if kind == "url" and not safe_business_url(action.get("value")):
            continue
        if kind in ("copy", "call") and not _text(action.get("value")):
            continue
        if kind == "reply" and source != "native" and not _text(action.get("value")):
            continue
        clean = dict(action)
        if not isinstance(clean.get("fullLabel"), str):
            clean.pop("fullLabel", None)
        found.append(clean)
    return sorted(found, key=lambda action: (("dynamic", "native", "template", "list").index(action["source"]),
                                             action["index"]))[:MAX_BUTTONS]


def business_message_text(msg, i18n):
    """One full body for rows, previews, copy and notifications in both modes."""
    kind = msg.get("messageType")
    content = _dict(_dict(msg.get("message")).get(kind))
    if kind == "listMessage":
        text = _joined(content.get("title"), content.get("description"), content.get("footerText"))
    else:
        text = _parts(content)
    actions = message_business_buttons(msg)
    if actions:
        labels = [business_button_label(msg, action) for action in actions]
        text = _joined(text, i18n.t("options") + ": " + "; ".join(labels))
    if not text:
        return i18n.t("business_template_unavailable")
    payload = _action_payload(msg)
    known_count = sum(len(value) for value in (
        payload.get("hydratedButtons") or [], payload.get("dynamicReplyButtons") or [],
        _dict(payload.get("interactivePayload")).get("buttons") or []
    ) if isinstance(value, list))
    listing = _dict(payload.get("list"))
    sections = listing.get("sections")
    if isinstance(sections, list):
        known_count += sum(len(section["rows"]) for section in sections if isinstance(section, dict)
                           and isinstance(section.get("rows"), list))
    incomplete = (msg.get("businessTemplateIncomplete") is True or content.get("businessTemplateIncomplete") is True
                  or known_count > len(actions)
                  or isinstance(msg.get("businessButtons"), list) and len(msg["businessButtons"]) > len(actions))
    if incomplete:
        text = _joined(text, i18n.t("business_parts_unavailable"))
    return text
