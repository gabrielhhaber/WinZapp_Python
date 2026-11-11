"""Headless business-template contract tests: no app, network or native imports.

The actual mapper methods are loaded through AST onto plain stubs. Protobuf
fixtures model explicit HasField presence; this is not a protobuf/DLL E2E test.
"""
import ast
import copy
import json
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def no_api(*args, **kwargs):
    raise AssertionError("No live requests are permitted")


def setUpModule():
    global actions, business_message_text, message_business_buttons, prepare_business_message, bridge, isolation, paths
    paths = list(sys.path)
    isolation = patch.dict(sys.modules)
    isolation.start()
    for name in list(sys.modules):
        if name == "core" or name.startswith("core.") or name in ("buttons", "commerce_messages", "template_messages"):
            sys.modules.pop(name)
    sys.path[:0] = [str(ROOT / "client"), str(ROOT / "neonize_bridge")]
    api = types.ModuleType("core.api_client")
    api.api_post = no_api
    sys.modules["core.api_client"] = api
    from core import business_buttons as actions
    from core.business_message_content import (
        business_message_text, message_business_buttons, prepare_business_message)
    import buttons as bridge


def tearDownModule():
    isolation.stop()
    sys.path[:] = paths


class Proto:
    def __init__(self, **values):
        self.__dict__.update(values)
    def HasField(self, field):
        return field in self.__dict__


def hydrated(text="", **kwargs):
    return Proto(hydratedTitleText=kwargs.pop("title", ""),
                 hydratedContentText=text, hydratedFooterText=kwargs.pop("footer", ""),
                 hydratedButtons=kwargs.pop("buttons", []), **kwargs)


def interactive(text="", buttons=None):
    return Proto(header=Proto(title="Header"), body=Proto(text=text),
                 footer=Proto(text="Footer"), carouselMessage=Proto(cards=[]),
                 nativeFlowMessage=Proto(buttons=buttons or []))


def method(path, name, namespace=None):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == name)
    fn = copy.deepcopy(fn)
    fn.decorator_list = []
    scope = dict(namespace or {})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 str(path), "exec"), scope)
    return scope[name]


class I18n:
    def t(self, key):
        return {"options": "Options", "business_template_unavailable": "Unavailable on this device",
                "business_parts_unavailable": "Some parts are unsupported"}.get(key, key)


def normalized(wpp):
    scope = dict(time=time, observe_payload=lambda *a, **k: None,
                 is_view_once_unavailable=lambda m: False,
                 channel_invites=types.SimpleNamespace(WPP_TYPES=()),
                 VIEW_ONCE_UNAVAILABLE_TYPE="view_once_unavailable",
                 CALL_LOG_MESSAGE_TYPE="callLogMessage", ORDER_MESSAGE_TYPE="orderMessage",
                 ORDER_UPDATE_MESSAGE_TYPE="orderUpdateMessage", PRODUCT_MESSAGE_TYPE="productMessage",
                 order_update_payload=lambda m: None, business_buttons=actions.business_buttons,
                 server_marks_edited=lambda m: False, remote_star_state=lambda m: {},
                 comment_count=lambda m: None, server_id_of=lambda m: None)
    fn = method("client/core/websocket_client.py", "_normalize_wpp_message", scope)
    owner = types.SimpleNamespace(_clean_jid=lambda jid: jid.replace("@c.us", "@s.whatsapp.net"))
    payload = {"id": "false_10000000000@c.us_SYNTHETIC123", "type": "template",
               "timestamp": 1000000000, **wpp}
    return fn(owner, payload)


def canonical(content, kind="templateMessage", **fields):
    return dict(messageType=kind, message={kind: content}, **fields)


class TemplateCompatibility(unittest.TestCase):
    def test_order_text_and_https_button_pipeline(self):
        b = Proto(urlButton=Proto(displayText="My order", URL="https://shop.example/order/synthetic"))
        raw = bridge.button_fields(Proto(templateMessage=Proto(
            hydratedFourRowTemplate=hydrated("Order received 📦", title="Shop", footer="Thank you", buttons=[b]))))
        msg = normalized(raw)
        self.assertEqual(msg["message"]["templateMessage"]["hydratedContentText"], "Shop\nOrder received 📦\nThank you")
        self.assertEqual(message_business_buttons(msg)[0]["value"], "https://shop.example/order/synthetic")
        self.assertIn("My order", business_message_text(msg, I18n()))
        self.assertEqual(msg["key"], {"remoteJid": "10000000000@s.whatsapp.net", "fromMe": False, "id": "SYNTHETIC123"})

    def test_empty_preferred_does_not_shadow_populated_alternative(self):
        raw = bridge.button_fields(Proto(templateMessage=Proto(
            hydratedTemplate=hydrated(), hydratedFourRowTemplate=hydrated("Alternate"))))
        self.assertEqual(raw["body"], "Alternate")

    def test_both_hydrated_variants(self):
        for name in ("hydratedTemplate", "hydratedFourRowTemplate"):
            with self.subTest(name=name):
                self.assertEqual(bridge.button_fields(Proto(templateMessage=Proto(**{name: hydrated("Text")})))["body"], "Text")

    def test_hydrated_hsm_and_legacy_nested(self):
        inner = Proto(hydratedTemplate=hydrated("Explicit text"))
        for message in (Proto(highlyStructuredMessage=Proto(hydratedHsm=inner)),
                        Proto(templateMessage=Proto(fourRowTemplate=Proto(content=Proto(hydratedHsm=inner))))):
            with self.subTest(message=message):
                self.assertEqual(bridge.button_fields(message)["body"], "Explicit text")

    def test_unhydrated_hsm_never_invents_text(self):
        raw = bridge.button_fields(Proto(highlyStructuredMessage=Proto(params=["SECRET_ORDER"], elementName="order")))
        self.assertEqual(raw["body"], "")
        self.assertTrue(raw["businessTemplateIncomplete"])
        self.assertEqual(business_message_text(normalized(raw), I18n()), "Unavailable on this device")

    def test_nested_template_interactive(self):
        b = Proto(name="cta_url", buttonParamsJSON=json.dumps(dict(display_text="Open", url="https://shop.example")))
        raw = bridge.button_fields(Proto(templateMessage=Proto(interactiveMessageTemplate=interactive("Body", [b]))))
        self.assertEqual(raw["type"], "interactive")
        self.assertEqual(raw["body"], "Header\nBody\nFooter")
        self.assertEqual(message_business_buttons(normalized(raw))[0]["kind"], "url")

    def test_media_header_is_readable_and_explicitly_incomplete(self):
        for field in ("documentMessage", "imageMessage", "videoMessage", "locationMessage"):
            with self.subTest(field=field):
                body = hydrated("Body", **{field: Proto(caption="Readable attachment")})
                raw = bridge.button_fields(Proto(templateMessage=Proto(hydratedTemplate=body)))
                self.assertEqual(raw["businessHeaderKind"], field)
                self.assertIn("Readable attachment", raw["body"])
                self.assertIn("unsupported", business_message_text(normalized(raw), I18n()))

    def test_slot_indices_are_preserved_for_every_source(self):
        payloads = [
            ({"hydratedButtons": [None, {"quickReplyButton": {"displayText": "Reply", "id": "T"}}]}, "template"),
            ({"dynamicReplyButtons": [None, {"buttonText": {"displayText": "Reply"}, "buttonId": "D"}]}, "dynamic"),
            ({"interactivePayload": {"buttons": [None, {"name": "quick_reply", "buttonParamsJson": '{"display_text":"Reply","id":"N"}'}]}}, "native"),
            ({"list": {"sections": [{"rows": [None, {"title": "Reply", "rowId": "L"}]}]}}, "list"),
        ]
        for payload, source in payloads:
            with self.subTest(source=source):
                selected = actions.business_buttons(payload)
                self.assertEqual(len(selected), 1)
                self.assertEqual((selected[0]["index"], selected[0]["source"]), (1, source))
                self.assertIn(bridge.reply_plan(payload, source, 1)["kind"], ("template", "button", "flow", "list"))

    def test_links_copy_call_and_replies_stay_separate(self):
        p = {"hydratedButtons": [
            {"quickReplyButton": {"displayText": "Reply", "id": "r"}},
            {"urlButton": {"displayText": "Open", "url": "https://shop.example"}},
            {"callButton": {"displayText": "Number", "phoneNumber": "+10000000000"}}],
            "interactivePayload": {"buttons": [{"name": "cta_copy", "buttonParamsJson": '{"display_text":"Copy","copy_code":"SYNTHETIC"}'}]}}
        self.assertEqual({b["kind"] for b in actions.business_buttons(p)}, {"reply", "url", "call", "copy"})

    def test_web_urls_only(self):
        for value in ("javascript:alert(1)", "file:///x", "trendyol://order", "https://", "https://user:pass@shop.example",
                      "https://shop.example/\ncmd", " https://shop.example", "https://[bad"):
            with self.subTest(value=value):
                self.assertFalse(actions.safe_business_url(value))
        self.assertTrue(actions.safe_business_url("https://shop.example/order?x=1"))
        self.assertTrue(actions.safe_business_url("http://shop.example"))

    def test_invalid_native_payloads_and_limits(self):
        entries = [None, {}, {"name": "cta_url", "buttonParamsJson": "bad"},
                   {"name": "quick_reply", "buttonParamsJson": "[]"},
                   {"name": "quick_reply", "buttonParamsJson": '{"disabled":true,"display_text":"No"}'},
                   {"name": "quick_reply", "buttonParamsJson": "x" * 65537}]
        self.assertEqual(actions.business_buttons({"interactivePayload": {"buttons": entries}}), [])
        payload = {"hydratedButtons": [{"quickReplyButton": {"displayText": "R", "id": str(n)}} for n in range(100)]}
        self.assertEqual([b["index"] for b in actions.business_buttons(payload)], list(range(32)))

    def test_unsupported_flow_keeps_text_and_explains_missing_action(self):
        msg = normalized(dict(type="interactive", body="Known body", interactivePayload={
            "buttons": [{"name": "open_form", "buttonParamsJson": '{"display_text":"Form"}'}]}))
        text = business_message_text(msg, I18n())
        self.assertIn("Known body", text)
        self.assertIn("unsupported", text)
        self.assertEqual(message_business_buttons(msg), [])

    def test_old_cached_content_is_read_without_mutation(self):
        msg = canonical({"hydratedFourRowTemplate": {"hydratedContentText": "Stored body", "hydratedButtons": [
            {"urlButton": {"displayText": "Stored order", "url": "https://shop.example"}}]}})
        before = copy.deepcopy(msg)
        self.assertIn("Stored body", business_message_text(msg, I18n()))
        self.assertEqual(message_business_buttons(msg)[0]["label"], "Stored order")
        self.assertEqual(msg, before)

    def test_irrecoverably_empty_record_is_explained(self):
        self.assertEqual(business_message_text(canonical({"hydratedContentText": "", "hydratedButtons": []}), I18n()),
                         "Unavailable on this device")

    def test_long_text_and_labels_are_not_lost(self):
        body, label = "Body " * 500, "Button " * 100
        msg = normalized(dict(body=body, hydratedButtons=[{"urlButton": {"displayText": label, "url": "https://shop.example"}}]))
        self.assertIn(body.strip(), business_message_text(msg, I18n()))
        action = message_business_buttons(msg)[0]
        self.assertEqual(actions.business_button_label(msg, action), label.strip())
        self.assertGreater(len(business_message_text(msg, I18n())), 511)

    def test_cached_actions_revalidated(self):
        invalid = [dict(kind="url", source="template", index=0, label="bad", value="file:///x"),
                   dict(kind="reply", source="template", index=True, label="bad", value="R"),
                   dict(kind="reply", source="template", index=33, label="bad", value="R")]
        self.assertEqual(message_business_buttons(canonical({}, businessButtons=invalid)), [])

    def test_history_direction_and_identity_flags_survive(self):
        msg = normalized(dict(body="History", isMdHistoryMsg=True, participant="10000000001@lid", fromMe=True))
        self.assertTrue(msg["isMdHistoryMsg"])
        self.assertTrue(msg["key"]["fromMe"])
        self.assertEqual(msg["key"]["participant"], "10000000001@lid")

    def test_wpp_nested_variant_and_native_shape(self):
        raw = dict(type="template", templateMessage={"hydratedFourRowTemplate": {
            "hydratedTitleText": "Title", "hydratedContentText": "Nested", "hydratedFooterText": "Footer"}})
        self.assertEqual(prepare_business_message(raw)["body"], "Title\nNested\nFooter")
        native = prepare_business_message(dict(type="template", templateMessage={"interactiveMessageTemplate": {
            "body": {"text": "Native"}, "nativeFlowMessage": {"buttons": [
                {"name": "cta_url", "buttonParamsJSON": '{"display_text":"Open","url":"https://shop.example"}'}]}}}))
        self.assertEqual(native["type"], "interactive")
        self.assertEqual(actions.business_buttons(native)[0]["kind"], "url")

    def test_rendering_and_notification_use_same_full_text(self):
        msg = normalized(dict(body="Body", hydratedButtons=[{"urlButton": {"displayText": "Open", "url": "https://shop.example"}}]))
        owner = types.SimpleNamespace(main_window=types.SimpleNamespace(i18n=I18n()))
        render = method("client/ui/conversation_panel/message_rendering.py", "_get_message_content")
        notify = method("client/core/notification_manager.py", "format_notification_body")
        self.assertEqual(render(owner, msg), business_message_text(msg, I18n()))
        self.assertEqual(notify(msg, owner.main_window, I18n()), render(owner, msg))

    def test_reply_validation_refuses_invalid_indices_before_any_request(self):
        for index in (-1, 32, True, "1", None):
            with self.subTest(index=index):
                with self.assertRaises(actions.ButtonReplyError) as caught:
                    actions.reply_to_button(("unused", 0, "synthetic"), "false_10000000000@c.us_SYNTHETIC123",
                                            dict(kind="reply", source="template", index=index))
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_cached_carousel_label_survives_action_validation(self):
        full = "A long card description " * 8
        msg = canonical({"body": {"text": full}}, "interactiveMessage", businessButtons=[
            {"kind": "reply", "source": "native", "index": 0, "label": "1. " + full[:32] + ": Select", "value": "ID"}])
        action = message_business_buttons(msg)[0]
        self.assertEqual(actions.business_button_label(msg, action), full.strip())

    def test_list_requires_its_own_row_title(self):
        self.assertEqual(actions.business_buttons({"list": {"sections": [{"title": "Section", "rows": [
            {"rowId": "ID"}]}]}}), [])

    def test_template_quote_and_forwarded_context_survive(self):
        msg = normalized(dict(body="Quoted template", contextInfo={
            "stanzaId": "SYNTHETICQUOTE", "participant": "10000000001@lid", "isForwarded": True}))
        context = msg["message"]["templateMessage"]["contextInfo"]
        self.assertEqual(context["stanzaId"], "SYNTHETICQUOTE")
        self.assertEqual(context["participant"], "10000000001@lid")
        self.assertTrue(context["isForwarded"])

    def test_malformed_cached_long_label_does_not_crash(self):
        msg = canonical({}, businessButtons=[dict(kind="url", source="template", index=0,
            label="Open", value="https://shop.example", fullLabel=5)])
        self.assertEqual(actions.business_button_label(msg, message_business_buttons(msg)[0]), "Open")

    def test_one_inflight_reply_no_retry(self):
        jobs, calls, callbacks = [], [], []
        class Thread:
            def __init__(self, target, daemon):
                jobs.append(target)
            def start(self):
                pass
        def refused(*args):
            calls.append(args)
            raise actions.ButtonReplyError()
        send = method("client/ui/conversation_panel/business_buttons.py", "_send_business_reply", dict(
            threading=types.SimpleNamespace(Thread=Thread),
            wx=types.SimpleNamespace(CallAfter=lambda *args: callbacks.append(args)),
            connection_identity=lambda mw: mw.connection, reply_to_button=refused,
            ButtonReplyError=actions.ButtonReplyError))
        owner = types.SimpleNamespace(main_window=types.SimpleNamespace(connection=("A",),
            _serialize_msg_id=lambda *args: "SYNTHETIC_ID"), conversation={"remoteJid": "synthetic@lid"},
            _business_reply_done=lambda *args: None)
        send(owner, {}, {})
        send(owner, {}, {})
        self.assertEqual(len(jobs), 1)
        jobs[0]()
        self.assertEqual(len(calls), 1)
        self.assertFalse(callbacks[0][1])
        self.assertEqual(len(jobs), 1)

    def test_buttons_controls_use_full_labels_without_sending(self):
        labels, bindings = [], []
        class Widget:
            def __getattr__(self, name):
                return lambda *a, **k: None
        class Button(Widget):
            def __init__(self, parent, label):
                labels.append(label)
            def Bind(self, event, callback):
                bindings.append(callback)
        fake_wx = types.SimpleNamespace(Button=Button, WrapSizer=lambda *a: Widget(),
                                      HORIZONTAL=1, ALL=2, EVT_BUTTON=3)
        show = method("client/ui/conversation_panel/business_buttons.py", "_show_business_buttons", dict(
            wx=fake_wx, message_business_buttons=message_business_buttons,
            business_button_label=actions.business_button_label, connection_identity=lambda mw: ("A",)))
        owner = types.SimpleNamespace(conversation={}, main_window=object(), _buttons_container=Widget(),
            conversation_panel=Widget(), _business_sending=True,
            _on_business_button=lambda *a: self.fail("Displaying must not activate"))
        label = "A&B " * 120
        msg = normalized(dict(body="Body", hydratedButtons=[{"urlButton": {"displayText": label, "url": "https://shop.example"}}]))
        show(owner, msg)
        self.assertEqual(labels, [label.strip().replace("&", "&&")])
        self.assertEqual(len(bindings), 1)
        self.assertTrue(owner._business_sending)

    def test_stale_control_never_opens_link(self):
        activation = method("client/ui/conversation_panel/business_buttons.py", "_on_business_button",
                            dict(connection_identity=lambda mw: mw.connection, URL="url", COPY="copy", CALL="call", REPLY="reply"))
        mw = types.SimpleNamespace(connection=("A",), i18n=I18n())
        conversation = {}
        context = (conversation, mw.connection, object())
        calls = []
        owner = types.SimpleNamespace(main_window=mw, conversation=conversation,
                                      _business_context=context, _activate_link=calls.append)
        button = dict(kind="url", value="https://shop.example")
        activation(owner, button, {}, context)
        self.assertEqual(len(calls), 1)
        owner.conversation = {}
        activation(owner, button, {}, context)
        owner.conversation = conversation
        mw.connection = ("B",)
        activation(owner, button, {}, context)
        self.assertEqual(len(calls), 1)

    def test_pending_result_does_not_announce_in_another_chat(self):
        done = method("client/ui/conversation_panel/business_buttons.py", "_business_reply_done",
                      dict(connection_identity=lambda mw: mw.connection))
        spoken = []
        mw = types.SimpleNamespace(connection=("A",), i18n=I18n(), output=lambda *a, **k: spoken.append(a))
        context = ({}, mw.connection, object())
        pending = object()
        owner = types.SimpleNamespace(main_window=mw, conversation={}, _business_context=context,
                                      _business_sending=True, _business_pending=pending)
        done(owner, True, pending, mw.connection, context)
        self.assertFalse(owner._business_sending)
        self.assertEqual(spoken, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
