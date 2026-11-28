"""Streamed (server-sent events) AI answers: parser, per-provider accumulation,
limits, pacing and the service wiring. Synthetic events only: nothing reaches
the network, sleeps or opens a window."""
import json

import pytest

from core.ai_media import config, service
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import Media
from core.ai_media.providers import build_request, parse_answer
from core.ai_media.streaming import (
    PartialPacer, SSEParser, StreamAnswer, read_stream, settled_prefix, streaming_request,
    supports_streaming)
from tests.test_ai_media_core import FakeHTTP

IMAGE = Media("image", b"synthetic", "image/jpeg", "image.jpg")
TEXT = Media("text", b"hello", "text/plain", "text.txt")


def sse(*events):
    """Wire bytes of events: dicts become JSON data lines, strings are used as is."""
    out = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event)
        out.append(f"data: {data}\n\n")
    return "".join(out).encode()


class StreamResponse:
    def __init__(self, body, size=7, status=200, content_type="text/event-stream", headers=None):
        self.status_code = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.raw = self
        self.closed = False
        self._chunks = [body[i:i + size] for i in range(0, len(body), size)]
        self.reads = 0

    def read1(self, amount, decode_content=True):
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""

    def shutdown(self):
        self.closed = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def stream(provider, body, kind="image", size=7, on_partial=None, check=lambda: None):
    return read_stream(StreamResponse(body, size), provider, kind, check, on_partial)


# --- the SSE line format ---------------------------------------------------

def test_parser_joins_data_lines_and_ignores_comments_and_other_fields():
    parser = SSEParser()
    wire = b": keep-alive\n\nevent: x\nid: 3\nretry: 10\ndata: one\ndata: two\n\ndata:three\n\n"
    assert parser.feed(wire) == ["one\ntwo", "three"]


def test_parser_handles_events_split_anywhere_and_every_line_ending():
    wire = "data: a\r\n\r\ndata: éà\r\rdata: c\n\n".encode()
    for size in range(1, len(wire) + 1):
        parser = SSEParser()
        events = []
        for i in range(0, len(wire), size):
            events += parser.feed(wire[i:i + size])
        assert events == ["a", "éà", "c"], size


def test_parser_drops_an_event_the_stream_ends_in_the_middle_of():
    parser = SSEParser()
    assert parser.feed(b"data: complete\n\ndata: half") == ["complete"]


def test_parser_refuses_an_endless_line():
    parser = SSEParser()
    with pytest.raises(DescriptionError, match="ai_error_response"):
        parser.feed(b"data: " + b"x" * (2 * 1024 * 1024))


# --- one fixture per provider family, equal to the plain answer ------------

OPENAI_PLAIN = {"output": [{"type": "message", "content": [
    {"type": "output_text", "text": "First paragraph.\n\nSecond one."}]}]}
CLAUDE_PLAIN = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Hello there. Bye."}]}
GEMINI_PLAIN = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
    {"text": "Hello there. Bye."}]}}]}
CHAT_PLAIN = {"choices": [{"finish_reason": "stop", "message": {"content": "Hello there. Bye."}}]}

OPENAI_STREAM = sse(
    {"type": "response.created", "response": {"status": "in_progress"}},
    {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "First paragraph."},
    {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "\n\nSecond one."},
    {"type": "response.completed", "response": {"status": "completed", **OPENAI_PLAIN}})
CLAUDE_STREAM = sse(
    {"type": "message_start", "message": {"content": []}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "ping"},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "there. Bye."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    {"type": "message_stop"})
GEMINI_STREAM = sse(
    {"candidates": [{"content": {"parts": [{"text": "Hello "}]}}]},
    {"candidates": [{"content": {"parts": [{"text": "there. ", "thought": True}, {"text": "there. "}]}}]},
    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Bye."}]}}]})
CHAT_STREAM = b": PROCESSING\n\n" + sse(
    {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
    {"choices": [{"delta": {"content": "Hello "}}]},
    {"choices": [{"delta": {"content": "there. Bye."}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"total_tokens": 3}}, "[DONE]")

FAMILIES = [("openai", OPENAI_STREAM, OPENAI_PLAIN), ("claude", CLAUDE_STREAM, CLAUDE_PLAIN),
            ("gemini", GEMINI_STREAM, GEMINI_PLAIN), ("groq", CHAT_STREAM, CHAT_PLAIN),
            ("openrouter", CHAT_STREAM, CHAT_PLAIN), ("mistral", CHAT_STREAM, CHAT_PLAIN)]


@pytest.mark.parametrize("provider,wire,plain", FAMILIES)
@pytest.mark.parametrize("size", [1, 5, 64, 100000])
def test_the_streamed_answer_equals_the_plain_answer(provider, wire, plain, size):
    assert stream(provider, wire, size=size) == parse_answer(provider, plain, "image")


@pytest.mark.parametrize("provider,wire,plain", FAMILIES)
def test_partial_text_only_grows_and_the_final_call_is_the_answer(provider, wire, plain):
    seen = []
    answer = stream(provider, wire, size=11, on_partial=lambda text, final=False: seen.append((text, final)))
    assert seen[-1] == (answer, True)
    partial = [text for text, final in seen if not final]
    assert partial and all(b.startswith(a) for a, b in zip(partial, partial[1:]))
    assert all(answer.startswith(text.strip()) for text in partial)


def test_separate_text_blocks_are_joined_by_a_line_break_like_the_plain_answer():
    body = sse(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "One"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Two"}},
        {"type": "message_stop"})
    plain = {"content": [{"type": "text", "text": "One"}, {"type": "text", "text": "Two"}]}
    assert stream("claude", body) == parse_answer("claude", plain, "image") == "One\nTwo"


def test_openai_without_the_final_object_uses_the_accumulated_text():
    body = sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": " Hi "},
               {"type": "response.completed", "response": {"status": "completed"}})
    assert stream("openai", body) == "Hi"


# --- never a partial answer as a complete one ------------------------------

def category(provider, body, kind="image"):
    with pytest.raises(DescriptionError) as error:
        stream(provider, body, kind=kind)
    return error.value.category


def test_a_stream_cut_before_its_end_marker_is_a_failure():
    ends = {"openai": b'"response.completed"', "claude": b'"message_delta"', "gemini": b'"STOP"',
            "groq": b'"finish_reason"', "openrouter": b'"finish_reason"', "mistral": b'"finish_reason"'}
    for provider, wire, _plain in FAMILIES:
        end = wire.index(ends[provider])
        assert category(provider, wire[:wire.rindex(b"data:", 0, end)]) == "response", provider
        # ...and a stream cut in the middle of the ending event is no better.
        assert category(provider, wire[:end]) == "response", provider


def test_gemini_without_a_finish_reason_and_empty_text_fail():
    body = sse({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]})
    assert category("gemini", body) == "response"
    assert category("groq", sse({"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]})) == "response"


def test_refusals_and_cutoffs_map_to_the_plain_categories():
    assert category("openai", sse({"type": "response.refusal.delta", "delta": "no"})) == "refusal"
    assert category("openai", sse({"type": "response.incomplete", "response": {}})) == "response"
    assert category("openai", sse({"type": "error", "message": "private"})) == "response"
    assert category("claude", sse({"type": "message_delta", "delta": {"stop_reason": "refusal"}})) == "refusal"
    assert category("claude", sse({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}})) == "response"
    assert category("claude", sse({"type": "error", "error": {"type": "overloaded_error"}})) == "server"
    assert category("claude", sse({"type": "error", "error": {"type": "rate_limit_error"}})) == "quota"
    assert category("claude", sse({"type": "error", "error": {"type": "weird"}})) == "response"
    assert category("gemini", sse({"promptFeedback": {"blockReason": "SAFETY"}})) == "refusal"
    assert category("gemini", sse({"candidates": [{"finishReason": "SAFETY"}]})) == "refusal"
    assert category("gemini", sse({"candidates": [{"finishReason": "MAX_TOKENS"}]})) == "response"
    assert category("gemini", sse({"error": {"code": 500}})) == "response"
    for provider in ("groq", "openrouter", "mistral"):
        assert category(provider, sse({"choices": [{"delta": {"refusal": "no"}}]})) == "refusal"
        assert category(provider, sse({"choices": [{"delta": {}, "finish_reason": "content_filter"}]})) == "refusal"
        for reason in ("length", "model_length", "error"):
            assert category(provider, sse({"choices": [{"delta": {}, "finish_reason": reason}]})) == "response"
        assert category(provider, sse({"error": {"message": "private"}})) == "response"
        assert category(provider, sse({"choices": [{"delta": {"content": ["chunk"]}}]})) == "response"


def test_an_error_in_the_middle_discards_the_text_already_received():
    body = sse({"choices": [{"delta": {"content": "Half an answer. More"}}]},
               {"error": {"message": "boom"}})
    seen = []
    with pytest.raises(DescriptionError):
        read_stream(StreamResponse(body), "groq", "image", lambda: None,
                    lambda text, final=False: seen.append(final))
    assert seen == [False]  # no final=True: it was never a complete answer


def test_garbage_events_fail_as_response():
    assert category("groq", b"data: {not json}\n\n") == "response"
    assert category("groq", b"data: [1, 2]\n\n") == "response"
    assert category("claude", b"data: [DONE]\n\n") == "response"


# --- limits ----------------------------------------------------------------

def test_a_pdf_over_its_character_limit_ends_with_output_limit(monkeypatch):
    monkeypatch.setitem(config.MAX_OUTPUT_CHARS, "pdf", 20)
    body = sse(*({"choices": [{"delta": {"content": "0123456789"}}]} for _ in range(5)))
    assert category("openrouter", body, kind="pdf") == "output_limit"


def test_a_description_over_its_limit_is_cut_like_the_plain_answer(monkeypatch):
    monkeypatch.setitem(config.MAX_OUTPUT_CHARS, "image", 8)
    body = sse({"choices": [{"delta": {"content": "0123456789"}, "finish_reason": "stop"}]})
    assert stream("groq", body) == "01234567"


def test_the_stream_is_bounded_in_bytes(monkeypatch):
    body = sse(*({"choices": [{"delta": {"content": "x"}}]} for _ in range(50)))
    with pytest.raises(DescriptionError, match="ai_error_response"):
        read_stream(StreamResponse(body, size=50), "groq", "image", lambda: None, max_bytes=100)


def test_a_cancel_stops_reading_at_once():
    response = StreamResponse(CHAT_STREAM, size=5)

    def check():
        if response.reads >= 2:
            raise DescriptionError("cancelled")
    with pytest.raises(DescriptionError, match="ai_error_cancelled"):
        read_stream(response, "groq", "image", check)
    assert response.reads == 2


def test_a_plain_json_body_is_read_as_before():
    body = json.dumps(CHAT_PLAIN).encode()
    response = StreamResponse(body, content_type="application/json")
    assert read_stream(response, "groq", "image", lambda: None) == "Hello there. Bye."


# --- the request -----------------------------------------------------------

def test_which_provider_and_kind_may_stream():
    for provider in config.PROVIDERS:
        assert not supports_streaming(provider, "audio")
    assert not supports_streaming("mistral", "pdf")  # OCR is not a chat request
    assert not supports_streaming("groq", "pdf")     # not accepted at all
    assert supports_streaming("openai", "pdf") and supports_streaming("claude", "text")
    assert supports_streaming("mistral", "image") and supports_streaming("gemini", "writing")
    assert not supports_streaming("nobody", "image")


@pytest.mark.parametrize("provider", ["openai", "claude", "groq", "openrouter", "mistral"])
def test_the_streaming_request_only_adds_the_stream_flag(provider):
    plain = build_request(provider, config.PROVIDERS[provider].model, "k", IMAGE, (), "q", "i", "balanced")
    streamed = streaming_request(provider, plain)
    assert streamed.url == plain.url
    assert streamed.json == {**plain.json, "stream": True} and "stream" not in plain.json
    assert streamed.headers["Accept"] == "text/event-stream"
    assert {k: v for k, v in streamed.headers.items() if k != "Accept"} == plain.headers


def test_gemini_streams_through_its_sse_method_with_the_key_in_a_header():
    plain = build_request("gemini", "gemini-x", "k", IMAGE, (), "q", "i", "balanced")
    streamed = streaming_request("gemini", plain)
    assert streamed.url.endswith("/models/gemini-x:streamGenerateContent?alt=sse")
    assert streamed.json == plain.json and "k" not in streamed.url
    assert streamed.headers["x-goog-api-key"] == "k"


# --- what the window shows -------------------------------------------------

def test_settled_prefix_holds_back_the_unfinished_tail():
    assert settled_prefix("One.\n\nTwo is being wri") == "One."
    assert settled_prefix("Line one\nLine tw") == "Line one"
    assert settled_prefix("First sentence. Second is being wri") == "First sentence."
    assert settled_prefix("A single unfinished sentence") == ""
    assert settled_prefix("  \n Hello world. ") == "Hello world."
    assert settled_prefix("") == ""


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def test_pacer_shows_whole_paragraphs_at_most_once_per_interval():
    clock = Clock()
    pacer = PartialPacer(1.0, clock)
    assert pacer.offer("Wor") is None
    assert pacer.offer("One.\nTw") == "One."
    assert pacer.offer("One.\nTwo.\nThr") is None  # too soon
    clock.now += 0.5
    assert pacer.offer("One.\nTwo.\nThr") is None
    clock.now += 0.6
    assert pacer.offer("One.\nTwo.\nThr") == "One.\nTwo."
    assert pacer.offer("One.\nTwo.\nThr") is None  # nothing new
    clock.now += 5
    assert pacer.offer("One.\nTwo.\nThr") is None


def test_pacer_resets_when_another_provider_starts_over():
    clock = Clock()
    pacer = PartialPacer(1.0, clock)
    assert pacer.offer("Old answer.\nmore") == "Old answer."
    assert pacer.offer("") == ""
    assert pacer.offer("") is None
    assert pacer.offer("New.\nx") == "New."  # not held back by the interval
    clock.now += 0.1
    assert pacer.offer("Different.\nx") == "Different."


# --- the service -----------------------------------------------------------

def run_service(http, on_partial=None, providers=("groq",), kind="image", media=IMAGE):
    operation = service.Operation(kind)
    models = {p: config.PROVIDERS[p].model for p in config.PROVIDERS}
    return service.run_chain(operation, list(providers), models, lambda p: "key", media, (), "q", "i",
                             "balanced", session_factory=lambda: http, on_partial=on_partial)


def test_run_chain_streams_when_asked_and_not_otherwise():
    http = FakeHTTP(StreamResponse(CHAT_STREAM))
    seen = []
    answer, provider = run_service(http, lambda text, final=False: seen.append(text))
    assert (answer, provider) == ("Hello there. Bye.", "groq") and seen
    assert http.calls[0][1]["json"]["stream"] is True
    assert http.calls[0][1]["allow_redirects"] is False and http.calls[0][1]["stream"] is True
    plain = FakeHTTP(StreamResponse(json.dumps(CHAT_PLAIN).encode(), content_type="application/json"))
    assert run_service(plain)[0] == "Hello there. Bye."
    assert "stream" not in plain.calls[0][1]["json"]


def test_audio_and_mistral_ocr_take_the_plain_path_even_when_streaming_is_asked():
    audio = Media("audio", b"x", "audio/ogg", "a.ogg")
    http = FakeHTTP(StreamResponse(json.dumps({"text": "spoken"}).encode(), content_type="application/json"))
    assert run_service(http, lambda *a: None, kind="audio", media=audio)[0] == "spoken"
    assert http.calls[0][1]["files"] is not None and http.calls[0][1]["json"] is None
    pdf = Media("pdf", b"%PDF", "application/pdf", "d.pdf")
    ocr = FakeHTTP(StreamResponse(json.dumps({"pages": [{"index": 0, "markdown": "Page"}],
                                              "usage_info": {"pages_processed": 1}}).encode(),
                                  content_type="application/json"))
    assert run_service(ocr, lambda *a: None, providers=("mistral",), kind="pdf", media=pdf)[0] == "Page"
    assert ocr.calls[0][0].endswith("/ocr") and "stream" not in ocr.calls[0][1]["json"]


def test_http_errors_and_retry_after_work_on_the_streaming_path():
    class Sequence(FakeHTTP):
        def __init__(self):
            super().__init__()
            self.responses = [StreamResponse(b"", status=429, headers={"Retry-After": "1"}),
                              StreamResponse(CHAT_STREAM)]

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)
    http = Sequence()
    operation = service.Operation("image")
    operation.cancelled.wait = lambda timeout=None: False  # the pause is not slept
    models = {p: config.PROVIDERS[p].model for p in config.PROVIDERS}
    answer, _provider = service.run_chain(operation, ["groq"], models, lambda p: "key", IMAGE, (), "q", "i",
                                          "balanced", session_factory=lambda: http,
                                          on_partial=lambda *a: None)
    assert answer == "Hello there. Bye." and len(http.calls) == 2


def test_a_failed_attempt_tells_the_listener_to_drop_its_partial_text():
    class Sequence(FakeHTTP):
        def __init__(self):
            super().__init__()
            half = sse({"choices": [{"delta": {"content": "Half. of an answer"}}]})  # cut off
            self.responses = [StreamResponse(half), StreamResponse(CHAT_STREAM)]

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)
    seen = []
    answer, provider = run_service(Sequence(), lambda text, final=False: seen.append((text, final)),
                                   providers=("groq", "openrouter"))
    assert provider == "openrouter" and answer == "Hello there. Bye."
    assert seen[0] == ("Half. of an answer", False) and seen[1] == ("", False)
    assert seen[-1] == ("Hello there. Bye.", True)


def test_a_cancel_closes_the_connection_while_streaming():
    response = StreamResponse(CHAT_STREAM, size=3)
    http = FakeHTTP(response)
    operation = service.Operation("image")
    models = {p: config.PROVIDERS[p].model for p in config.PROVIDERS}

    def on_partial(text, final=False):
        operation.cancel()
    with pytest.raises(DescriptionError) as error:
        service.run_chain(operation, ["groq"], models, lambda p: "key", IMAGE, (), "q", "i", "balanced",
                          session_factory=lambda: http, on_partial=on_partial)
    assert error.value.category == "cancelled" and response.closed
