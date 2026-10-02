"""Bound the existing WPPConnect media route, including older base64 servers."""
import base64
import json

from core.bounded_http import BodyTooLarge, HTTPError, TimeoutError, read_bounded
import requests
from .errors import DescriptionError


def fetch_bounded_media(url, headers, message, max_bytes, cancel_check, timeout, post):
    check = cancel_check or (lambda: None)
    check()
    response = None
    try:
        response = post(url, headers=headers, json=message, timeout=timeout,
                        stream=True, allow_redirects=False)
        check()
        if response.status_code not in (200, 201):
            raise DescriptionError("media")
        content_type = response.headers.get("Content-Type", "").lower()
        as_json = not content_type or "json" in content_type
        limit = max_bytes * 4 // 3 + 4096 if as_json else max_bytes
        body = read_bounded(response, limit, check)
        if as_json:
            encoded = json.loads(body).get("base64", "")
            body = base64.b64decode(encoded, validate=True)
        if len(body) > max_bytes:
            raise DescriptionError("image_size")
        return body
    except BodyTooLarge:
        raise DescriptionError("image_size") from None
    except DescriptionError:
        raise
    except (requests.RequestException, HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
        check()
        raise DescriptionError("media") from None
    finally:
        if response is not None:
            response.close()
