"""Downloading a large document, which never worked.

Reported by several users: a 200 MB document says it is downloading, takes a
very long time, downloads nothing, fills the machine's RAM, and the button
returns to "Download". Sending files that size started working once the upload
was cut into bounded chunks; the download was never given the same treatment.

Two independent faults, and each one alone is enough to lose the file.

**The response was base64 inside JSON.** For a 200 MB file the server holds the
200 MB Buffer, a 267 MB base64 string and the ~267 MB string `JSON.stringify`
builds — and emits nothing until all three exist. Python then holds the chunks,
the joined body, its decoded str, the str `json.loads` builds, the decoded
bytes and finally encrypt()'s Fernet token: roughly 1.3 GB in this process for
one 200 MB file. Past about 400 MB the server cannot answer at all —
`toString('base64')` exceeds V8's maximum string length and throws.

**The read timeout was a flat 60 seconds.** `requests` measures a read timeout
*between bytes*, which is normally forgiving — except the server sends no bytes
at all while it fetches the file from WhatsApp's CDN and decrypts it. That
silence is the whole download, so the client abandoned the request while the
server was still working on it. The server keeps going, holding everything
above, which is the memory the user watches fill.
"""

import inspect
import types

import pytest

import main
from main import MainWindow, media_fetch_timeout, _looks_like_json_response


def _doc(size=None):
    inner = {} if size is None else {"fileLength": size}
    return {"message": {"documentMessage": inner}}


class TestTheTimeoutKnowsHowBigTheFileIs:
    def test_a_photo_keeps_the_old_budget(self):
        assert media_fetch_timeout(_doc(50 * 1024)) == 60

    def test_a_large_document_gets_far_longer(self):
        """60s could never have covered it: the server is silent for the whole
        CDN download."""
        assert media_fetch_timeout(_doc(200 * 1024 ** 2)) > 900

    def test_it_grows_with_the_file(self):
        assert (media_fetch_timeout(_doc(200 * 1024 ** 2))
                > media_fetch_timeout(_doc(20 * 1024 ** 2))
                > media_fetch_timeout(_doc(2 * 1024 ** 2)))

    def test_it_is_capped(self):
        """A declared size is wrong-by-accident in practice and
        attacker-controlled in principle; a request that hangs forever is a
        worker thread that never comes back."""
        assert media_fetch_timeout(_doc(500 * 1024 ** 3)) <= 30 * 60

    @pytest.mark.parametrize("msg", [
        {}, None, {"message": None}, _doc(), _doc("abc"), _doc(None), _doc(0),
        _doc(-1),
    ])
    def test_anything_unreadable_falls_back_to_the_flat_budget(self, msg):
        """A message that declares nothing must behave exactly as before."""
        assert media_fetch_timeout(msg) == 60

    def test_a_numeric_string_is_accepted(self):
        """WPPConnect hands fileLength through as a string often enough."""
        assert media_fetch_timeout(_doc("209715200")) == media_fetch_timeout(
            _doc(209715200))

    def test_the_caller_passes_it(self):
        source = inspect.getsource(MainWindow.handle_media_message)
        assert "media_fetch_timeout(msg, timeout)" in source, (
            "handle_media_message must scale its own timeout — a flat 60s is "
            "what abandoned the request mid-download"
        )


class TestTellingTheTwoResponseShapesApart:
    """client/api/ is reinstalled independently of this app, so a newer client
    must keep reading an older server's base64 JSON."""

    def _response(self, content_type):
        headers = {} if content_type is None else {"Content-Type": content_type}
        return types.SimpleNamespace(headers=headers)

    @pytest.mark.parametrize("content_type", [
        "application/json", "application/json; charset=utf-8", "APPLICATION/JSON",
    ])
    def test_json_is_recognised(self, content_type):
        assert _looks_like_json_response(self._response(content_type)) is True

    @pytest.mark.parametrize("content_type", [
        "application/octet-stream", "application/pdf", "video/mp4", "image/jpeg",
    ])
    def test_a_file_body_is_recognised(self, content_type):
        assert _looks_like_json_response(self._response(content_type)) is False

    def test_a_missing_header_is_treated_as_the_old_shape(self):
        """An unknown shape must keep the behaviour it has always had, rather
        than writing raw JSON to disk as if it were the file."""
        assert _looks_like_json_response(self._response(None)) is True

    def test_unreadable_headers_are_treated_as_the_old_shape(self):
        broken = types.SimpleNamespace()
        assert _looks_like_json_response(broken) is True

    def test_it_never_touches_the_body(self):
        """Sniffing means reading, and the body may be hundreds of megabytes
        that must be read exactly once."""
        source = inspect.getsource(_looks_like_json_response)
        for attribute in (".content", ".text", ".json(", ".iter_content"):
            assert attribute not in source


class TestTheRequestAsksForBytes:
    def test_the_binary_path_negotiates(self):
        source = inspect.getsource(MainWindow.get_base64_from_media)
        assert 'headers["Accept"] = "application/octet-stream"' in source

    def test_negotiation_is_conditional_so_old_servers_still_work(self):
        source = inspect.getsource(MainWindow.get_base64_from_media)
        accept = source.index('headers["Accept"]')
        guard = source.rindex("if _binary:", 0, accept)
        assert guard < accept

    def test_the_success_path_no_longer_decodes_the_body_just_to_log_it(self):
        """response.text on a 200 MB body is a quarter-gigabyte allocation
        whose only purpose was a 200-character log line — and on the binary
        path, a str built out of arbitrary bytes."""
        source = inspect.getsource(MainWindow.get_base64_from_media)
        snippet = source[source.index('resp_text = ""'):]
        snippet = snippet[:snippet.index("logging.info")]
        assert "status_code not in (200, 201)" in snippet

    def test_the_streaming_path_no_longer_joins_a_chunk_list(self):
        """The join doubles the peak, and this is the path the Download button
        uses — so it is the one a large document dies on."""
        source = inspect.getsource(MainWindow.get_base64_from_media)
        assert 'b"".join(chunks)' not in source
        assert "body = bytearray()" in source

    def test_handle_media_message_takes_bytes_not_base64(self):
        source = inspect.getsource(MainWindow.handle_media_message)
        assert "fetch_media_bytes(" in source
        assert "base64.b64decode" not in source


class TestTheServerSendsRawBytes:
    @staticmethod
    def _source():
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "client" / "api_patches"
                / "src" / "controller" / "sessionController.ts").read_text(
                    encoding="utf-8")

    def test_the_decrypt_paths_go_through_the_shared_sender(self):
        source = self._source()
        media = source[source.index("export async function getMediaByMessage("):]
        assert "sendMediaBuffer(req, res, buffer" in media
        # The two base64+JSON responses those paths used are gone.
        assert "base64: buffer.toString('base64')" not in media

    def test_it_still_answers_an_older_client_with_json(self):
        source = self._source()
        sender = source[source.index("function sendMediaBuffer("):]
        sender = sender[:sender.index("\n}")]
        assert "buffer.toString('base64')" in sender, (
            "an older WinZapp only knows how to read {base64, mimetype}, and "
            "client/api/ updates independently of it"
        )

    def test_raw_bytes_are_only_sent_when_asked_for(self):
        source = self._source()
        sender = source[source.index("function sendMediaBuffer("):]
        sender = sender[:sender.index("\n}")]
        assert "application/octet-stream" in sender
        assert "req.headers['accept']" in sender

    def test_the_mimetype_survives_the_binary_response(self):
        """The body is the file now, so the mimetype needs its own header."""
        source = self._source()
        sender = source[source.index("function sendMediaBuffer("):]
        sender = sender[:sender.index("\n}")]
        assert "x-winzapp-mimetype" in sender
