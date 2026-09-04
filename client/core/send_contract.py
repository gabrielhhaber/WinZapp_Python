"""Strict normalization for WPPConnect send responses."""


class SendContractError(ValueError):
    pass


def accepted_message_id(body) -> str:
    """Return a confirmed message id or reject WPPConnect false-success data."""
    if not isinstance(body, dict):
        raise SendContractError("invalid JSON response")
    api_status = str(body.get("status", "")).lower()
    if api_status and api_status != "success":
        raise SendContractError(str(body.get("message") or "API did not report success"))

    result = body.get("response")
    if isinstance(result, list):
        if not result:
            raise SendContractError("empty send result")
        result = result[0]
    if isinstance(result, str):
        if result.strip():
            return result.strip()
        raise SendContractError("empty message id")
    if not isinstance(result, dict):
        raise SendContractError("send result is missing")

    embedded_error = result.get("error") or result.get("erro")
    if embedded_error:
        if isinstance(embedded_error, dict):
            embedded_error = embedded_error.get("message") or repr(embedded_error)
        raise SendContractError(str(embedded_error))

    ack = result.get("ack")
    try:
        numeric_ack = int(ack) if ack is not None else None
    except (TypeError, ValueError):
        raise SendContractError(f"invalid ACK value: {ack!r}")
    if numeric_ack is not None and numeric_ack < 0:
        raise SendContractError(f"WhatsApp rejected the send (ack={ack})")

    send_result = (result.get("sendMsgResult") or {}).get("messageSendResult")
    if send_result is not None:
        normalized = str(send_result).upper()
        if normalized not in {"SUCCESS", "OK", "0"}:
            raise SendContractError(f"WhatsApp rejected the send ({send_result})")

    raw_id = result.get("id") or (result.get("key") or {}).get("id") or result.get("messageId")
    if isinstance(raw_id, dict):
        raw_id = raw_id.get("_serialized") or raw_id.get("id")
    message_id = str(raw_id or "").strip()
    if not message_id:
        raise SendContractError("success response has no message id")
    parts = message_id.split("_")
    return parts[2] if len(parts) > 2 else parts[-1]
