"""HTTPS verified the way the machine verifies it, not the way we shipped it.

`requests` verifies against `certifi`'s baked-in CA list, which knows nothing
about the computer it runs on. On a machine whose TLS is intercepted locally —
an antivirus with HTTPS scanning on, a corporate proxy, an MDM profile — the
certificate presented is signed by a root that exists only in *Windows'* own
store, and every download fails with CERTIFICATE_VERIFY_FAILED. Measured on a
real install against huggingface.co, nodejs.org and api.github.com: all three
failed, all three worked once verification went through the system store.

Which is three unrelated features breaking at once, for a reason none of them
explains to the user: the Whisper models (up to 3 GB, and without them there is
no transcription), the portable Node.js runtime WinZapp needs before it can talk
to WhatsApp at all, and the updater.

The rules this file pins:

* **Absence is never an error.** No truststore, or a Windows that refuses to
  give a context, leaves an ordinary session behaving exactly as the code did
  before. A certificate helper that could take the updater down on import
  would be a worse bug than the one it fixes.
* **The proxy path counts too**, because a machine that intercepts TLS is very
  often the same machine that routes through a proxy.
* **Every internet download goes through it.** The three modules that fetch
  from outside `127.0.0.1` are checked by name — a fourth call site added later
  and left on bare `requests.get` would fail for that user with no clue why.
"""

import builtins
import logging
import os
import ssl

import pytest
import requests

from core import tls_trust
from core.transcription import errors, model_catalog, model_store

# The modules that reach the internet over HTTPS from Python. Most of the app
# talks to the local WPPConnect server over plain HTTP on 127.0.0.1, where
# there is no certificate to verify and nothing here applies.
_DOWNLOADERS = (
    os.path.join("client", "updater.py"),
    os.path.join("client", "ui", "dialogs", "node_download.py"),
    os.path.join("client", "core", "transcription", "model_store.py"),
    os.path.join("client", "core", "transcription", "cuda_runtime.py"),
    os.path.join("client", "core", "transcription", "whisper_cpp_runtime.py"),
    # The noise reducer's CUDA components (pinned wheels from PyPI).
    os.path.join("client", "core", "cuda_components", "store.py"),
    # The AI image/audio description providers: HTTPS to third-party APIs, with
    # the session injectable (session_factory=tls_trust.create_session).
    os.path.join("client", "core", "ai_media", "service.py"),
    os.path.join("client", "core", "ai_media", "model_catalog.py"),
    # Not a download, but the same certificate: the reachability probe's HEAD
    # at web.whatsapp.com. On the bundled CA list an intercepted machine read
    # as offline (tests/test_offline_session_start_deferral.py::
    # TestAnInterceptedCertificateIsNotAnOutage pins the behaviour, this
    # only the wiring). Its retry on http_pool's certifi session after an
    # SSLError is deliberate, not a way back to the bundled list:
    # ::TestAStoreThatCannotBuildTheChainIsNotAnOutageEither, in that module.
    os.path.join("client", "main_window", "connection.py"),
)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _no_truststore(monkeypatch):
    """Make `import truststore` fail, as it does on an install without it."""
    real_import = builtins.__import__

    def _watch(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("no truststore in this install")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _watch)


class TestSessionConstruction:
    def test_https_is_verified_through_the_system_store(self):
        pytest.importorskip("truststore")
        session = tls_trust.create_session()
        adapter = session.get_adapter("https://example.invalid/")
        assert isinstance(adapter, tls_trust._SystemTrustAdapter)

    def test_plain_http_is_left_alone(self):
        """There is nothing to verify on http://, and the local WPPConnect
        server is reached that way — this must not touch it."""
        session = tls_trust.create_session()
        adapter = session.get_adapter("http://127.0.0.1:6300/api/")
        assert not isinstance(adapter, tls_trust._SystemTrustAdapter)

    def test_the_context_is_used_for_proxied_connections_too(self):
        context = ssl.create_default_context()
        adapter = tls_trust._SystemTrustAdapter(context)
        manager = adapter.proxy_manager_for("http://proxy.invalid:8080")
        assert manager.connection_pool_kw.get("ssl_context") is context

    def test_a_missing_truststore_is_not_an_error(self, monkeypatch):
        """The whole point of the fallback: worse verification, never a crash."""
        _no_truststore(monkeypatch)
        monkeypatch.setattr(tls_trust, "_reported", False)

        assert tls_trust.system_ssl_context() is None
        session = tls_trust.create_session()

        assert isinstance(session, requests.Session)
        # Still a usable session, with requests' own adapter behind it.
        assert session.get_adapter("https://example.invalid/") is not None
        assert not isinstance(
            session.get_adapter("https://example.invalid/"),
            tls_trust._SystemTrustAdapter,
        )

    def test_a_context_that_cannot_be_built_is_not_an_error_either(self, monkeypatch):
        class _Broken:
            @staticmethod
            def SSLContext(*args, **kwargs):
                raise ValueError("this Windows says no")

        monkeypatch.setitem(__import__("sys").modules, "truststore", _Broken)
        monkeypatch.setattr(tls_trust, "_reported", False)
        assert tls_trust.system_ssl_context() is None

    def test_an_adapter_that_cannot_be_installed_still_yields_a_session(
        self, monkeypatch
    ):
        """The context is only half of it: building the adapter runs
        HTTPAdapter.__init__ -> init_poolmanager, which hands `ssl_context` to
        urllib3 — a pinned dependency that gets bumped like any other. A version
        that renamed or validated that keyword would take the model download,
        the Node download and the background update check down together, which
        is the one failure this module exists to prevent."""

        real_adapter = tls_trust._SystemTrustAdapter

        def _explode(_context, **_kwargs):
            raise TypeError("urllib3 does not like this keyword any more")

        monkeypatch.setattr(tls_trust, "_SystemTrustAdapter", _explode)
        session = tls_trust.create_session()

        assert session is not None
        # Left on the stock adapter rather than a half-built one.
        assert not isinstance(
            session.get_adapter("https://example.invalid"), real_adapter
        )
        session.close()


class TestLogging:
    def test_which_path_was_taken_is_logged(self, monkeypatch, caplog):
        """"Why does it work on his machine" is the question this answers."""
        monkeypatch.setattr(tls_trust, "_reported", False)
        caplog.set_level(logging.INFO)

        tls_trust.create_session()

        lines = [r.getMessage() for r in caplog.records if "[tls]" in r.getMessage()]
        assert len(lines) == 1

    def test_it_is_logged_once_and_not_per_request(self, monkeypatch, caplog):
        """A line per call would bury the log of a 3 GB download."""
        monkeypatch.setattr(tls_trust, "_reported", False)
        caplog.set_level(logging.INFO)

        for _ in range(3):
            tls_trust.create_session()

        lines = [r.getMessage() for r in caplog.records if "[tls]" in r.getMessage()]
        assert len(lines) == 1

    def test_the_fallback_says_why(self, monkeypatch, caplog):
        _no_truststore(monkeypatch)
        monkeypatch.setattr(tls_trust, "_reported", False)
        caplog.set_level(logging.INFO)

        tls_trust.create_session()

        assert "no truststore in this install" in caplog.text


class TestGet:
    def test_get_goes_through_a_session_and_closes_it(self, monkeypatch):
        """`requests.get` builds and closes a session per call; so does this,
        which is what keeps a caller's connection lifetime unchanged."""
        calls = []

        class _Session:
            def __init__(self):
                self.closed = False

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return "response"

            def close(self):
                self.closed = True

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        session = _Session()
        monkeypatch.setattr(tls_trust, "create_session", lambda: session)

        assert tls_trust.get("https://example.invalid/x", timeout=15) == "response"
        assert calls == [("https://example.invalid/x", {"timeout": 15})]
        assert session.closed is True


class TestCallers:
    @pytest.mark.parametrize("relative", _DOWNLOADERS)
    def test_no_internet_download_is_left_on_the_bundled_ca_list(self, relative):
        with open(os.path.join(_repo_root(), relative), encoding="utf-8") as handle:
            source = handle.read()
        assert "tls_trust" in source, f"{relative} does not use the trust layer"
        assert "requests.get(" not in source, f"{relative} still calls requests.get"
        assert "requests.Session()" not in source, (
            f"{relative} still builds a plain session"
        )

    def test_the_model_download_defaults_to_a_system_trust_session(
        self, tmp_path, monkeypatch
    ):
        """model_store takes an injectable session, so this is its default.

        Driven all the way to a failure rather than stubbed at the seam: what
        matters is that the session the download *actually* uses is the one
        this module builds.
        """
        created = []

        class _DeadSession:
            def get(self, *args, **kwargs):
                raise requests.ConnectionError("no network in a test")

            def close(self):
                pass

        def _create():
            created.append(True)
            return _DeadSession()

        monkeypatch.setattr(model_store.tls_trust, "create_session", _create)

        with pytest.raises(errors.TranscriptionError) as caught:
            model_store.download_model(
                model_catalog.get_model("tiny"), str(tmp_path / "models")
            )

        assert caught.value.code == errors.MODEL_DOWNLOAD_FAILED
        assert created == [True]
