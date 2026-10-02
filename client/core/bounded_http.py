"""Bounded, cancellable response reads for opt-in image transfers.

read1 returns available bytes, rather than waiting for an entire chunk while a
slow peer keeps the socket alive. Only public urllib3 APIs are used.
"""
from urllib3.exceptions import HTTPError, TimeoutError


class BodyTooLarge(ValueError):
    pass


def read_bounded(response, limit, check=lambda: None):
    check()
    length = response.headers.get("Content-Length", "")
    try:
        declared = int(length)
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise BodyTooLarge()
    body = bytearray()
    while True:
        check()
        chunk = response.raw.read1(min(4096, limit - len(body) + 1), decode_content=True)
        check()
        if not chunk:
            return bytes(body)
        if len(body) + len(chunk) > limit:
            raise BodyTooLarge()
        body.extend(chunk)
