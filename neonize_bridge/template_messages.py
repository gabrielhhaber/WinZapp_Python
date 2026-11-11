"""Template variants -> existing business-message fields, without I/O.

Unhydrated HSM parameters are not a translated sentence. Never invent their
body, URL or reply IDs; keep readable siblings and report incomplete content.
"""


def has(message, field):
    try:
        return message.HasField(field)
    except (AttributeError, ValueError):
        return False


def _header(body):
    title = getattr(body, "hydratedTitleText", "")
    for field in ("documentMessage", "imageMessage", "videoMessage", "locationMessage"):
        if has(body, field):
            media = getattr(body, field)
            title = (getattr(media, "caption", "") or getattr(media, "fileName", "")
                     or getattr(media, "title", "") or getattr(media, "name", "") or title)
            return title, field
    return title, ""


def template_fields(message, hydrated_buttons, interactive_fields):
    """Resolve only explicit populated variants, bounded to six HSM layers."""
    if has(message, "templateMessage"):
        template = message.templateMessage
    elif has(message, "highlyStructuredMessage") and has(message.highlyStructuredMessage, "hydratedHsm"):
        template = message.highlyStructuredMessage.hydratedHsm
    elif has(message, "highlyStructuredMessage"):
        return {"type": "template", "body": "", "businessTemplateIncomplete": True}
    else:
        return None
    prefix = []
    incomplete = False
    for _ in range(6):
        for name in ("hydratedTemplate", "hydratedFourRowTemplate"):
            if not has(template, name):
                continue
            body = getattr(template, name)
            title, header_kind = _header(body)
            parts = [title, body.hydratedContentText, body.hydratedFooterText]
            if any(parts) or body.hydratedButtons or header_kind:
                result = {"type": "template", "body": "\n".join(p for p in prefix + parts if p),
                          "hydratedButtons": hydrated_buttons(body)}
                if header_kind or incomplete:
                    # Media downloads need a separate owner/record contract. Do
                    # not present a filename as proof the attachment is available.
                    result["businessHeaderKind"] = header_kind
                    result["businessTemplateIncomplete"] = True
                return result
        if has(template, "interactiveMessageTemplate"):
            result = interactive_fields(template.interactiveMessageTemplate)
            result["body"] = "\n".join(part for part in prefix + [result.get("body", "")] if part)
            if incomplete:
                result["businessTemplateIncomplete"] = True
            return result
        if has(template, "fourRowTemplate"):
            legacy = template.fourRowTemplate
            title, header_kind = _header(legacy)
            if title:
                prefix.append(title)
            incomplete = True
            if has(legacy, "content") and has(legacy.content, "hydratedHsm"):
                template = legacy.content.hydratedHsm
                continue
        break
    return {"type": "template", "body": "\n".join(prefix), "hydratedButtons": [],
            "businessTemplateIncomplete": True}
