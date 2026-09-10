"""Strict normalization for WPPConnect send responses."""


class SendContractError(ValueError):
    """A send response that could not be confirmed as accepted.

    The message text is a developer diagnostic written to the log and never
    shown to anyone: it is English, and WinZapp's UI is translated into five
    locales. Callers that have to pick a user-facing string switch on
    ``reason`` instead of matching that text — ``"rejected"`` means WhatsApp
    itself refused the message (a negative ACK), everything else is
    ``"unconfirmed"``: the response simply never proved the send happened.
    """

    def __init__(self, message, reason="unconfirmed"):
        super().__init__(message)
        self.reason = reason


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

    # WhatsApp's own verdict is read *before* the embedded error text, because
    # the Node side annotates a refused send with both halves at once:
    # describeSendRejection() (messageController.ts) writes
    # "send-file was rejected (ack=-1)" into result.error, so a body carrying a
    # negative ACK always carries an error string too. Testing the text first
    # made reason="rejected" a branch no production response could ever reach,
    # and send_media_attachment() then told the user to go and check a
    # conversation where — the send having been refused — there is nothing.
    ack = result.get("ack")
    try:
        numeric_ack = int(ack) if ack is not None else None
    except (TypeError, ValueError):
        raise SendContractError(f"invalid ACK value: {ack!r}")
    rejected = numeric_ack is not None and numeric_ack < 0

    embedded_error = result.get("error") or result.get("erro")
    if embedded_error:
        if isinstance(embedded_error, dict):
            embedded_error = embedded_error.get("message") or repr(embedded_error)
        raise SendContractError(
            str(embedded_error), "rejected" if rejected else "unconfirmed"
        )
    if rejected:
        raise SendContractError(f"WhatsApp rejected the send (ack={ack})", "rejected")

    # The same set describeSendRejection() accepts, and only that set: the two
    # halves are one contract, and a value Node calls a rejection must not be
    # a success here.
    send_result = (result.get("sendMsgResult") or {}).get("messageSendResult")
    if send_result is not None:
        normalized = str(send_result).upper()
        if normalized not in {"SUCCESS", "OK"}:
            raise SendContractError(f"WhatsApp rejected the send ({send_result})")

    raw_id = result.get("id") or (result.get("key") or {}).get("id") or result.get("messageId")
    if isinstance(raw_id, dict):
        raw_id = raw_id.get("_serialized") or raw_id.get("id")
    message_id = str(raw_id or "").strip()
    if not message_id:
        raise SendContractError("success response has no message id")
    parts = message_id.split("_")
    return parts[2] if len(parts) > 2 else parts[-1]


def send_failure_is_ambiguous(status_code) -> bool:
    """Whether a failed send might nevertheless have reached WhatsApp.

    A 5xx from a send endpoint is `returnError` on the Node side — an
    exception raised somewhere inside the controller — and by then WPPConnect
    has usually already handed the message to WhatsApp Web. Measured on a real
    install on 2026-09-09, and the ordering is the whole point:

        16:17:04.744  echo  id=3EB0B499A4020A9246C939  extendedTextMessage
        16:17:04.754  POST /send-reply -> 500

    The echo of the delivered reply arrived **ten milliseconds before** the
    error response for the very request that sent it. Everything the caller
    does after that 500 is acting on a message that is already on its way.

    So a 5xx proves nothing, and no caller may answer it by sending again:
    send_text_message()'s own fallbacks used to retry it without the quote,
    which put a second copy in the conversation (the user's report: "the reply
    shows up correctly and is then duplicated"). The shape to answer with is
    the one _classify_send_exception() already uses for a timeout —
    ``retry: False, ambiguous: True`` — so the queue drops the message rather
    than resending it and the WebSocket echo resolves the pending row if and
    when WhatsApp really delivers it.

    4xx is the opposite: the controller rejected the request before doing
    anything with it (a bad phone, a quoted message it cannot find), so a
    fallback there is free. That is the distinction the fallbacks lost by
    testing "not 200/201".

    An unreadable status is treated as ambiguous, because the only cost of
    being wrong that way is a message the user has to send again — against a
    duplicate nobody can take back.
    """
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return True
    return code >= 500
