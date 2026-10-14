"""Carts and orders: the contracts, what a row says, and what the windows
decide, without creating a wx window.

What the fixtures carry was read from a real, non-business account over CDP
(2026-10-13, WhatsApp Web 2.3000.1049387914): a business's catalog, its cart
after add / update / remove, and then, with the owner's leave, one real order
sent to a business, read back and deleted for everyone. The order message's
fields below (itemCount, status, message, totalAmount1000, totalCurrencyCode)
are the ones that order carried. The seller's side needs a business account,
and no order update ever arrived on that account, so those follow WhatsApp
Web's own program text (WAWebOrderStatus, WAWebOrderDetails), and the tests
hold the code to never claiming more than it read.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import commerce
from core.commerce import (
    ACCEPTED, DECLINED, INQUIRY, ORDER_MESSAGE_TYPE, ORDER_UPDATE_MESSAGE_TYPE, PRODUCT_MESSAGE_TYPE, Account,
    Cart, CartItem,
    Catalog, CommerceError, Order, Product,
)
from core.notification_manager import format_notification_body
from core.websocket_client import WebSocketClient
from main import is_countable_message
from main_window.commerce import CommerceMixin
from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings, mnemonic
from ui.conversation_panel.commerce import CommercePanelMixin
from ui.conversations import ConversationsPanel
from ui.dialogs.cart_dialog import CartDialog
from ui.dialogs.order_create_dialog import OrderCreateDialog, new_item_label
from ui.dialogs.order_dialog import OrderDialog

CONNECTION = ("http://localhost", 6300, "synthetic-token")
HEADERS = {"Authorization": "Bearer synthetic-token"}
BASE = "http://localhost:6300/api/synthetic-token/"
SELLER = "777@lid"
ORDER_ID = "false_777@lid_3EB0AAAAAAAAAAAAAAAA"
STRINGS = load_strings("en-US")
I18N = SimpleNamespace(t=lambda key: STRINGS[key].replace("&", ""))

BREAD = Product("100", "Bread", "Fresh", 7500, "BRL", True)
CAKE = Product("300", "Cake", "", 45000, "BRL", False)
TWO_BREADS = Cart((CartItem("100", "Bread", 2, 7500, "BRL"),), 15000, "BRL")
EMPTY = Cart((), None, "")
RAW_CART = {"items": [{"id": "100", "name": "Bread", "quantity": 2, "price": 7500, "currency": "BRL"}],
            "itemCount": 2, "total": 15000, "currency": "BRL", "message": ""}
RAW_EMPTY = {"items": [], "itemCount": 0, "total": None, "currency": "", "message": ""}
RAW_CATALOG = {"catalog": True, "cartEnabled": True, "cart": RAW_EMPTY, "products": [
    {"id": "100", "name": "Bread", "description": "Fresh", "price": 7500, "currency": "BRL", "available": True},
    {"id": "300", "name": "Cake", "description": "", "price": 45000, "currency": "BRL", "available": False}]}
RAW_ORDER = {"order": {"mine": False, "status": 1, "note": "please", "subtotal": 15000, "total": 15000,
                       "currency": "BRL", "createdAt": 1790000000, "canAnswer": True, "canUpdate": False,
                       "products": [{"id": "100", "name": "Bread", "price": 7500, "currency": "BRL", "quantity": 2}]}}
RECEIVED = Order(False, INQUIRY, "please", (CartItem("100", "Bread", 2, 7500, "BRL"),), 15000, 15000, "BRL",
                 True, False)


def answer(body, status=200):
    return SimpleNamespace(status_code=status, json=lambda: body)


def ok(data):
    return answer({"status": "success", "response": data})


# ── amounts ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount,currency,separator,text", [
    (7500, "BRL", ",", "R$ 7,50"), (15000, "USD", ".", "US$ 15.00"), (1234560, "EUR", ",", "€ 1234,56"),
    (5, "BRL", ",", "R$ 0,01"), (0, "BRL", ",", "R$ 0,00"), (7500, "XYZ", ".", "XYZ 7.50"),
    (7500, "", ".", "7.50"), (None, "BRL", ",", ""), (-1, "BRL", ",", ""), (True, "BRL", ",", ""),
    ("7500", "BRL", ",", "")])
def test_an_amount_in_thousandths_is_said_as_money_or_not_at_all(amount, currency, separator, text):
    assert commerce.format_money(amount, currency, separator) == text


@pytest.mark.parametrize("typed,amount", [
    ("12", 12000), ("12,5", 12500), ("12.50", 12500), (" 0,05 ", 50), ("1.234,56", 1234560),
    ("1,234.56", 1234560), ("1.234", 1234000), ("1234", 1234000),
    ("", None), ("abc", None), ("12,345,6", None), ("-3", None), ("1,2,3", None), (None, None)])
def test_a_typed_price_is_read_in_either_decimal_convention(typed, amount):
    assert commerce.parse_money(typed) == amount


# ── answers ────────────────────────────────────────────────────────────────

def test_the_servers_answers_become_values():
    assert commerce.parse_catalog(RAW_CATALOG) == Catalog(True, True, (BREAD, CAKE), EMPTY)
    assert commerce.parse_cart(RAW_CART) == TWO_BREADS and TWO_BREADS.item_count == 2
    assert commerce.parse_order(RAW_ORDER) == RECEIVED
    assert commerce.parse_account({"business": False, "cartEnabled": None, "sellers": [SELLER, "5511@c.us", "1-0@g.us", 7]}) \
        == Account(False, None, frozenset({SELLER, "5511@c.us"}))
    assert commerce.parse_account({"business": True, "cartEnabled": True}).cart_enabled is True


@pytest.mark.parametrize("parse,data", [
    (commerce.parse_catalog, None), (commerce.parse_catalog, {"products": "x"}),
    (commerce.parse_catalog, {"products": [], "cart": None}), (commerce.parse_cart, {"items": None}),
    (commerce.parse_order, {}), (commerce.parse_order, {"order": []}),
    (commerce.parse_account, {"business": "yes"}), (commerce.parse_account, None)])
def test_an_answer_of_another_shape_is_refused(parse, data):
    with pytest.raises(CommerceError):
        parse(data)


def test_what_is_not_an_item_a_number_or_a_permission_is_dropped_never_guessed():
    cart = commerce.parse_cart({"items": [{"id": "", "quantity": 1}, None, {"id": "1", "quantity": 0},
                                          {"id": "2", "quantity": "3"}, {"id": "3", "quantity": 2, "price": "7500"}],
                                "total": "15000", "currency": 5})
    assert cart == Cart((CartItem("3", "", 2, None, ""),), None, "")
    order = commerce.parse_order({"order": {"mine": 1, "status": 9, "canAnswer": "true", "canUpdate": 1}})
    assert (order.mine, order.status, order.can_answer, order.can_update) == (False, None, False, False)
    catalog = commerce.parse_catalog({"catalog": "yes", "cartEnabled": 1, "cart": RAW_EMPTY,
                                      "products": [{"id": "1", "available": "true"}]})
    assert (catalog.has_catalog, catalog.cart_enabled, catalog.products[0].available) == (False, False, False)


# ── transport ──────────────────────────────────────────────────────────────

def test_reading_is_a_get_on_the_commerce_routes(monkeypatch):
    get = Mock(side_effect=[ok({"business": False, "cartEnabled": None, "sellers": [SELLER]}), ok(RAW_CATALOG),
                            ok(RAW_ORDER)])
    monkeypatch.setattr(commerce, "api_get", get)
    assert commerce.read_account(CONNECTION).sellers == frozenset({SELLER})
    assert commerce.read_catalog(CONNECTION, SELLER).products == (BREAD, CAKE)
    assert commerce.read_order(CONNECTION, ORDER_ID) == RECEIVED
    assert [call.args[0] for call in get.call_args_list] == [
        BASE + "commerce/account", BASE + "commerce/catalog/777@lid",
        BASE + "commerce/order/false_777%40lid_3EB0AAAAAAAAAAAAAAAA"]
    assert all(call.kwargs["headers"] == HEADERS for call in get.call_args_list)


@pytest.mark.parametrize("call,path,payload", [
    (lambda: commerce.set_cart_quantity(CONNECTION, SELLER, "100", 2), "cart/777@lid/set", {"productId": "100", "quantity": 2}),
    (lambda: commerce.remove_from_cart(CONNECTION, SELLER, "100"), "cart/777@lid/remove", {"productId": "100"}),
    (lambda: commerce.clear_cart(CONNECTION, SELLER), "cart/777@lid/clear", {}),
])
def test_each_cart_change_is_one_post_and_answers_the_cart(monkeypatch, call, path, payload):
    post = Mock(return_value=ok({"cart": RAW_CART}))
    monkeypatch.setattr(commerce, "api_post", post)
    assert call() == TWO_BREADS
    post.assert_called_once_with(BASE + "commerce/" + path, json=payload, headers=HEADERS, timeout=45)


def test_sending_a_cart_is_one_post_and_needs_the_servers_word(monkeypatch):
    post = Mock(return_value=ok({"sent": True, "id": "true_777@lid_X", "cart": RAW_EMPTY}))
    monkeypatch.setattr(commerce, "api_post", post)
    assert commerce.submit_cart(CONNECTION, SELLER, "  thanks ") == EMPTY
    post.assert_called_once_with(BASE + "commerce/cart/777@lid/submit", json={"message": "thanks"},
                                 headers=HEADERS, timeout=90)
    monkeypatch.setattr(commerce, "api_post", Mock(return_value=ok({"cart": RAW_EMPTY})))
    with pytest.raises(CommerceError) as error:
        commerce.submit_cart(CONNECTION, SELLER)
    assert error.value.code == "unconfirmed"


@pytest.mark.parametrize("call,path,payload", [
    (lambda: commerce.accept_order(CONNECTION, ORDER_ID), "accept", {}),
    (lambda: commerce.decline_order(CONNECTION, ORDER_ID, " Sold out "), "decline", {"note": "Sold out"}),
    (lambda: commerce.update_order(CONNECTION, ORDER_ID, "shipped", "On its way"), "status",
     {"status": "shipped", "note": "On its way"}),
    (lambda: commerce.update_order(CONNECTION, ORDER_ID, "completed", payment_status="captured"), "status",
     {"status": "completed", "note": "", "paymentStatus": "captured"}),
])
def test_a_sellers_answer_is_one_post_and_returns_the_state_read_back(monkeypatch, call, path, payload):
    post = Mock(return_value=ok({"accepted": True, "status": 2}))
    monkeypatch.setattr(commerce, "api_post", post)
    assert call() == ACCEPTED
    post.assert_called_once_with(BASE + "commerce/order/false_777%40lid_3EB0AAAAAAAAAAAAAAAA/" + path,
                                 json=payload, headers=HEADERS, timeout=90)
    monkeypatch.setattr(commerce, "api_post", Mock(return_value=ok({"status": 2})))
    with pytest.raises(CommerceError):
        call()  # no "accepted": not an answer


@pytest.mark.parametrize("call", [
    lambda: commerce.read_catalog(CONNECTION, "111-0@g.us"),
    lambda: commerce.set_cart_quantity(CONNECTION, SELLER, "100", 0),
    lambda: commerce.set_cart_quantity(CONNECTION, SELLER, "100", 100),
    lambda: commerce.set_cart_quantity(CONNECTION, SELLER, "100", "2"),
    lambda: commerce.remove_from_cart(CONNECTION, SELLER, "../1"),
    lambda: commerce.submit_cart(CONNECTION, SELLER, "x" * 1025),
    lambda: commerce.read_order(CONNECTION, "false_111-0@g.us_3EB0AAAAAAAAAAAAAAAA"),
    lambda: commerce.update_order(CONNECTION, ORDER_ID, "teleported"),
    lambda: commerce.update_order(CONNECTION, ORDER_ID, "shipped", payment_status="free"),
    lambda: commerce.send_order(CONNECTION, SELLER, []),
    lambda: commerce.send_order(CONNECTION, SELLER, [("", 1000, 1)]),
    lambda: commerce.send_order(CONNECTION, SELLER, [("Bread", 0, 1)]),
    lambda: commerce.send_order(CONNECTION, SELLER, [("Bread", 1000, 100)]),
])
def test_a_malformed_request_is_never_sent(monkeypatch, call):
    get, post = Mock(), Mock()
    monkeypatch.setattr(commerce, "api_get", get)
    monkeypatch.setattr(commerce, "api_post", post)
    with pytest.raises(CommerceError):
        call()
    assert not get.called and not post.called


def test_a_refusal_keeps_the_servers_code_and_the_cart_it_carried(monkeypatch):
    refusal = answer({"status": "error", "code": "product_unavailable", "response": {"cart": RAW_CART}}, 409)
    monkeypatch.setattr(commerce, "api_post", Mock(return_value=refusal))
    with pytest.raises(CommerceError) as error:
        commerce.set_cart_quantity(CONNECTION, SELLER, "999", 1)
    assert error.value.code == "product_unavailable" and error.value.cart == TWO_BREADS


@pytest.mark.parametrize("response", [
    answer({"status": "error", "code": "commerce_unavailable"}, 503), answer([], 200), answer({"status": "error"}, 500)])
def test_anything_else_is_unavailable(monkeypatch, response):
    monkeypatch.setattr(commerce, "api_get", Mock(return_value=response))
    with pytest.raises(CommerceError) as error:
        commerce.read_catalog(CONNECTION, SELLER)
    assert error.value.code == "unavailable" and error.value.cart is None
    monkeypatch.setattr(commerce, "api_get", Mock(side_effect=TimeoutError("777@lid")))
    with pytest.raises(CommerceError) as error:
        commerce.read_catalog(CONNECTION, SELLER)
    assert error.value.code == "unavailable"


def test_the_two_upstream_routes_are_used_as_they_are(monkeypatch):
    post = Mock(return_value=answer({"anything": 1}, 201))
    monkeypatch.setattr(commerce, "api_post", post)
    commerce.send_order(CONNECTION, SELLER, [(" Bread ", 7500, 2)])
    post.assert_called_with(BASE + "send-order-message", headers=HEADERS, timeout=90, json={
        "phone": SELLER, "isGroup": False, "items": [{"type": "custom", "name": "Bread", "price": 7500, "qnt": 2}]})
    commerce.set_cart_enabled(CONNECTION, True)
    post.assert_called_with(BASE + "set-cart-enabled", json={"enabled": True}, headers=HEADERS, timeout=90)
    monkeypatch.setattr(commerce, "api_post", Mock(return_value=answer({}, 500)))
    with pytest.raises(CommerceError):
        commerce.set_cart_enabled(CONNECTION, False)
    assert commerce.order_total([("a", 7500, 2), ("b", 1000, 1)]) == 16000


# ── messages ───────────────────────────────────────────────────────────────

class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


def raw_order(from_me=False, **fields):
    """An `order` message with the fields WhatsApp Web's own parser fills
    (WAWebParseOrderMessageProto)."""
    message = {"id": f"{'true' if from_me else 'false'}_{SELLER}_3EB0AAAAAAAAAAAAAAAA", "type": "order",
               "t": 1790000000, "timestamp": 1790000000, "from": SELLER, "to": "999@lid", "fromMe": from_me,
               "body": "", "orderId": "O1", "token": "T1", "sellerJid": "5511900000001@s.whatsapp.net",
               "orderTitle": "Bakery", "itemCount": 3, "status": 1, "message": "please",
               "totalAmount1000": 15000, "totalCurrencyCode": "BRL",
               "chatId": {"server": "lid", "user": "777", "_serialized": SELLER}}
    message.update(fields)
    return message


def raw_update(flow="order_status", params=None, **fields):
    params = {"reference_id": "REF1", "order": {"status": "shipped"}} if params is None else params
    message = {"id": f"false_{SELLER}_3EB0CCCCCCCCCCCCCCCC", "type": "interactive", "t": 1790000100,
               "from": SELLER, "to": "999@lid", "fromMe": False, "body": "", "nativeFlowName": flow,
               "interactivePayload": {"buttons": [{"name": flow, "buttonParamsJson": json.dumps(params)}]},
               "chatId": {"server": "lid", "user": "777", "_serialized": SELLER}}
    message.update(fields)
    return message


def normalized(raw):
    return _Normalizer()._normalize_wpp_message(raw)


def test_an_order_message_keeps_what_a_row_needs_and_nothing_secret():
    message = normalized(raw_order())
    assert message["messageType"] == ORDER_MESSAGE_TYPE
    assert message["message"] == {ORDER_MESSAGE_TYPE: {
        "itemCount": 3, "total": 15000, "currency": "BRL", "note": "please", "status": 1}}
    assert "T1" not in json.dumps(message)  # the token that opens the order is not stored


def test_fields_of_another_type_are_dropped_from_an_order():
    payload = commerce.order_payload({"itemCount": "3", "totalAmount1000": 0, "status": 9, "message": 7,
                                      "totalCurrencyCode": None})
    assert payload == {"itemCount": 0, "total": None, "currency": "", "note": "", "status": None}


def test_a_sellers_follow_up_is_recognised_by_its_flow_and_its_reference():
    message = normalized(raw_update(params={
        "reference_id": "REF1", "currency": "BRL", "total_amount": {"value": 1500, "offset": 100},
        "order": {"status": "payment_requested"}, "payment": {"status": "pending"}}, flow="order_details"))
    assert message["messageType"] == ORDER_UPDATE_MESSAGE_TYPE
    assert message["message"][ORDER_UPDATE_MESSAGE_TYPE] == {
        "reference": "REF1", "status": "payment_requested", "payment": "pending", "total": 15000, "currency": "BRL"}


@pytest.mark.parametrize("raw", [
    raw_update(flow="cta_url"),                                   # another interactive message
    raw_update(params={"order": {"status": "shipped"}}),          # no reference: not an order's
    raw_update(interactivePayload={"buttons": [{"buttonParamsJson": "{not json"}]}),
    raw_update(interactivePayload={"buttons": []}), raw_update(interactivePayload=None)])
def test_any_other_interactive_message_is_left_to_its_own_handling(raw):
    assert commerce.order_update_payload(raw) is None
    assert normalized(raw)["messageType"] not in (ORDER_UPDATE_MESSAGE_TYPE, ORDER_MESSAGE_TYPE)


def raw_product(**fields):
    """A `product` message with the fields WhatsApp Web's own parser fills
    (WAWebParseProductMessageProto). `body` is the picture's thumbnail."""
    message = {"id": f"false_{SELLER}_3EB0DDDDDDDDDDDDDDDD", "type": "product", "t": 1790000200, "from": SELLER,
               "to": "999@lid", "fromMe": False, "body": "/9j/4AAQSkZJRgABAQAAAQABAAD" + "A" * 400,
               "title": "Bread", "description": "Fresh every morning", "priceAmount1000": 7500,
               "currencyCode": "BRL", "productId": "100", "businessOwnerJid": "5511900000001@s.whatsapp.net",
               "productImageCount": 1, "url": "https://wa.me/p/100/5511900000001",
               "chatId": {"server": "lid", "user": "777", "_serialized": SELLER}}
    message.update(fields)
    return message


def test_a_product_of_a_catalog_is_a_row_with_its_name_and_price_never_its_thumbnail():
    message = normalized(raw_product())
    assert message["messageType"] == PRODUCT_MESSAGE_TYPE
    assert message["message"] == {PRODUCT_MESSAGE_TYPE: {
        "title": "Bread", "description": "Fresh every morning", "price": 7500, "currency": "BRL", "productId": "100"}}
    assert "/9j/" not in json.dumps(message)
    assert commerce.message_label(message, I18N) == "Product: Bread, R$ 7.50. Fresh every morning"
    on_sale = normalized(raw_product(salePriceAmount1000=5000, description=""))
    assert commerce.message_label(on_sale, I18N) == "Product: Bread, R$ 5.00"
    bare = normalized(raw_product(title=None, priceAmount1000=None, description=7, productId="../x"))
    assert bare["message"][PRODUCT_MESSAGE_TYPE]["productId"] == ""
    assert commerce.message_label(bare, I18N) == "Product: Unnamed product"


def test_unknown_statuses_are_not_repeated_to_the_user():
    payload = commerce.order_update_payload(raw_update(params={
        "reference_id": "R", "order": {"status": "<b>hacked</b>"}, "payment_status": "free",
        "total_amount": {"value": -1, "offset": 0}}))
    assert payload == {"reference": "R", "status": "", "payment": "", "total": None, "currency": ""}


def test_a_row_says_whose_cart_it_is_how_much_and_its_state():
    received = normalized(raw_order())
    assert commerce.order_label(received, I18N) == "Order received: 3 items, estimated total R$ 15.00. please"
    sent = normalized(raw_order(from_me=True, status=2, message="", itemCount=1, totalAmount1000=0))
    assert commerce.order_label(sent, I18N) == "Cart sent: 1 item, accepted"
    declined = normalized(raw_order(status=3, message=""))
    assert commerce.order_label(declined, I18N).endswith("estimated total R$ 15.00, declined")
    update = normalized(raw_update(params={
        "reference_id": "REF1", "currency": "BRL", "total_amount": {"value": 15000, "offset": 1000},
        "order": {"status": "shipped"}, "payment": {"status": "captured"}}))
    assert commerce.order_update_label(update, I18N) == \
        "Order update, reference REF1: Shipped, payment: received, total R$ 15.00"
    bare = {"messageType": ORDER_UPDATE_MESSAGE_TYPE, "message": {ORDER_UPDATE_MESSAGE_TYPE: {}}}
    assert commerce.order_update_label(bare, I18N) == "Order update"
    assert commerce.message_label({"messageType": "conversation"}, I18N) is None


class _Panel:
    _get_message_content = ConversationsPanel._get_message_content
    _is_displayable_message = ConversationsPanel._is_displayable_message
    _is_separator = staticmethod(lambda m: False)

    def __init__(self):
        self.main_window = SimpleNamespace(i18n=I18N, app_name="WinZapp")
        self._download_progress = {}


@pytest.mark.parametrize("raw,start", [(raw_order(), "Order received: 3 items"),
                                       (raw_update(), "Order update, reference REF1: Shipped"),
                                       (raw_product(), "Product: Bread, R$ 7.50")])
def test_orders_reach_the_list_the_notification_and_the_unread_count(raw, start):
    message = normalized(raw)
    panel = _Panel()
    assert panel._is_displayable_message(message)
    assert panel._get_message_content(message).startswith(start)
    assert format_notification_body(message, None, I18N).startswith(start)
    assert is_countable_message(message)


def test_an_order_is_the_chat_preview_with_the_same_sentence_as_its_row():
    from main import MainWindow

    class _MainWindowStub:
        _counts_as_last_message = classmethod(MainWindow._counts_as_last_message.__func__)
        _last_msg_preview = MainWindow._last_msg_preview
        _PREVIEW_MESSAGE_TYPES = MainWindow._PREVIEW_MESSAGE_TYPES
        i18n = I18N
        settings = {"user_interface": {"show_delivery_status_in_chat_list": False}}
        conversations_panel = None

        def self_reference_label(self):
            return "Me"

    message = normalized(raw_order())
    assert MainWindow._counts_as_last_message(message) is True
    chat = {"remoteJid": "5511900000001@s.whatsapp.net", "messages": {"messages": {"records": [message]}}}
    assert "Order received: 3 items, estimated total R$ 15.00" in _MainWindowStub()._last_msg_preview(chat)


def test_catalog_and_cart_rows():
    assert commerce.product_label(BREAD, I18N) == "Bread, R$ 7.50, Fresh"
    assert commerce.product_label(CAKE, I18N) == "Cake, R$ 45.00, out of stock"
    assert commerce.product_label(Product("1", "", "", None, "", True), I18N) == "Unnamed product"
    assert commerce.item_label(TWO_BREADS.items[0], I18N) == "2 × Bread, R$ 7.50 each"
    assert commerce.cart_summary(TWO_BREADS, I18N) == "2 items, estimated total R$ 15.00"
    assert commerce.cart_summary(EMPTY, I18N) == "The cart is empty."
    assert new_item_label(("Bread", 7500, 2), I18N) == "2 × Bread, 7.50 each"


# ── the account's facts ────────────────────────────────────────────────────

class _Now:
    """threading.Thread, run on the spot."""
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class _MainWindow:
    for _name in ("refresh_commerce_account", "commerce_account", "is_business_account", "is_seller_chat",
                  "commerce_chat_id", "toggle_cart_enabled", "_finish_cart_toggle"):
        locals()[_name] = getattr(CommerceMixin, _name)
    del _name
    wpp_server, wpp_port, token = CONNECTION
    _normalize_jid = staticmethod(lambda jid: jid.replace("@c.us", "@s.whatsapp.net"))

    def __init__(self):
        self._lid_to_phone = {"777@lid": "5511900000001@s.whatsapp.net"}
        self._phone_to_lid = {"5511900000001@s.whatsapp.net": "777@lid"}
        self.i18n = I18N
        self.speak_output = SimpleNamespace(output=Mock())


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr("main_window.commerce.threading.Thread", _Now)
    monkeypatch.setattr("main_window.commerce._CART_READ_PAUSE", 0)
    monkeypatch.setattr("main_window.commerce.wx.CallAfter", lambda fn, *args: fn(*args))
    return _MainWindow()


def test_nothing_commercial_is_known_until_the_server_says_so(window, monkeypatch):
    read = Mock(side_effect=CommerceError())
    monkeypatch.setattr(commerce, "read_account", read)
    assert window.commerce_account() is None
    assert not window.is_business_account() and not window.is_seller_chat("777@lid")
    assert read.call_count == 3  # a failed reading is asked for again, not cached


def test_a_reading_is_kept_and_not_asked_for_again_at_once(window, monkeypatch):
    read = Mock(return_value=Account(False, None, frozenset({"777@lid", "5511900000002@c.us"})))
    monkeypatch.setattr(commerce, "read_account", read)
    assert window.commerce_account().business is False
    # One person under every form of their id; a group never.
    assert window.is_seller_chat("777@lid") and window.is_seller_chat("5511900000001@s.whatsapp.net")
    assert window.is_seller_chat("5511900000002@s.whatsapp.net")
    assert not window.is_seller_chat("5511900000003@s.whatsapp.net") and not window.is_seller_chat("111-0@g.us")
    assert not window.is_business_account()
    read.assert_called_once_with(CONNECTION)
    window.refresh_commerce_account(force=True)
    assert read.call_count == 2


def test_a_reading_for_another_connection_is_thrown_away(window, monkeypatch):
    def read(connection):
        window.token = "another-account"
        return Account(True, True, frozenset())
    monkeypatch.setattr(commerce, "read_account", read)
    window.refresh_commerce_account()
    assert window.__dict__.get("_commerce_account") is None


def test_a_chat_is_addressed_by_its_lid_when_known(window):
    assert window.commerce_chat_id("5511900000001@s.whatsapp.net") == "777@lid"
    assert window.commerce_chat_id("5511900000009@s.whatsapp.net") == "5511900000009@c.us"
    assert window.commerce_chat_id("888@lid") == "888@lid"


def test_the_cart_setting_says_what_the_server_reports_not_what_was_asked(window, monkeypatch):
    setting = Mock()
    monkeypatch.setattr(commerce, "set_cart_enabled", setting)
    window._commerce_account = Account(True, False, frozenset())
    monkeypatch.setattr(commerce, "read_account", Mock(return_value=Account(True, True, frozenset())))
    window.toggle_cart_enabled()
    setting.assert_called_once_with(CONNECTION, True)
    assert window.speak_output.output.call_args.args[0] == "Your catalog now accepts carts."
    assert window._commerce_account.cart_enabled is True
    # Asked to turn it off, and the server still says on: not announced as done.
    window.toggle_cart_enabled()
    assert window.speak_output.output.call_args.args[0] == "The cart setting could not be changed."
    # A consumer account has no such setting.
    window._commerce_account = Account(False, None, frozenset())
    setting.reset_mock()
    window.toggle_cart_enabled()
    setting.assert_not_called()


# ── menus ──────────────────────────────────────────────────────────────────

class _Menu:
    def __init__(self):
        self.items = []

    def Append(self, _id, label):
        self.items.append(label)
        return Mock()

    def AppendCheckItem(self, _id, label):
        item = Mock()
        self.items.append((label, item))
        return item

    def AppendSubMenu(self, menu, label):
        self.items.append((label, menu.items))


class _ChatPanel:
    for _name in ("_append_commerce_items", "_can_view_order", "_show_order", "_show_catalog",
                  "_show_order_create", "_chat_name"):
        locals()[_name] = getattr(CommercePanelMixin, _name)
    del _name

    def __init__(self, account, seller=False, jid="5511900000001@s.whatsapp.net"):
        self.main_window = SimpleNamespace(
            i18n=I18N, commerce_account=lambda: account, is_seller_chat=lambda chat: seller,
            is_business_account=lambda: bool(account and account.business),
            commerce_chat_id=lambda chat: "777@lid", _resolve_contact_name=lambda chat: "Bakery",
            _serialize_msg_id=Mock(return_value=ORDER_ID), toggle_cart_enabled=Mock())
        self.conversation = {"remoteJid": jid}
        self.Bind = Mock()
        self._show_modal = Mock()


def menu_of(panel, monkeypatch):
    monkeypatch.setattr("ui.conversation_panel.commerce.wx.Menu", _Menu)
    menu = _Menu()
    panel._append_commerce_items(menu, panel.conversation)
    return menu.items


def test_a_consumer_sees_the_catalog_only_in_a_chat_with_a_business(monkeypatch):
    consumer = Account(False, None, frozenset())
    assert menu_of(_ChatPanel(consumer, seller=True), monkeypatch) == ["Business catalog and cart...	Alt+Shift+B"]
    assert menu_of(_ChatPanel(consumer, seller=False), monkeypatch) == []
    assert menu_of(_ChatPanel(None, seller=False), monkeypatch) == []  # no reading yet: nothing offered


def test_a_business_account_gets_its_own_tools_and_nobody_else_does(monkeypatch):
    items = menu_of(_ChatPanel(Account(True, True, frozenset())), monkeypatch)
    [(label, tools)] = items
    assert label == "Business tools" and tools[0] == "Create an order for this contact...	Ctrl+Shift+N"
    text, check = tools[1]
    assert text == "Accept carts in my catalog"
    check.Check.assert_called_once_with(True)
    # A business whose own setting could not be read is not shown a guess.
    [(_label, tools)] = menu_of(_ChatPanel(Account(True, None, frozenset())), monkeypatch)
    assert tools == ["Create an order for this contact...	Ctrl+Shift+N"]


def test_an_order_opens_from_its_message_in_a_one_to_one_chat(monkeypatch):
    opened = Mock()
    monkeypatch.setattr("ui.dialogs.order_dialog.OrderDialog", opened)
    order = normalized(raw_order())
    panel = _ChatPanel(Account(False, None, frozenset()))
    assert panel._can_view_order(order) and not panel._can_view_order({"messageType": "conversation"})
    assert not _ChatPanel(None, jid="111-0@g.us")._can_view_order(order)
    panel._show_order(order)
    opened.assert_called_once_with(panel.main_window, panel.main_window, ORDER_ID, "Bakery")
    panel._show_modal.assert_called_once()
    creating = Mock()
    monkeypatch.setattr("ui.dialogs.order_create_dialog.OrderCreateDialog", creating)
    panel._show_order_create(panel.conversation)
    creating.assert_not_called()  # not a business account


# ── windows, on stubs ──────────────────────────────────────────────────────

class _ListBox:
    def __init__(self):
        self.items, self.selection = [], -1
        self.Freeze, self.Thaw = Mock(), Mock()

    def Set(self, items):
        self.items, self.selection = list(items), -1

    def SetSelection(self, index):
        self.selection = index

    def GetSelection(self):
        return self.selection


class _Window:
    """What every stub below shares: a connection that is current, work that
    is queued instead of threaded, and what was last said."""
    def _base(self):
        self._connection, self._busy = CONNECTION, False
        self._i18n = I18N
        self._mw = SimpleNamespace(i18n=I18N, speak_output=SimpleNamespace(output=Mock()))
        self._closed = SimpleNamespace(is_set=lambda: False, set=Mock())
        self._status = SimpleNamespace(SetLabel=Mock())
        self.work = []
        self.EndModal = Mock()
        self._confirm = Mock(return_value=True)

    def _current(self):
        return True

    def _run_async(self, work, done):
        self.work.append((work, done))

    def run(self):
        work, done = self.work.pop()
        done(work())

    def enabled(self, name):
        return getattr(self, name).Enable.call_args.args[0]

    def said(self):
        return self._mw.speak_output.output.call_args.args[0]

    def buttons(self, *names):
        for name in names:
            setattr(self, name, SimpleNamespace(Enable=Mock()))


class _Cart(_Window):
    for _name in ("_product", "_item", "_carts", "_set_busy", "_update_buttons", "_fill", "_show_cart", "_reload",
                  "_finish_reload", "_change_cart", "_finish_change", "_set_quantity", "_on_add", "_on_change",
                  "_on_remove", "_on_clear", "_on_send", "_finish_send", "_announce"):
        locals()[_name] = getattr(CartDialog, _name)
    del _name

    def __init__(self, catalog=Catalog(True, True, (BREAD, CAKE), EMPTY)):
        self._base()
        self._chat, self._seller, self._catalog, self._cart, self._sent = SELLER, "Bakery", None, EMPTY, False
        self._unconfirmed = False
        self._products, self._items = _ListBox(), _ListBox()
        self._note = SimpleNamespace(Enable=Mock(), GetValue=lambda: "thanks")
        self.buttons("_add", "_change", "_remove", "_clear", "_send", "_refresh")
        self._ask_quantity = Mock(return_value=2)
        if catalog is not None:
            self._finish_reload(catalog)


def test_the_catalog_lists_products_and_says_how_many_and_what_the_cart_holds(monkeypatch):
    monkeypatch.setattr(commerce, "read_catalog", Mock(return_value=Catalog(True, True, (BREAD, CAKE), TWO_BREADS)))
    dialog = _Cart(catalog=None)
    dialog._reload()
    assert dialog._busy and not dialog.enabled("_refresh")
    dialog.run()
    assert dialog._products.items == ["Bread, R$ 7.50, Fresh", "Cake, R$ 45.00, out of stock"]
    assert dialog._items.items == ["2 × Bread, R$ 7.50 each"]
    assert dialog.said() == "Products: 2. 2 items, estimated total R$ 15.00"
    assert dialog.enabled("_add") and dialog.enabled("_send") and dialog.enabled("_clear")
    assert dialog._products.Freeze.call_count == dialog._products.Thaw.call_count


def test_a_business_that_takes_no_carts_shows_its_catalog_and_no_cart_at_all():
    dialog = _Cart(Catalog(True, False, (BREAD,), EMPTY))
    assert dialog.said().endswith("the catalog can only be looked at.")
    for name in ("_add", "_change", "_remove", "_clear", "_send"):
        assert dialog.enabled(name) is False
    assert dialog._note.Enable.call_args.args[0] is False
    # The handlers hold the rule too, not only the greyed-out buttons.
    dialog._show_cart(TWO_BREADS)
    for handler in (dialog._on_add, dialog._on_change, dialog._on_remove, dialog._on_clear, dialog._on_send):
        handler(None)
    assert not dialog.work and not dialog._ask_quantity.called and not dialog._confirm.called
    none = _Cart(Catalog(False, False, (), EMPTY))
    assert none.said() == "This business has no catalog."
    failed = _Cart(catalog=None)
    failed._finish_reload(None)
    assert failed.said().startswith("WhatsApp could not be asked")


def test_a_product_out_of_stock_cannot_be_added():
    dialog = _Cart()
    dialog._products.SetSelection(1)
    dialog._update_buttons()
    assert dialog.enabled("_add") is False
    dialog._on_add(None)
    dialog._ask_quantity.assert_not_called()


def test_adding_asks_how_many_and_shows_the_cart_the_server_answers(monkeypatch):
    setting = Mock(return_value=TWO_BREADS)
    monkeypatch.setattr(commerce, "set_cart_quantity", setting)
    dialog = _Cart()
    dialog._on_add(None)
    assert dialog._ask_quantity.call_args.args == ("Bread", 1)
    dialog.run()
    setting.assert_called_once_with(CONNECTION, SELLER, "100", 2)
    assert dialog._cart == TWO_BREADS and dialog.said() == "2 items, estimated total R$ 15.00"
    # Adding again proposes one more than the cart holds; changing proposes what it holds.
    dialog._on_add(None)
    assert dialog._ask_quantity.call_args.args == ("Bread", 3)
    dialog.work.clear()
    dialog._busy = False
    dialog._on_change(None)
    assert dialog._ask_quantity.call_args.args == ("Bread", 2)
    dialog.work.clear()
    dialog._busy = False
    dialog._ask_quantity.return_value = None  # cancelled
    dialog._on_change(None)
    assert not dialog.work


def test_a_refused_change_still_shows_the_cart_as_it_is(monkeypatch):
    monkeypatch.setattr(commerce, "set_cart_quantity", Mock(side_effect=CommerceError("product_unavailable", TWO_BREADS)))
    dialog = _Cart()
    dialog._on_add(None)
    dialog.run()
    assert dialog._cart == TWO_BREADS and dialog.said() == "This product is no longer in the catalog."
    monkeypatch.setattr(commerce, "remove_from_cart", Mock(side_effect=CommerceError()))
    dialog._on_remove(None)
    dialog.run()
    assert dialog._cart == TWO_BREADS and dialog.said().startswith("WhatsApp could not be asked")


def test_removing_and_emptying(monkeypatch):
    removing = Mock(return_value=EMPTY)
    clearing = Mock(return_value=EMPTY)
    monkeypatch.setattr(commerce, "remove_from_cart", removing)
    monkeypatch.setattr(commerce, "clear_cart", clearing)
    dialog = _Cart(Catalog(True, True, (BREAD,), TWO_BREADS))
    dialog._confirm.return_value = False
    dialog._on_clear(None)
    assert not dialog.work
    dialog._confirm.return_value = True
    dialog._on_clear(None)
    dialog.run()
    clearing.assert_called_once_with(CONNECTION, SELLER)
    assert dialog.said() == "The cart is empty." and not dialog.enabled("_send")
    dialog._show_cart(TWO_BREADS)
    dialog._on_remove(None)
    dialog.run()
    removing.assert_called_once_with(CONNECTION, SELLER, "100")


def test_an_order_is_sent_once_after_a_question_that_says_what_and_to_whom(monkeypatch):
    sending = Mock(return_value=EMPTY)
    monkeypatch.setattr(commerce, "submit_cart", sending)
    dialog = _Cart(Catalog(True, True, (BREAD,), TWO_BREADS))
    dialog._confirm.return_value = False
    dialog._on_send(None)
    assert not dialog.work and not dialog._sent
    dialog._confirm.return_value = True
    dialog._on_send(None)
    assert dialog._confirm.call_args.args[0] == "Send this order to Bakery? 2 items, estimated total R$ 15.00"
    dialog._on_send(None)  # a second press while the first is on its way
    assert len(dialog.work) == 1
    dialog.run()
    sending.assert_called_once_with(CONNECTION, SELLER, "thanks")
    assert dialog.said() == "Order sent." and dialog.EndModal.called


def test_an_unconfirmed_send_is_never_offered_again_by_itself(monkeypatch):
    monkeypatch.setattr(commerce, "submit_cart", Mock(side_effect=CommerceError("unconfirmed")))
    monkeypatch.setattr(commerce, "read_catalog", Mock(return_value=Catalog(True, True, (BREAD,), TWO_BREADS)))
    dialog = _Cart(Catalog(True, True, (BREAD,), TWO_BREADS))
    dialog._on_send(None)
    dialog.run()
    dialog.run()  # the cart is read back once, and still holds the items
    assert dialog.said().startswith("The order may or may not have been sent")
    assert dialog._sent and not dialog.enabled("_send") and not dialog.EndModal.called
    dialog._reload()  # a manual refresh must not reopen the send either
    dialog.run()
    assert dialog._sent and not dialog.enabled("_send")
    dialog._on_send(None)
    assert not dialog.work


def test_an_unconfirmed_send_is_confirmed_when_the_cart_reads_back_empty(monkeypatch):
    monkeypatch.setattr(commerce, "submit_cart", Mock(side_effect=CommerceError("unconfirmed")))
    monkeypatch.setattr(commerce, "read_catalog", Mock(return_value=Catalog(True, True, (BREAD,), EMPTY)))
    dialog = _Cart(Catalog(True, True, (BREAD,), TWO_BREADS))
    dialog._on_send(None)
    dialog.run()
    dialog.run()
    assert dialog.said() == "Order sent." and dialog.EndModal.called


def test_a_refused_send_can_be_tried_again(monkeypatch):
    # A definite refusal sent nothing, so trying again is fine.
    monkeypatch.setattr(commerce, "submit_cart", Mock(side_effect=CommerceError("cart_disabled", TWO_BREADS)))
    again = _Cart(Catalog(True, True, (BREAD,), TWO_BREADS))
    again._on_send(None)
    again.run()
    assert not again._sent and again.said().startswith("This business does not take orders by cart")


class _Order(_Window):
    for _name in ("_set_busy", "_update_buttons", "describe", "_show", "_read", "_reload", "_finish_reload",
                  "_answer", "_finish_answer", "_on_accept", "_on_decline", "_on_update", "_announce"):
        locals()[_name] = getattr(OrderDialog, _name)
    del _name

    def __init__(self, order=None):
        self._base()
        self._message, self._order = ORDER_ID, None
        self._items = _ListBox()
        text = {"value": ""}
        self._details = SimpleNamespace(ChangeValue=lambda value: text.update(value=value),
                                        GetValue=lambda: text["value"])
        for name in ("_accept", "_decline", "_update", "_refresh"):
            setattr(self, name, Mock(IsShown=Mock(return_value=False)))
        self.Layout = Mock()
        self._ask_note = Mock(return_value="")
        self._choose = Mock(return_value=None)
        if order is not None:
            self._show(order)

    def shown(self, name):
        return getattr(self, name).Show.call_args.args[0]


def test_an_order_is_described_and_read_out(monkeypatch):
    monkeypatch.setattr(commerce, "read_order", Mock(return_value=RECEIVED))
    dialog = _Order()
    dialog._reload()
    dialog.run()
    assert dialog._items.items == ["2 × Bread, R$ 7.50 each"]
    assert dialog._details.GetValue().split("\n") == [
        "Order received", "State: waiting for the business's answer", "2 items", "total R$ 15.00",
        "Message: please"]
    assert dialog.said().startswith("Order received. State: waiting")
    taxed = Order(True, ACCEPTED, "", RECEIVED.items, 15000, 16500, "BRL", False, False)
    assert dialog.describe(taxed).split("\n") == ["Cart sent", "State: accepted", "2 items",
                                                    "subtotal R$ 15.00", "total R$ 16.50"]


def test_the_sellers_buttons_exist_only_for_who_the_server_says_may_use_them():
    buyer = _Order(Order(True, INQUIRY, "", RECEIVED.items, 15000, 15000, "BRL", False, False))
    assert not buyer.shown("_accept") and not buyer.shown("_decline") and not buyer.shown("_update")
    seller = _Order(RECEIVED)
    assert seller.shown("_accept") and seller.shown("_decline") and not seller.shown("_update")
    later = _Order(Order(False, ACCEPTED, "", RECEIVED.items, 15000, 15000, "BRL", False, True))
    assert not later.shown("_accept") and later.shown("_update")
    for handler in (buyer._on_accept, buyer._on_decline, buyer._on_update, seller._on_update, later._on_accept):
        handler(None)
    assert not buyer.work and not seller.work and not later.work


def test_a_refused_reading_says_why(monkeypatch):
    dialog = _Order()
    for code, start in (("not_an_order", "This order could not be read"), ("unavailable", "WhatsApp could not be asked")):
        monkeypatch.setattr(commerce, "read_order", Mock(side_effect=CommerceError(code)))
        dialog._reload()
        dialog.run()
        assert dialog.said().startswith(start) and dialog._order is None


def test_accepting_is_announced_only_when_the_order_read_again_shows_it(monkeypatch):
    accepting = Mock(return_value=ACCEPTED)
    monkeypatch.setattr(commerce, "accept_order", accepting)
    accepted = Order(False, ACCEPTED, "please", RECEIVED.items, 15000, 15000, "BRL", False, True)
    monkeypatch.setattr(commerce, "read_order", Mock(return_value=accepted))
    dialog = _Order(RECEIVED)
    dialog._on_accept(None)
    assert dialog._busy
    dialog.run()
    accepting.assert_called_once_with(CONNECTION, ORDER_ID)
    assert dialog.said() == "Order accepted." and dialog._order == accepted and dialog.shown("_update")
    # WhatsApp took the answer, and the order still reads as waiting: not announced as accepted.
    monkeypatch.setattr(commerce, "read_order", Mock(return_value=RECEIVED))
    still = _Order(RECEIVED)
    still._on_accept(None)
    still.run()
    assert still.said().startswith("The answer was sent, but the order does not show it yet")


def test_declining_asks_for_an_optional_note_and_cancelling_it_sends_nothing(monkeypatch):
    declining = Mock(return_value=DECLINED)
    monkeypatch.setattr(commerce, "decline_order", declining)
    declined = Order(False, DECLINED, "", RECEIVED.items, 15000, 15000, "BRL", False, True)
    monkeypatch.setattr(commerce, "read_order", Mock(return_value=declined))
    dialog = _Order(RECEIVED)
    dialog._ask_note.return_value = None
    dialog._on_decline(None)
    assert not dialog.work
    dialog._ask_note.return_value = "Sold out"
    dialog._on_decline(None)
    dialog.run()
    declining.assert_called_once_with(CONNECTION, ORDER_ID, "Sold out")
    assert dialog.said() == "Order declined."


@pytest.mark.parametrize("code,start", [
    ("not_business", "Only a WhatsApp Business account"), ("unavailable", "The answer could not be sent"),
    ("unconfirmed", "The answer could not be sent")])
def test_a_refused_answer_is_never_announced_as_given(monkeypatch, code, start):
    monkeypatch.setattr(commerce, "accept_order", Mock(side_effect=CommerceError(code)))
    monkeypatch.setattr(commerce, "read_order", Mock(return_value=RECEIVED))
    dialog = _Order(RECEIVED)
    dialog._on_accept(None)
    dialog.run()
    assert dialog.said().startswith(start)


def test_a_status_update_offers_whatsapps_own_statuses_and_claims_only_that_it_was_sent(monkeypatch):
    updating = Mock(return_value=ACCEPTED)
    monkeypatch.setattr(commerce, "update_order", updating)
    accepted = Order(False, ACCEPTED, "", RECEIVED.items, 15000, 15000, "BRL", False, True)
    monkeypatch.setattr(commerce, "read_order", Mock(return_value=accepted))
    dialog = _Order(accepted)
    dialog._on_update(None)
    assert not dialog.work  # no status chosen
    labels = dialog._choose.call_args.args[1]
    assert len(labels) == len(commerce.ORDER_STATUSES) and labels[6] == "Shipped"
    dialog._choose.return_value = 6
    dialog._ask_note.return_value = "On its way"
    dialog._on_update(None)
    dialog.run()
    updating.assert_called_once_with(CONNECTION, ORDER_ID, "shipped", "On its way")
    assert dialog.said() == "WhatsApp accepted the status update for sending."


class _Create(_Window):
    for _name in ("_update_buttons", "_total", "_refill", "_on_add", "_on_remove", "_on_send", "_finish_send",
                  "_announce"):
        locals()[_name] = getattr(OrderCreateDialog, _name)
    del _name

    def __init__(self):
        self._base()
        self._chat, self._customer, self._new_items, self._attempted = SELLER, "Maria", [], False
        self._list = _ListBox()
        self.buttons("_add", "_remove", "_send")
        self._ask_item = Mock(return_value=("Bread", 7500, 2))


def test_a_new_order_is_built_item_by_item_and_says_its_total():
    dialog = _Create()
    dialog._update_buttons()
    assert not dialog.enabled("_send") and not dialog.enabled("_remove")
    dialog._on_add(None)
    dialog._ask_item.return_value = ("Cake", 45000, 1)
    dialog._on_add(None)
    assert dialog._list.items == ["2 × Bread, 7.50 each", "1 × Cake, 45.00 each"]
    assert dialog.said() == "Items: 2, total 60.00." and dialog.enabled("_send")
    dialog._on_remove(None)  # the last one added is selected
    assert dialog._list.items == ["2 × Bread, 7.50 each"]
    dialog._on_remove(None)
    assert dialog.said() == "The order has no items yet." and not dialog.enabled("_send")
    dialog._ask_item.return_value = None
    dialog._on_add(None)
    assert dialog._new_items == []


def test_a_new_order_is_sent_once(monkeypatch):
    sending = Mock()
    monkeypatch.setattr(commerce, "send_order", sending)
    dialog = _Create()
    dialog._on_add(None)
    dialog._confirm.return_value = False
    dialog._on_send(None)
    assert not dialog.work
    dialog._confirm.return_value = True
    dialog._on_send(None)
    assert dialog._confirm.call_args.args[0] == "Send Maria the order? Items: 1. Total: 15.00."
    dialog._on_send(None)
    assert len(dialog.work) == 1
    dialog.run()
    sending.assert_called_once_with(CONNECTION, SELLER, [("Bread", 7500, 2)])
    assert dialog.said() == "Order sent." and dialog.EndModal.called


def test_an_order_whose_sending_is_not_confirmed_is_not_sent_again(monkeypatch):
    monkeypatch.setattr(commerce, "send_order", Mock(side_effect=CommerceError("refused")))
    dialog = _Create()
    dialog._on_add(None)
    dialog._on_send(None)
    dialog.run()
    assert dialog.said().startswith("The order may or may not have been sent")
    assert not dialog.enabled("_send") and not dialog.enabled("_add") and not dialog.EndModal.called
    dialog._on_send(None)
    assert not dialog.work


# ── every locale ───────────────────────────────────────────────────────────

WINDOWS = [
    ("commerce_products_list", "commerce_cart_list", "commerce_cart_note", "commerce_cart_add",
     "commerce_cart_change", "commerce_cart_remove", "commerce_cart_clear", "commerce_cart_send",
     "commerce_refresh", "close"),
    ("commerce_order_items", "commerce_order_details", "commerce_order_accept", "commerce_order_decline",
     "commerce_order_update_status", "commerce_refresh", "close"),
    ("commerce_new_order_items", "commerce_new_order_add", "commerce_new_order_remove",
     "commerce_new_order_send", "close"),
    ("commerce_item_name", "commerce_item_price", "commerce_item_quantity", "commerce_item_add", "cancel"),
]


@pytest.mark.parametrize("locale", registered_locale_codes())
@pytest.mark.parametrize("keys", WINDOWS)
def test_every_control_has_a_letter_of_its_own(locale, keys):
    strings = load_strings(locale)
    letters = {}
    for key in keys:
        assert mnemonic(strings[key]) is not None, key
        letters.setdefault(mnemonic(strings[key]), []).append(key)
    assert not {letter: found for letter, found in letters.items() if len(found) > 1}


@pytest.mark.parametrize("locale", registered_locale_codes())
def test_every_status_whatsapp_can_send_has_a_name(locale):
    """These keys are built at run time, which the static key check cannot see."""
    strings = load_strings(locale)
    for status in commerce.ORDER_STATUSES:
        assert strings[f"commerce_status_{status}"].strip()
    for status in commerce.PAYMENT_STATUSES:
        assert strings[f"commerce_payment_{status}"].strip()


def test_a_lost_answer_after_the_cart_setting_changed_is_not_a_failure(window, monkeypatch):
    """Sent once and believed by reading back: the request timing out after
    WhatsApp took the change still says it was done, and keeps the reading."""
    setting = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(commerce, "set_cart_enabled", setting)
    monkeypatch.setattr(commerce, "read_account", Mock(return_value=Account(True, True, frozenset())))
    window._commerce_account = Account(True, False, frozenset())
    window.toggle_cart_enabled()
    setting.assert_called_once_with(CONNECTION, True)
    assert window.speak_output.output.call_args.args[0] == "Your catalog now accepts carts."
    assert window._commerce_account.cart_enabled is True
