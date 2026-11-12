"""Media retry (media_retry.py): the expired-link path of get-media, against a
fake neonize client. Without WhatsApp and without the DLL's retry calls."""
import asyncio
import base64
import threading
from types import SimpleNamespace as N

import pytest
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ImageMessage, Message

import media_retry

KEY = b"k" * 32
CHAT = "5511999990000@c.us"


def image(path="/old", url="https://old"):
    return Message(imageMessage=ImageMessage(
        mediaKey=KEY, directPath=path, URL=url, fileLength=10))


class Event:
    """A MediaRetryEv: what the code reads, with HasField like a protobuf."""

    def __init__(self, msg_id="M1", error_code=None):
        self.MessageID, self.Ciphertext, self.IV = msg_id, b"cc", b"iv"
        self.ErrorCode = error_code or 0
        self._error = error_code is not None

    def HasField(self, name):
        assert name == "ErrorCode"
        return self._error


def test_parse_serialized_reads_from_me_chat_id_and_author():
    assert media_retry.parse_serialized(f"false_{CHAT}_M1") == (False, CHAT, "M1", "")
    assert media_retry.parse_serialized("true_1@g.us_M2_9@lid") == (True, "1@g.us", "M2", "9@lid")
    assert media_retry.parse_serialized("garbage") is None
    assert media_retry.parse_serialized("maybe_1@g.us_M1") is None


def test_retry_request_uses_whatsmeow_jids_and_marks_groups():
    request = media_retry.retry_request((False, "1@g.us", "M1", "2@c.us"), KEY)
    assert request == {"chat": "1@g.us", "id": "M1", "from_me": False, "is_group": True,
                       "sender": "2@s.whatsapp.net",
                       "media_key": base64.b64encode(KEY).decode()}
    direct = media_retry.retry_request((True, CHAT, "M1", ""), KEY)
    assert direct["chat"] == "5511999990000@s.whatsapp.net"
    assert direct["is_group"] is False and direct["sender"] == ""


def test_decrypt_request_carries_the_error_code_only_when_there_is_one():
    ok = media_retry.decrypt_request(Event(), KEY)
    assert ok["has_error"] is False and ok["error_code"] == 0
    assert base64.b64decode(ok["ciphertext"]) == b"cc"
    failed = media_retry.decrypt_request(Event(error_code=2), KEY)
    assert failed["has_error"] is True and failed["error_code"] == 2


def test_with_new_path_replaces_the_path_and_clears_the_url_without_touching_the_original():
    original = image()
    fresh = media_retry.with_new_path(original, "/new")
    assert fresh.imageMessage.directPath == "/new" and fresh.imageMessage.URL == ""
    assert fresh.imageMessage.mediaKey == KEY
    assert original.imageMessage.directPath == "/old" and original.imageMessage.URL == "https://old"


def test_media_of_needs_a_media_key():
    assert media_retry.media_of(image()) is not None
    assert media_retry.media_of(Message(imageMessage=ImageMessage())) is None
    assert media_retry.media_of(Message(conversation="hi")) is None


def test_only_an_expired_link_error_is_retried():
    for text in ("404 not found", "HTTP 410", "403 Forbidden"):
        assert media_retry.is_expired_error(RuntimeError(text))
    assert not media_retry.is_expired_error(RuntimeError("connection reset"))


class RetryClient:
    """send_media_retry delivers the answer through `answers` like the event."""

    def __init__(self, answers, result, event=None, send=None):
        self.answers, self.result, self.event = answers, result, event or Event()
        self.send, self.sent, self.decrypted = send or {"ok": True}, [], []

    def send_media_retry(self, request):
        self.sent.append(request)
        if self.event is not None:
            threading.Timer(0.01, self.answers.deliver, (request["id"], self.event)).start()
        return self.send

    def decrypt_media_retry(self, request):
        self.decrypted.append(request)
        return self.result


def run(session, message, download, msgid=f"false_{CHAT}_M1"):
    return asyncio.run(media_retry.download_async(session, message, msgid, download))


def downloads(outcomes):
    seen = []

    async def download(message):
        seen.append(message.imageMessage.directPath)
        outcome = outcomes[min(len(seen), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    download.seen = seen
    return download


def session_with(result, **kw):
    answers = media_retry.Answers()
    client = RetryClient(answers, result, **kw)
    return N(client=client, media_retries=answers), client


def test_an_expired_link_asks_the_phone_and_downloads_from_the_new_path():
    s, client = session_with({"result": "SUCCESS", "direct_path": "/new"})
    download = downloads([RuntimeError("403 forbidden"), b"data"])
    assert run(s, image(), download) == b"data"
    assert download.seen == ["/old", "/new"]
    assert client.sent[0]["id"] == "M1" and client.sent[0]["from_me"] is False
    assert client.decrypted[0]["message_id"] == "M1"


def test_a_phone_without_the_media_raises_a_404_so_the_route_answers_410():
    import transfers
    s, _ = session_with({"result": "NOT_FOUND"})
    with pytest.raises(media_retry.MediaGone) as raised:
        run(s, image(), downloads([RuntimeError("403 forbidden")]))
    assert transfers.status_for_download_error(raised.value) == 410


def test_a_phone_that_does_not_answer_is_said_and_not_taken_for_expired(monkeypatch):
    monkeypatch.setattr(media_retry, "TIMEOUT", 0.05)
    s, client = session_with({"result": "SUCCESS", "direct_path": "/new"})
    client.event = None                      # the phone never answers
    original = RuntimeError("410 gone")
    with pytest.raises(media_retry.PhoneDidNotAnswer) as raised:
        run(s, image(), downloads([original]))
    assert raised.value.__context__ is original
    assert media_retry.failure_code(raised.value) == "media_retry_no_answer"


def test_a_failed_retry_keeps_the_original_error():
    for result in ({"result": "DECRYPTION_ERROR"}, {"error": "bad key"},
                   {"result": "SUCCESS", "direct_path": ""}):
        s, _ = session_with(result)
        original = RuntimeError("404")
        with pytest.raises(RuntimeError) as raised:
            run(s, image(), downloads([original]))
        assert raised.value is original


def test_a_real_fault_is_not_retried():
    s, client = session_with({"result": "SUCCESS", "direct_path": "/new"})
    with pytest.raises(RuntimeError):
        run(s, image(), downloads([RuntimeError("connection reset")]))
    assert client.sent == []


def test_without_the_builds_calls_nothing_is_retried():
    s = N(client=N(), media_retries=media_retry.Answers())
    with pytest.raises(RuntimeError):
        run(s, image(), downloads([RuntimeError("404")]))


def test_a_message_without_a_key_or_an_unparsable_id_is_not_retried():
    s, client = session_with({"result": "SUCCESS", "direct_path": "/new"})
    with pytest.raises(RuntimeError):
        run(s, Message(imageMessage=ImageMessage()), downloads([RuntimeError("404")]))
    with pytest.raises(RuntimeError):
        run(s, image(), downloads([RuntimeError("404")]), msgid="nonsense")
    assert client.sent == []


def test_answers_deliver_only_to_who_is_waiting():
    answers = media_retry.Answers()
    answers.deliver("nobody", Event())          # nobody waits: no error
    slot = answers.expect("M1")
    answers.deliver("M2", Event())
    assert not slot["event"].is_set()
    answers.deliver("M1", "answer")
    assert slot["event"].is_set() and slot["answer"] == "answer"
    answers.forget("M1")
    answers.deliver("M1", "late")
    assert slot["answer"] == "answer"


def test_register_events_hands_the_phone_answer_to_the_waiting_download():
    import neonize.events as nev
    handlers = {}

    class Cli:
        def event(self, kind):
            def add(func):
                handlers[kind] = func
                return func
            return add
    s = N()
    media_retry.register_events(Cli(), s)
    if not hasattr(nev, "MediaRetryEv"):
        pytest.skip("neonize without WinZapp's media retry event")
    slot = s.media_retries.expect("M1")
    handlers[nev.MediaRetryEv](None, Event("M1"))
    assert slot["event"].is_set()


# ---- the download after the retry: authenticated by HMAC, not the old hash (#756)

def keyed_image():
    return Message(imageMessage=ImageMessage(
        mediaKey=KEY, directPath="/old", URL="https://old", fileLength=10,
        fileEncSHA256=b"e" * 32, fileSHA256=b"f" * 32))


class RetriedClient(RetryClient):
    """A build with `download_retried_media`; writes the plaintext to `dest`."""

    def __init__(self, answers, answer, plain=b"voice"):
        super().__init__(answers, {"result": "SUCCESS", "direct_path": "/new"})
        self.answer, self.plain, self.requests = answer, plain, []

    def download_retried_media(self, request):
        self.requests.append(request)
        if not self.answer.get("error"):
            with open(request["dest"], "wb") as f:
                f.write(self.plain)
        return self.answer


def retried_session(answer):
    answers = media_retry.Answers()
    client = RetriedClient(answers, answer)
    return N(client=client, media_retries=answers), client


def test_retried_request_carries_the_keys_and_the_kind_of_the_media():
    request = media_retry.retried_request(keyed_image(), "/new", "C:/tmp/x")
    assert request["direct_path"] == "/new" and request["kind"] == "image"
    assert base64.b64decode(request["media_key"]) == KEY
    assert base64.b64decode(request["enc_sha256"]) == b"e" * 32
    assert base64.b64decode(request["file_sha256"]) == b"f" * 32
    assert request["dest"] == "C:/tmp/x"


def test_kind_of_names_each_media_message():
    assert media_retry.kind_of(keyed_image()) == "image"
    assert media_retry.kind_of(Message(conversation="hi")) is None
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import AudioMessage
    assert media_retry.kind_of(Message(audioMessage=AudioMessage(mediaKey=KEY))) == "audio"


def test_after_the_retry_a_build_with_the_call_downloads_through_it_not_through_the_old_hash():
    s, client = retried_session({"ok": True})
    download = downloads([RuntimeError("403 forbidden")])
    assert run(s, keyed_image(), download) == b"voice"
    assert download.seen == ["/old"]                 # the old path was tried once, no second go
    assert client.requests[0]["direct_path"] == "/new"


def test_a_file_the_key_does_not_authenticate_is_reported_with_its_own_text():
    import transfers
    s, _ = retried_session({"error": "invalid media hmac"})
    with pytest.raises(RuntimeError) as raised:
        run(s, keyed_image(), downloads([RuntimeError("403 forbidden")]))
    assert "invalid media hmac" in str(raised.value)
    assert transfers.status_for_download_error(raised.value) == 410
    assert media_retry.failure_code(raised.value) == "media_retry_unreadable"


# ---- issue #766: WhatsApp Web's mms-type, and what each host served

def test_the_mms_type_is_whatsapp_webs_own():
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import AudioMessage, VideoMessage
    voice = Message(audioMessage=AudioMessage(mediaKey=KEY, PTT=True))
    song = Message(audioMessage=AudioMessage(mediaKey=KEY))
    gif = Message(videoMessage=VideoMessage(mediaKey=KEY, gifPlayback=True))
    note = Message(ptvMessage=VideoMessage(mediaKey=KEY))
    assert [media_retry.mms_type_of(m) for m in (voice, song, gif, note, keyed_image())] == [
        "ptt", "audio", "gif", "ptv", "image"]
    request = media_retry.retried_request(voice, "/new", "x")
    assert (request["kind"], request["mms_type"]) == ("audio", "ptt")
    assert media_retry.retried_request(keyed_image(), "/new", "x")["file_length"] == 10


def test_what_each_host_served_is_logged_without_keys(caplog):
    tried = [{"host": "mmg.whatsapp.net", "mms_type": "ptt", "error": "invalid media hmac",
              "size": 12, "file_length": 4096, "old_enc_sha256": False,
              "looks_like_text": False, "head": "00ff"},
             {"host": "media-x.cdn", "mms_type": "audio", "error": "download failed with 403"}]
    s, _ = retried_session({"error": "invalid media hmac", "tried": tried})
    import logging
    with caplog.at_level(logging.INFO, logger="bridge"), pytest.raises(RuntimeError):
        run(s, keyed_image(), downloads([RuntimeError("403 forbidden")]))
    text = caplog.text
    assert "size=12 expected=4096 old_hash=False" in text and "media-x.cdn/audio" in text
    assert base64.b64encode(KEY).decode() not in text


def test_a_busy_phone_gets_longer_but_the_client_does_not_give_up_first():
    assert 25 < media_retry.TIMEOUT <= 45          # the client reads for 60 s


def test_only_the_retrys_own_failures_have_a_code():
    assert media_retry.failure_code(media_retry.MediaGone("404")) == "media_gone_from_phone"
    assert media_retry.failure_code(RuntimeError("403 forbidden")) == ""
