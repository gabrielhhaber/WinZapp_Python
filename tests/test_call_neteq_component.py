"""The call playback component (NetEq) as an optional download (#768, Tier C).

What is pinned here: the download is never offered without a pinned release
(tag, sizes, digests); a downloaded DLL is loaded only when its SHA-256 is the
pinned one, and before the bridge's own copy; a damaged or missing component
leaves calls on the compatible playback; removal says when Windows kept a DLL
a WinZapp process has loaded. No window is created and no network is reached.
"""
import hashlib
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import call_neteq, call_neteq_component as comp
from core import call_neteq_component_text as ctext
from core.transcription import model_store

DLL_BYTES = b"MZ" + bytes(range(256)) * 4
NOTICES_BYTES = b"NetEq notices\n"


def _pinned(dll=DLL_BYTES, notices=NOTICES_BYTES, tag="neteq-1.0.1-winzapp.1"):
    return replace(
        comp.NETEQ, tag=tag,
        files=((comp.DLL, len(dll)), (comp.NOTICES, len(notices))),
        digests=((comp.DLL, hashlib.sha256(dll).hexdigest()),
                 (comp.NOTICES, hashlib.sha256(notices).hexdigest())),
    )


class _Response:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=None):
        yield self._body

    def close(self):
        pass


class _Session:
    def __init__(self, bodies):
        self.bodies, self.requested = bodies, []

    def get(self, url, stream=False, timeout=None, headers=None):
        self.requested.append(url)
        return _Response(self.bodies[url])

    def close(self):
        pass


def _session_for(component, dll=DLL_BYTES, notices=NOTICES_BYTES):
    return _Session({component.url_of(comp.DLL): dll, component.url_of(comp.NOTICES): notices})


class TestThePin:
    def test_this_version_ships_unpinned_so_nothing_is_offered(self):
        # Flipped by the maintainer who publishes the release; this test then
        # checks the pin is whole instead.
        if comp.RELEASE_TAG:
            assert comp.is_pinned()
        else:
            assert not comp.is_pinned()
            with pytest.raises(ValueError):
                comp.download()

    def test_a_partial_pin_is_no_pin(self):
        assert comp.is_pinned(_pinned())
        assert not comp.is_pinned(_pinned(tag=""))
        assert not comp.is_pinned(_pinned(tag="v1.0.0"))
        assert not comp.is_pinned(replace(_pinned(), files=((comp.DLL, 0), (comp.NOTICES, 3))))
        assert not comp.is_pinned(replace(_pinned(), digests=((comp.DLL, "abc"), (comp.NOTICES, ""))))

    def test_the_url_names_this_repository_and_the_pinned_tag(self):
        url = _pinned().url_of(comp.DLL)
        assert url == ("https://github.com/gabrielhhaber/WinZapp_Python/releases/download/"
                       "neteq-1.0.1-winzapp.1/winzapp_call_neteq.dll")
        assert model_store.file_url(_pinned(), comp.DLL) == url

    def test_the_entry_speaks_the_bindings_abi(self):
        assert comp.NETEQ.abi == call_neteq.ABI == 1

    def test_an_unpinned_entry_never_vouches_for_a_file(self, tmp_path):
        path = Path(comp.installed_dll(str(tmp_path)))
        path.parent.mkdir(parents=True)
        path.write_bytes(DLL_BYTES)
        assert comp.verified_dll(str(tmp_path)) is None


class TestDownload:
    def test_a_download_installs_both_files_and_the_dll_verifies(self, tmp_path):
        component = _pinned()
        session = _session_for(component)
        path = comp.download(str(tmp_path), session=session, component=component)
        assert Path(path).read_bytes() == DLL_BYTES
        assert comp.installation_state(str(tmp_path), component) == model_store.STATE_INSTALLED
        assert comp.verified_dll(str(tmp_path), component) == path
        assert sorted(session.requested) == sorted(component.url_of(n) for n, _ in component.files)

    def test_a_dll_of_the_right_size_and_wrong_digest_is_not_loaded_and_is_repaired(self, tmp_path):
        component = _pinned()
        comp.download(str(tmp_path), session=_session_for(component), component=component)
        path = Path(comp.installed_dll(str(tmp_path), component))
        path.write_bytes(b"XX" + DLL_BYTES[2:])
        assert comp.verified_dll(str(tmp_path), component) is None
        assert comp.installation_state(str(tmp_path), component) == model_store.STATE_INCOMPLETE
        session = _session_for(component)
        comp.download(str(tmp_path), session=session, component=component)
        assert session.requested       # download_model alone would have trusted the size
        assert comp.verified_dll(str(tmp_path), component) == str(path)

    def test_bytes_that_fail_the_digest_never_reach_the_final_name(self, tmp_path):
        component = _pinned()
        with pytest.raises(Exception):
            comp.download(str(tmp_path), component=component,
                          session=_session_for(component, dll=b"YY" + DLL_BYTES[2:]))
        assert not os.path.exists(comp.installed_dll(str(tmp_path), component))


class TestRemove:
    def test_removal_deletes_the_entry_files_and_says_the_dll_went(self, tmp_path):
        component = _pinned()
        comp.download(str(tmp_path), session=_session_for(component), component=component)
        assert comp.remove(str(tmp_path), component) is True
        assert comp.installation_state(str(tmp_path), component) == model_store.STATE_ABSENT

    def test_a_dll_windows_keeps_because_it_is_loaded_is_reported(self, tmp_path, monkeypatch):
        component = _pinned()
        comp.download(str(tmp_path), session=_session_for(component), component=component)
        real_remove = os.remove

        def locked(path):
            if path.endswith(comp.DLL):
                raise PermissionError(32, "in use")
            real_remove(path)

        monkeypatch.setattr(os, "remove", locked)
        assert comp.remove(str(tmp_path), component) is False


class TestLoading:
    def test_the_verified_download_goes_first_then_existing_bridge_copies(self, tmp_path):
        bundled = tmp_path / "bridge.dll"
        bundled.write_bytes(b"MZ")
        missing = tmp_path / "missing.dll"
        assert call_neteq.candidate_paths("dl.dll", (missing, bundled)) == [Path("dl.dll"), bundled]
        assert call_neteq.candidate_paths(None, (missing, bundled)) == [bundled]
        assert call_neteq.candidate_paths(None, (missing,)) == []

    def test_a_copy_that_will_not_load_falls_through_to_the_next(self, tmp_path, monkeypatch):
        downloaded, bundled = tmp_path / "a.dll", tmp_path / "b.dll"
        downloaded.write_bytes(b"MZ")
        bundled.write_bytes(b"MZ")
        monkeypatch.setattr(comp, "verified_dll", lambda *a, **k: str(downloaded))
        monkeypatch.setattr(call_neteq, "bundled_paths", lambda: (bundled,))
        tried = []

        def library(path):
            tried.append(path.name)
            if path.name == "a.dll":
                raise ValueError("unsupported NetEq ABI")
            return SimpleNamespace(path=path)

        monkeypatch.setattr(call_neteq, "NetEqLibrary", library)
        assert call_neteq.load_library().path.name == "b.dll"
        assert tried == ["a.dll", "b.dll"]

    def test_no_component_anywhere_keeps_the_compatible_playback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(comp, "verified_dll", lambda *a, **k: None)
        monkeypatch.setattr(call_neteq, "bundled_paths", lambda: (tmp_path / "none.dll",))
        assert call_neteq.load_library() is None

    def test_a_failing_check_of_the_download_is_not_a_failed_call(self, tmp_path, monkeypatch):
        def broken(*a, **k):
            raise OSError("disk gone")

        monkeypatch.setattr(comp, "verified_dll", broken)
        monkeypatch.setattr(call_neteq, "bundled_paths", lambda: ())
        assert call_neteq.load_library() is None


class TestWhatTheWindowSays:
    def test_the_state_shown(self):
        absent, incomplete, installed = (model_store.STATE_ABSENT, model_store.STATE_INCOMPLETE,
                                         model_store.STATE_INSTALLED)
        assert ctext.section_state(installed, False, True) == installed
        assert ctext.section_state(absent, True, True) == ctext.STATE_BUNDLED
        assert ctext.section_state(absent, False, False) == ctext.STATE_UNAVAILABLE
        assert ctext.section_state(incomplete, True, False) == incomplete
        assert ctext.section_state(absent, True, False) == absent

    def test_download_needs_a_pin_and_something_to_fetch(self):
        assert ctext.download_enabled(model_store.STATE_ABSENT)
        assert ctext.download_enabled(model_store.STATE_INCOMPLETE)
        for state in (model_store.STATE_INSTALLED, ctext.STATE_BUNDLED, ctext.STATE_UNAVAILABLE):
            assert not ctext.download_enabled(state)
        assert not ctext.download_enabled(model_store.STATE_ABSENT, job_running=True)

    def test_remove_follows_what_is_on_the_disk(self):
        assert not ctext.remove_enabled(model_store.STATE_ABSENT)
        assert ctext.remove_enabled(model_store.STATE_INCOMPLETE)
        assert ctext.remove_enabled(model_store.STATE_INSTALLED)
        assert not ctext.remove_enabled(model_store.STATE_INSTALLED, job_running=True)

    def test_half_a_megabyte_is_not_zero(self):
        i18n = SimpleNamespace(t=lambda key: ",")
        assert ctext.size_text(i18n, 484_864) == "0,5 MB"


class _PoI18n:
    def __init__(self, locale):
        import polib
        path = os.path.join(os.path.dirname(__file__), "..", "translations", locale,
                            "LC_MESSAGES", "winzapp.po")
        self._t = {e.msgctxt: e.msgstr for e in polib.pofile(path) if e.msgctxt}

    def t(self, key):
        return self._t[key]


def _locales():
    import json
    path = os.path.join(os.path.dirname(__file__), "..", "client", "languages", "language_map.json")
    with open(path, encoding="utf-8") as handle:
        return list(json.load(handle))


@pytest.mark.parametrize("locale", _locales())
def test_every_sentence_is_said_in_every_locale(locale, monkeypatch):
    from core.transcription import errors
    monkeypatch.setattr(comp, "NETEQ", _pinned())
    i18n = _PoI18n(locale)
    for state in ctext.STATE_KEYS:
        text = ctext.state_text(i18n, state)
        assert text and "{" not in text
    assert "MB" in ctext.state_text(i18n, model_store.STATE_ABSENT)
    assert "MB" in ctext.confirmation_text(i18n) and "{" not in ctext.confirmation_text(i18n)
    assert ctext.outcome_text(i18n, "x", None)
    assert ctext.outcome_text(i18n, None, errors.TranscriptionError(errors.CANCELLED, "x"))
    assert ctext.removal_text(i18n, True) and ctext.removal_text(i18n, False)
    for key in ("neteq_component_button", "neteq_component_label", "neteq_component_progress",
                "neteq_component_download_btn", "neteq_component_remove_btn",
                "neteq_component_remove_confirm"):
        assert i18n.t(key)


def test_the_download_job_installs_and_reports_once(tmp_path, monkeypatch):
    from core.call_neteq_component_job import NetEqComponentDownloadJob

    monkeypatch.setattr(comp, "download",
                        lambda root, progress, should_cancel, session: (progress(5, 10), "p")[1])
    done = []
    job = NetEqComponentDownloadJob(root=str(tmp_path), on_finished=lambda r, e: done.append((r, e)))
    job.start()
    job.join(5)
    assert done == [("p", None)]
    assert job.announcement("p", None).i18n_key == "neteq_component_downloaded"


# ── The window's handlers, on a stub (no window is created) ──────────────────


class _Control:
    def __init__(self):
        self.enabled, self.value, self.focused = True, "", False

    def Enable(self, on=True):
        self.enabled = bool(on)

    def Disable(self):
        self.enabled = False

    def IsEnabled(self):
        return self.enabled

    def SetValue(self, value):
        self.value = value

    def SetFocus(self):
        self.focused = True


def _window(started=True, running=False):
    from ui.dialogs.neteq_component_dialog import NetEqComponentDialog

    class _Stub:
        _refresh = NetEqComponentDialog._refresh
        _focus_after = NetEqComponentDialog._focus_after
        _background_busy = NetEqComponentDialog._background_busy
        _on_remove = NetEqComponentDialog._on_remove
        _start_in_background = NetEqComponentDialog._start_in_background
        _background_ended = NetEqComponentDialog._background_ended

    window = _Stub()
    window.said, window.watched = [], []
    window._running = False
    window._field, window._download_btn, window._remove_btn = _Control(), _Control(), _Control()
    window.main_window = SimpleNamespace(
        i18n=SimpleNamespace(t=lambda key: key if key != "decimal_separator" else "."),
        speak_output=SimpleNamespace(output=window.said.append),
        start_transcription_background_job=lambda make_job, status: started,
        watch_transcription_background=window.watched.append,
        transcription_background_running=lambda: running)
    return window


def test_an_unpinned_version_shows_why_and_offers_nothing(monkeypatch):
    from ui.dialogs import neteq_component_dialog as dialog

    monkeypatch.setattr(comp, "installation_state", lambda *a, **k: model_store.STATE_ABSENT)
    monkeypatch.setattr(comp, "is_pinned", lambda *a, **k: False)
    monkeypatch.setattr(call_neteq, "bundled_dll", lambda: None)
    assert dialog.read_states() == (model_store.STATE_ABSENT, ctext.STATE_UNAVAILABLE)
    window = _window()
    window._refresh()
    assert window._field.value == "neteq_component_state_unavailable"
    assert not window._download_btn.enabled and not window._remove_btn.enabled


def test_a_broken_state_read_is_shown_as_absent(monkeypatch):
    from ui.dialogs import neteq_component_dialog as dialog

    def broken(*a, **k):
        raise OSError("gone")

    monkeypatch.setattr(comp, "installation_state", broken)
    monkeypatch.setattr(call_neteq, "bundled_dll", broken)
    monkeypatch.setattr(comp, "is_pinned", lambda *a, **k: True)
    assert dialog.read_states() == (model_store.STATE_ABSENT, model_store.STATE_ABSENT)


@pytest.mark.parametrize("gone,said", [(True, "neteq_component_removed"),
                                       (False, "neteq_component_remove_in_use")])
def test_removal_says_what_happened(monkeypatch, gone, said):
    from ui.dialogs import neteq_component_dialog as dialog

    monkeypatch.setattr(dialog.wx, "MessageBox", lambda *a, **k: dialog.wx.YES)
    monkeypatch.setattr(comp, "remove", lambda *a, **k: gone)
    monkeypatch.setattr(dialog, "read_states",
                        lambda: (model_store.STATE_ABSENT, model_store.STATE_ABSENT))
    window = _window()
    window._on_remove(None)
    assert window.said == [said]
    # The button switched itself off: the focus goes to the state field.
    assert not window._remove_btn.enabled and window._field.focused


def test_removal_waits_for_a_background_download(monkeypatch):
    from ui.dialogs import neteq_component_dialog as dialog

    monkeypatch.setattr(dialog.wx, "MessageBox", lambda *a, **k: pytest.fail("asked"))
    window = _window(running=True)
    window._on_remove(None)
    assert window.said == ["noise_reducer_background_busy"]


def test_a_background_download_is_followed_to_its_end(monkeypatch):
    from ui.dialogs import neteq_component_dialog as dialog

    refreshed = []
    monkeypatch.setattr(dialog, "read_states",
                        lambda: refreshed.append(1) or (model_store.STATE_INSTALLED,) * 2)
    window = _window()
    assert window._start_in_background(lambda *a: None, "status") is True
    assert window.said == [] and not window._download_btn.enabled
    listener = window.watched[0]
    assert listener(None, None, None, False) is False and refreshed == []
    assert listener(None, None, None, True) is False and refreshed == [1]
    refused = _window(started=False)
    assert refused._start_in_background(lambda *a: None, "status") is False
    assert refused.said == ["noise_reducer_background_busy"]
