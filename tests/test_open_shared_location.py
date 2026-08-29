"""Tests for opening a shared location (GitHub issue #36).

Received locations couldn't be opened/viewed at all, unlike on the phone
client. WinZapp has no in-app map view, so "opening" one hands its
coordinates to the system's default map/browser handler — the same way an
image or document already opens in its associated app (Enter/double-click,
or the visible "Open" action button/label).
"""

from ui.conversations import ConversationsPanel


def _location_msg(msg_type="locationMessage", lat=-23.55, lng=-46.63):
    inner = {}
    if lat is not None:
        inner["degreesLatitude"] = lat
    if lng is not None:
        inner["degreesLongitude"] = lng
    return {
        "key": {"id": "LOC1", "fromMe": False},
        "message": {msg_type: inner},
        "messageType": msg_type,
    }


class _PanelStub:
    _location_maps_url = ConversationsPanel._location_maps_url


class TestLocationMapsUrl:
    def test_builds_google_maps_url_from_coordinates(self):
        panel = _PanelStub()
        url = panel._location_maps_url(_location_msg())
        assert url == "https://www.google.com/maps/search/?api=1&query=-23.55,-46.63"

    def test_live_location_message_type_also_works(self):
        panel = _PanelStub()
        url = panel._location_maps_url(_location_msg(msg_type="liveLocationMessage"))
        assert "query=-23.55,-46.63" in url

    def test_missing_latitude_returns_none(self):
        panel = _PanelStub()
        assert panel._location_maps_url(_location_msg(lat=None)) is None

    def test_missing_longitude_returns_none(self):
        panel = _PanelStub()
        assert panel._location_maps_url(_location_msg(lng=None)) is None

    def test_non_numeric_coordinates_return_none(self):
        panel = _PanelStub()
        msg = _location_msg()
        msg["message"]["locationMessage"]["degreesLatitude"] = "not-a-number"
        assert panel._location_maps_url(msg) is None

    def test_wrong_message_type_returns_none(self):
        panel = _PanelStub()
        msg = {"key": {"id": "X"}, "message": {"conversation": "oi"}, "messageType": "conversation"}
        assert panel._location_maps_url(msg) is None


class TestActivationOpensLocation:
    def test_enter_on_location_routes_to_open_action(self):
        """Enter/double-click/Space on a location must dispatch through
        _on_action_open() — same path images/documents already use — rather
        than doing nothing, checked at source level since driving the whole
        activation flow needs a live wx.App and populated message list."""
        import inspect

        src = inspect.getsource(ConversationsPanel.activate_message)
        assert '"locationMessage"' in src
        assert '"liveLocationMessage"' in src

    def test_open_action_opens_the_maps_url_for_locations(self):
        # The Open action's body now lives in open_media_message(): the group
        # data dialog's Media tab holds messages that are not in this panel's
        # list at all (it reads the group's whole history from the database),
        # so it needs a by-message entry point rather than a row index.
        # _on_action_open() is the thin row-index wrapper over it.
        src_open = __import__("inspect").getsource(
            ConversationsPanel.open_media_message)
        assert "_location_maps_url" in src_open
        assert "open_media_message" in __import__("inspect").getsource(
            ConversationsPanel._on_action_open)
