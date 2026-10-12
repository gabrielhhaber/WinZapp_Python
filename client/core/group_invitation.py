"""Invitation transport. Never persist links, QR data, or native exceptions."""

import base64
from core.api_client import api_get, api_post
from core.group_operations import invitation_link


def connection_identity(main_window):
    return main_window.wpp_server, main_window.wpp_port, main_window.token


def read_invitation(connection, jid, reset=False):
    server, port, token = connection
    url = f"{server}:{port}/api/{token}/group-invitation/{jid}"
    request = api_post if reset else api_get
    if reset:
        url += "/reset"
    response = request(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    body = response.json()
    if response.status_code != 200 or not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError("invitation_unconfirmed")
    data = body.get("response")
    if not isinstance(data, dict) or not invitation_link(data.get("link")):
        raise ValueError("invitation_unconfirmed")
    encoded = data.get("qr")
    prefix = "data:image/png;base64,"
    if not isinstance(encoded, str) or not encoded.startswith(prefix) or len(encoded) > 512_000:
        raise ValueError("invitation_unconfirmed")
    png = base64.b64decode(encoded[len(prefix):], validate=True)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invitation_unconfirmed")
    return data["link"], png
