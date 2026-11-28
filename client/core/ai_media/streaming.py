"""Server-sent events for a provider's answer: read it as it is written.

Pure parts (no network, no wx): the SSE line parser, one accumulator per
provider family, the paragraph pacing the result window uses, and the request
rewrite that asks for a stream. ``read_stream`` is the only function that
touches a response, and only through ``response.raw.read1`` like
``bounded_http.read_bounded``.

The streamed answer is the same answer the plain request would have returned:
it ends in ``providers.clean_answer`` (non-empty, trimmed, the same output
limits) and a provider's refusal, cut-off or in-stream error raises the same
``DescriptionError`` as ``providers.parse_answer``. What was received so far is
only ever offered to ``on_partial``; a stream that ends without its own end
marker is a cut-off answer and fails as ``response``, never shown as complete.
Nothing here logs: not a chunk, an event or a word of the text.
"""
import codecs
import json
import re
import time

from core.bounded_http import read_bounded

from .config import MAX_OUTPUT_CHARS, MAX_RESPONSE_BYTES, PROVIDERS, supports
from .errors import DescriptionError
from .providers import clean_answer, parse_answer

#: The kinds whose answer is plain generated text. Voice messages go to a
#: multipart transcription endpoint and Mistral's OCR is not a chat request.
STREAM_KINDS = frozenset({"image", "sticker", "video", "pdf", "text", "writing"})
#: Server-sent events wrap every few characters in a JSON object, so the same
#: answer is several times larger than the plain body ``MAX_RESPONSE_BYTES``
#: bounds; this is that bound for a stream. The text itself is still held to
#: ``MAX_OUTPUT_CHARS``.
MAX_STREAM_BYTES = 8 * MAX_RESPONSE_BYTES
#: One event (and one unfinished line) larger than this is not an answer.
MAX_EVENT_BYTES = MAX_RESPONSE_BYTES
_READ_BYTES = 8192
_LIMITED_KINDS = frozenset({"audio", "pdf", "text"})
_LINE_END = re.compile(r"\r\n|\n|\r")


def supports_streaming(provider, kind):
    """Whether this provider's answer to this kind can be requested as a
    stream; the caller falls back to the plain request when it cannot."""
    return (provider in PROVIDERS and kind in STREAM_KINDS and supports(provider, kind)
            and not (provider == "mistral" and kind == "pdf"))


def streaming_request(provider, request):
    """``request`` asking for server-sent events: the same body with the
    stream flag (a different method of the same URL for Gemini), the key still
    only in a header and no new field."""
    headers = {**request.headers, "Accept": "text/event-stream"}
    if provider == "gemini":
        if ":generateContent" not in request.url:
            raise DescriptionError("request")
        return request._replace(
            url=request.url.replace(":generateContent", ":streamGenerateContent") + "?alt=sse",
            headers=headers)
    if not isinstance(request.json, dict):
        raise DescriptionError("request")
    return request._replace(headers=headers, json={**request.json, "stream": True})


class SSEParser:
    """The event stream format: lines end in LF, CR or CRLF (a CRLF may be cut
    between two chunks); ``data:`` lines of one event are joined with a line
    feed and the event is dispatched by a blank line; comments (``:``) and the
    ``event``, ``id`` and ``retry`` fields are not needed (every provider's JSON
    carries its own type) and are dropped. An event the stream ends in the
    middle of is discarded, as the format says."""

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._data = []
        self._data_size = 0
        self._skip_lf = False

    def feed(self, chunk):
        """The data of every event ``chunk`` completes, in order."""
        text = self._buffer + self._decoder.decode(chunk)
        if self._skip_lf and text.startswith("\n"):
            text = text[1:]
        self._skip_lf = text.endswith("\r")
        lines = _LINE_END.split(text)
        self._buffer = lines.pop()
        if len(self._buffer) > MAX_EVENT_BYTES:
            raise DescriptionError("response")
        events = []
        for line in lines:
            if not line:
                if self._data:
                    events.append("\n".join(self._data))
                self._data, self._data_size = [], 0
            elif line.startswith("data:"):
                value = line[5:]
                value = value[1:] if value.startswith(" ") else value
                self._data_size += len(value)
                if self._data_size > MAX_EVENT_BYTES:
                    raise DescriptionError("response")
                self._data.append(value)
        return events


def _error_category(kind):
    """A provider's own error type, sent inside a stream that already answered
    200, as the category its HTTP status would have been."""
    return {"overloaded_error": "server", "api_error": "server",
            "rate_limit_error": "quota", "authentication_error": "authentication",
            "permission_error": "authentication"}.get(kind, "response")


class StreamAnswer:
    """Accumulates one provider's events into its answer.

    ``feed`` takes the data of one event; ``text`` is what has arrived;
    ``finished`` is true once the provider said the answer ended;
    ``result()`` is the validated final answer."""

    def __init__(self, provider, kind):
        self.provider = provider
        self.kind = kind
        self.finished = False
        self._family = ("responses" if provider == "openai" else provider
                        if provider in ("gemini", "claude") else "chat")
        self._parts = []
        self._length = 0
        self._block = None
        self._joined = ""
        self._final = None

    @property
    def text(self):
        if len(self._joined) != self._length:
            self._joined = "".join(self._parts)
        return self._joined

    def _add(self, block, text):
        if not text:
            return
        if not isinstance(text, str):
            raise DescriptionError("response")
        if self._block is not None and block != self._block and self._parts:
            # Separate text parts are joined by a line break in the plain answer.
            self._parts.append("\n")
            self._length += 1
        self._block = block
        self._parts.append(text)
        self._length += len(text)
        limit = MAX_OUTPUT_CHARS[self.kind]
        if self.kind in _LIMITED_KINDS and self._length > limit and len(self.text.strip()) > limit:
            raise DescriptionError("output_limit")

    def feed(self, data):
        if self.finished:
            return
        if data.strip() == "[DONE]":
            if self._family != "chat":
                raise DescriptionError("response")
            self.finished = True
            return
        try:
            event = json.loads(data)
        except ValueError:
            raise DescriptionError("response") from None
        if not isinstance(event, dict):
            raise DescriptionError("response")
        try:
            getattr(self, "_" + self._family)(event)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise DescriptionError("response") from None

    def _responses(self, event):
        kind = event.get("type")
        if kind == "response.output_text.delta":
            self._add((event.get("output_index"), event.get("content_index")), event.get("delta"))
        elif kind in ("response.refusal.delta", "response.refusal.done"):
            raise DescriptionError("refusal")
        elif kind in ("response.failed", "response.incomplete", "error"):
            raise DescriptionError("response")
        elif kind == "response.completed":
            response = event.get("response")
            if isinstance(response, dict) and response.get("output"):
                # The finished response object is the very body the plain
                # request returns: same parser, same text.
                self._final = parse_answer(self.provider, response, self.kind)
            self.finished = True

    def _claude(self, event):
        kind = event.get("type")
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "text":
                self._add(event.get("index"), block.get("text"))
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                self._add(event.get("index"), delta.get("text"))
        elif kind == "message_delta":
            reason = (event.get("delta") or {}).get("stop_reason")
            if reason == "refusal":
                raise DescriptionError("refusal")
            if reason == "max_tokens":
                raise DescriptionError("response")
        elif kind == "message_stop":
            self.finished = True
        elif kind == "error":
            raise DescriptionError(_error_category((event.get("error") or {}).get("type")))

    def _gemini(self, event):
        if "error" in event:
            raise DescriptionError("response")
        candidates = event.get("candidates") or []
        if (event.get("promptFeedback") or {}).get("blockReason") or any(
                c.get("finishReason") in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST")
                for c in candidates):
            raise DescriptionError("refusal")
        if any(c.get("finishReason") == "MAX_TOKENS" for c in candidates):
            raise DescriptionError("response")
        for candidate in candidates[:1]:
            for part in (candidate.get("content") or {}).get("parts") or []:
                if "text" in part and not part.get("thought"):
                    self._add(0, part["text"])
        # Gemini's stream has no end marker of its own: the last chunk's
        # finish reason is it.
        if any(c.get("finishReason") for c in candidates):
            self.finished = True

    def _chat(self, event):
        if "error" in event:
            raise DescriptionError("response")
        choices = event.get("choices") or []
        if not choices:
            return  # a usage-only chunk
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("refusal"):
            raise DescriptionError("refusal")
        self._add(0, delta.get("content"))
        reason = choice.get("finish_reason")
        if reason == "content_filter":
            raise DescriptionError("refusal")
        if reason in ("length", "model_length", "error"):
            raise DescriptionError("response")
        if reason is not None:
            self.finished = True

    def result(self):
        """The final answer, or the error the plain request would have raised.
        A stream that stopped before its end marker was cut off."""
        if not self.finished:
            raise DescriptionError("response")
        if self._final is not None:
            return self._final
        return clean_answer(self.text, self.kind)


def read_stream(response, provider, kind, check, on_partial=None, max_bytes=MAX_STREAM_BYTES):
    """The answer of a 200 response. ``check`` raises on cancel or deadline
    (called around every read, like ``read_bounded``); ``on_partial(text,
    final)`` hears the text received so far after each network chunk that grew
    it, then the final answer with ``final=True``. A provider that ignored the
    request and sent its plain JSON body is read as before."""
    check()
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in content_type:
        return parse_answer(provider, json.loads(read_bounded(response, MAX_RESPONSE_BYTES, check)),
                            kind)
    answer = StreamAnswer(provider, kind)
    parser = SSEParser()
    total = 0
    shown = 0
    while not answer.finished:
        check()
        chunk = response.raw.read1(_READ_BYTES, decode_content=True)
        check()
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DescriptionError("response")
        for data in parser.feed(chunk):
            answer.feed(data)
            if answer.finished:
                break
        if on_partial is not None and not answer.finished and len(answer.text) != shown:
            shown = len(answer.text)
            on_partial(answer.text, False)
    final = answer.result()
    if on_partial is not None:
        on_partial(final, True)
    return final


_SENTENCE_END = re.compile(r"[.!?…。！？؟][\"'”’)\]]*\s")


def settled_prefix(text):
    """The part of ``text`` that is whole lines or, when no line has ended yet,
    whole sentences; the unfinished tail is held back so the window never shows
    a half-written word."""
    text = text.lstrip()
    cut = text.rfind("\n")
    if cut >= 0:
        return text[:cut].rstrip()
    last = None
    for last in _SENTENCE_END.finditer(text):
        pass
    return text[:last.end()].rstrip() if last else ""


class PartialPacer:
    """What to show of a growing answer: ``offer(text)`` returns the settled
    text when it is longer than what was shown and ``interval`` seconds have
    passed since the last update, ``""`` when the answer restarted (another
    provider's attempt), else None. Never a character at a time and never
    more than one update per interval."""

    def __init__(self, interval=1.0, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self._shown = ""
        self._at = None

    def offer(self, text):
        if not text:
            if self._shown:
                self._shown, self._at = "", None
                return ""
            return None
        settled = settled_prefix(text)
        restarted = bool(self._shown) and not settled.startswith(self._shown)
        if not restarted and len(settled) <= len(self._shown):
            return None
        now = self.clock()
        if not restarted and self._at is not None and now - self._at < self.interval:
            return None
        self._shown, self._at = settled, now
        return settled
