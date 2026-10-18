"""Using a Whisper model the user already has, without lying about what it is.

A user who already runs faster-whisper from a script of their own has the model
on disk, and part 10 lets WinZapp use that folder instead of downloading the
same 3 GB again. Every failure this file pins costs that user something real,
and each has a specific mechanism:

* **"Looks like large-v3" is not "is large-v3".** A fine-tune of large-v3
  converted the same way has a model.bin of exactly the same size, and a folder
  in the Hugging Face cache is named after a repository and a commit whatever it
  actually holds. Accepting either on sizes or on the folder's name would have
  the automatic choice budget memory for one model and load another, or trust
  weights nobody checked. So a folder becomes "the catalogue's model X" only
  when model.bin hashes to X's pinned sha256, and the tests below build folders
  with the right sizes and the wrong bytes, and snapshots whose name is right
  and whose files are not.

* **A folder of the user's is not WinZapp's to delete.** "Remove" forgets the
  reference and nothing else; a test counts the files afterwards. A reference
  inside WinZapp's own models root is refused, so that removing a WinZapp model
  can never delete what the user was told is "their" folder.

* **An unplugged disk is not a corrupted model.** A reference whose folder is
  gone has its own state and its own error code, instead of "download it
  again" — 3 GB of advice for a problem whose answer is a USB cable.

* **One folder, one reference.** The same folder under another case or through
  a junction is refreshed, not listed twice — canonical_dir(), never abspath.

* **Two accounts can add at the same moment.** The list lives in the shared
  app.json, and a get() followed by a set() loses one of the two additions.

* **With no reference at all, nothing changes.** `model_directory()` is what
  part 10b swaps in for model_store.ensure_ready(); for a user who never pointed
  WinZapp anywhere, it has to answer exactly the same.

No real model is downloaded or loaded: the catalogue is replaced by entries of
a few kilobytes, and the backend's loader by a factory that records its calls.
"""

import gc
import hashlib
import json
import os
import shutil
import threading
import time
import weakref

import pytest

import app_settings
from coord_locks import canonical_dir
from core.transcription import (
    backend as backend_module,
    errors,
    external_models,
    faster_whisper_backend,
    model_catalog,
    model_store,
)
from core.utils import DEFAULT_SETTINGS
from tests.test_transcription_model_store import _link_directory


# ── Synthetic catalogue ──────────────────────────────────────────────────────


def _synthetic_model(model_id, weights, large_shape=False):
    """A catalogue entry of a few kilobytes, plus the bytes behind it.

    `large_shape` gives it the large-v3/turbo file list — vocabulary.json and a
    preprocessor_config.json — because identification must check the files the
    entry actually has, not one assumed shape.
    """
    contents = {
        "config.json": b'{"lang_ids": [50259], "suppress_ids": []}',
        "model.bin": weights,
        "tokenizer.json": b'{"added_tokens": []}',
    }
    if large_shape:
        contents["preprocessor_config.json"] = b'{"feature_size": 128}'
        contents["vocabulary.json"] = b'["one", "two"]'
    else:
        contents["vocabulary.txt"] = b"one\ntwo\nthree\n"
    files = tuple((name, len(data)) for name, data in sorted(contents.items()))
    total = sum(size for _name, size in files)
    model = model_catalog.WhisperModel(
        id=model_id,
        repo=f"Example-Org/faster-whisper-{model_id}",
        revision=model_id.encode().hex().ljust(40, "0")[:40],
        model_bin_sha256=hashlib.sha256(weights).hexdigest(),
        files=files,
        model_bin_bytes=len(weights),
        download_bytes=total,
        disk_bytes=total,
        size_class=model_catalog.SIZE_SMALL,
        min_vram_mb=1024,
        min_ram_mb=2048,
    )
    return model, contents


@pytest.fixture
def catalogue(monkeypatch):
    """Two synthetic entries standing in for the whole catalogue: one of the
    small shape and one of the large shape."""
    alpha = _synthetic_model("alpha", b"A" * 4096)
    gamma = _synthetic_model("gamma", b"G" * 8192, large_shape=True)
    monkeypatch.setattr(model_catalog, "MODELS", (alpha[0], gamma[0]))
    return {"alpha": alpha, "gamma": gamma}


def _write(folder, contents):
    folder = str(folder)
    os.makedirs(folder, exist_ok=True)
    for name, data in contents.items():
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(data)
    return folder


def _same_length_other_bytes(contents):
    """The files of a model with model.bin's bytes changed and its length kept:
    a fine-tune converted the same way, or a silent corruption."""
    altered = dict(contents)
    weights = contents["model.bin"]
    altered["model.bin"] = bytes([weights[0] ^ 0xFF]) + weights[1:]
    return altered


def _hf_snapshot(cache, model, contents, repo=None, revision=None, link=False):
    """A snapshot laid out the way huggingface_hub lays one out.

    With `link`, every file is a relative symbolic link into ``blobs/``, as on
    any machine that allows them. Without, the files sit in the snapshot
    itself — what huggingface_hub does on a Windows account that lacks the
    symlink privilege (it moves the blob into the snapshot), and therefore the
    layout this module meets most often on its own platform.
    """
    repo = repo or model.repo
    revision = revision or model.revision
    repo_dir = os.path.join(str(cache), "models--" + repo.replace("/", "--"))
    snapshot = os.path.join(repo_dir, "snapshots", revision)
    blobs = os.path.join(repo_dir, "blobs")
    os.makedirs(snapshot, exist_ok=True)
    os.makedirs(blobs, exist_ok=True)
    os.makedirs(os.path.join(repo_dir, "refs"), exist_ok=True)
    with open(os.path.join(repo_dir, "refs", "main"), "w") as fh:
        fh.write(revision)
    for name, data in contents.items():
        if not link:
            with open(os.path.join(snapshot, name), "wb") as fh:
                fh.write(data)
            continue
        blob = os.path.join(blobs, hashlib.sha256(data).hexdigest())
        with open(blob, "wb") as fh:
            fh.write(data)
        os.symlink(os.path.relpath(blob, snapshot), os.path.join(snapshot, name))
    return snapshot


def _symlinks_or_skip(tmp_path):
    """Skip unless this account may create file symbolic links.

    An ordinary Windows account may not (SeCreateSymbolicLinkPrivilege), and
    unlike directories there is no junction to fall back on for a file. CI runs
    as an administrator, so the symlinked layout is exercised there; the copied
    layout — which is what huggingface_hub itself produces on such an account —
    is exercised everywhere.
    """
    probe_target = tmp_path / "probe-target"
    probe_target.write_bytes(b"x")
    try:
        os.symlink(str(probe_target), str(tmp_path / "probe-link"))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this account cannot create symbolic links: {exc}")


def _names_in(folder):
    return sorted(os.listdir(folder))


def _rewrite_weights_same_size(folder):
    """What `ct2-transformers-converter --force` into the same folder does with
    a fine-tune: model.bin rewritten with other bytes, at exactly its size.

    The mtime is then moved on by a second, explicitly. A converter that ran
    for minutes cannot land in the same tick of the file system's clock as the
    check before it; a test that rewrites the file a millisecond later can, and
    would then be testing the clock rather than the code.
    """
    weights = os.path.join(str(folder), "model.bin")
    before = os.stat(weights)
    with open(weights, "rb") as fh:
        data = fh.read()
    with open(weights, "wb") as fh:
        fh.write(bytes([data[0] ^ 0xFF]) + data[1:])
    os.utime(weights, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    assert os.path.getsize(weights) == before.st_size


@pytest.fixture
def settings(tmp_path):
    return app_settings.AppSettings(str(tmp_path / "global"))


@pytest.fixture
def models_root(tmp_path):
    return str(tmp_path / "winzapp_models")


# ── A backend whose loader only records ──────────────────────────────────────


class _Loaded:
    """What the fake loader hands back: a model that is never used."""


class _Factory:
    """Stands in for faster_whisper.WhisperModel.

    Keeps a weak reference to each model it built, never a strong one — the
    test that the trial lets the model go depends on nothing here holding it.
    """

    def __init__(self, error=None):
        self.error = error
        self.loads = []
        self.built = []

    def __call__(self, path, **kwargs):
        self.loads.append({"path": path, **kwargs})
        if self.error is not None:
            raise self.error
        model = _Loaded()
        self.built.append(weakref.ref(model))
        return model


def _backend(factory):
    backend = faster_whisper_backend.FasterWhisperBackend(model_factory=factory)
    # is_available() looks for the faster_whisper package on this machine; these
    # tests hand the backend their own model factory, so it is not needed.
    backend.is_available = lambda: True
    return backend


# ── Tests ────────────────────────────────────────────────────────────────────


class TestTheShapeOfAModelFolder:
    def test_a_complete_folder_is_accepted_with_its_sizes(self, catalogue, tmp_path):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)

        shape = external_models.inspect_folder(folder)

        assert shape.ok and shape.refusal is None
        assert dict(shape.sizes)["model.bin"] == len(contents["model.bin"])

    @pytest.mark.parametrize("name", ["config.json", "model.bin", "tokenizer.json"])
    def test_each_required_file_is_named_when_missing(self, catalogue, tmp_path, name):
        """tokenizer.json above all: without it faster-whisper downloads one
        from Hugging Face whatever local_files_only says."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        os.remove(os.path.join(folder, name))

        shape = external_models.inspect_folder(folder)

        assert shape.refusal == external_models.REFUSED_FILES_MISSING
        assert shape.missing == (name,)

    def test_either_vocabulary_will_do_and_neither_will_not(self, catalogue, tmp_path):
        _model, small = catalogue["alpha"]
        _model, large = catalogue["gamma"]
        assert external_models.inspect_folder(_write(tmp_path / "txt", small)).ok
        assert external_models.inspect_folder(_write(tmp_path / "json", large)).ok

        folder = _write(tmp_path / "none", small)
        os.remove(os.path.join(folder, "vocabulary.txt"))
        shape = external_models.inspect_folder(folder)
        assert shape.missing == (external_models.VOCABULARY_PATTERN,)

    def test_an_empty_weights_file_is_missing(self, catalogue, tmp_path):
        """What an interrupted copy leaves behind under the final name."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", dict(contents, **{"model.bin": b""}))
        assert external_models.inspect_folder(folder).missing == ("model.bin",)

    @pytest.mark.parametrize("config", [b"{ not json", b"[1, 2]", b"\xff\xfe"])
    def test_a_config_that_is_not_a_json_object_is_refused(self, catalogue, tmp_path,
                                                            config):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", dict(contents, **{"config.json": config}))
        shape = external_models.inspect_folder(folder)
        assert shape.refusal == external_models.REFUSED_BAD_CONFIG

    def test_the_preprocessor_config_is_not_required_for_the_shape(self, catalogue,
                                                                    tmp_path):
        """Only some models ship it; for a catalogue model it is checked by
        identification, against that entry's own file list."""
        _model, contents = catalogue["gamma"]
        trimmed = {k: v for k, v in contents.items() if k != "preprocessor_config.json"}
        assert external_models.inspect_folder(_write(tmp_path / "mine", trimmed)).ok

    def test_a_folder_that_is_not_there_says_so(self, tmp_path):
        shape = external_models.inspect_folder(tmp_path / "gone")
        assert shape.refusal == external_models.REFUSED_FOLDER_MISSING

    def test_a_file_is_not_a_folder(self, tmp_path):
        (tmp_path / "model.bin").write_bytes(b"x")
        shape = external_models.inspect_folder(tmp_path / "model.bin")
        assert shape.refusal == external_models.REFUSED_NOT_A_FOLDER

    def test_every_refusal_is_a_known_code(self):
        assert len(set(external_models.REFUSAL_CODES)) == len(
            external_models.REFUSAL_CODES)


class TestTheHuggingFaceCachePath:
    def test_a_snapshot_names_its_repository_and_commit(self, tmp_path):
        path = tmp_path / "hub" / "models--Systran--faster-whisper-small" / "snapshots" / "abc123"
        assert external_models.hf_cache_coordinates(path) == (
            "Systran/faster-whisper-small", "abc123")

    @pytest.mark.parametrize("parts", [
        ("hub", "models--Systran--faster-whisper-small"),
        ("hub", "models--Systran--faster-whisper-small", "blobs", "abc123"),
        ("hub", "datasets--someone--audio", "snapshots", "abc123"),
        ("hub", "models--", "snapshots", "abc123"),
        ("somewhere", "whisper", "large-v3"),
    ])
    def test_anything_else_is_not_a_snapshot(self, tmp_path, parts):
        assert external_models.hf_cache_coordinates(tmp_path.joinpath(*parts)) is None

    def test_the_repository_folder_stands_for_its_snapshots(self, tmp_path):
        """What somebody browsing the cache actually stops at."""
        repo = tmp_path / "models--Systran--faster-whisper-small"
        (repo / "snapshots" / "one").mkdir(parents=True)
        (repo / "snapshots" / "two").mkdir()
        assert external_models.candidate_folders(repo) == (
            str(repo / "snapshots" / "one"), str(repo / "snapshots" / "two"))
        assert external_models.candidate_folders(tmp_path) == (str(tmp_path),)


class TestIdentificationWithoutReadingTheWeights:
    def test_a_cache_snapshot_of_a_catalogue_entry_is_a_candidate(self, catalogue,
                                                                  tmp_path):
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model, contents)

        found = external_models.identify_quick(snapshot)

        assert (found.match, found.model_id, found.via) == (
            external_models.MATCH_CANDIDATE, "gamma", external_models.VIA_HF_CACHE)

    def test_the_repository_is_compared_without_case(self, catalogue, tmp_path):
        """The cache keeps whatever case the downloading script typed."""
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model, contents,
                                repo=model.repo.lower())
        assert external_models.identify_quick(snapshot).via == (
            external_models.VIA_HF_CACHE)

    def test_the_right_name_with_a_wrong_size_is_not_a_candidate(self, catalogue,
                                                                 tmp_path):
        """The folder's name is a claim, not evidence. An auxiliary file of the
        wrong size — a tokenizer from another revision — leaves nothing to
        identify it by."""
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(
            tmp_path / "hub", model,
            dict(contents, **{"tokenizer.json": contents["tokenizer.json"] + b" "}),
        )
        found = external_models.identify_quick(snapshot)
        assert found.match == external_models.MATCH_NONE
        assert found.model_id is None

    def test_the_right_name_with_other_weights_is_not_a_candidate(self, catalogue,
                                                                  tmp_path):
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(
            tmp_path / "hub", model,
            dict(contents, **{"model.bin": contents["model.bin"] + b"x"}),
        )
        assert external_models.identify_quick(snapshot).match == (
            external_models.MATCH_NONE)

    def test_a_missing_preprocessor_config_leaves_it_unidentified(self, catalogue,
                                                                  tmp_path):
        """The catalogue entry has one, so a copy without it is not that entry
        — and a large-v3 loaded without it gets 80 mel bins instead of 128."""
        model, contents = catalogue["gamma"]
        trimmed = {k: v for k, v in contents.items() if k != "preprocessor_config.json"}
        snapshot = _hf_snapshot(tmp_path / "hub", model, trimmed)
        assert external_models.identify_quick(snapshot).match == (
            external_models.MATCH_NONE)

    def test_a_newer_revision_of_the_same_repository_is_not_the_catalogue_entry(
        self, catalogue, tmp_path
    ):
        """The catalogue pins a commit. The same files under another commit are
        still found — by their sizes — but not by the shortcut."""
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model, contents, revision="f" * 40)
        found = external_models.identify_quick(snapshot)
        assert (found.match, found.via) == (
            external_models.MATCH_CANDIDATE, external_models.VIA_SIZES)

    def test_any_folder_with_every_catalogued_size_is_a_candidate(self, catalogue,
                                                                  tmp_path):
        model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "my scripts" / "whisper-tiny", contents)
        found = external_models.identify_quick(folder)
        assert (found.match, found.model_id, found.via) == (
            external_models.MATCH_CANDIDATE, "alpha", external_models.VIA_SIZES)

    def test_the_weights_size_alone_is_not_enough(self, catalogue, tmp_path):
        model, contents = catalogue["alpha"]
        folder = _write(
            tmp_path / "mine",
            dict(contents, **{"vocabulary.txt": b"a different vocabulary\n"}),
        )
        assert external_models.identify_quick(folder).match == (
            external_models.MATCH_NONE)

    def test_a_valid_folder_nobody_claims_is_a_custom_model(self, catalogue, tmp_path):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", dict(contents, **{"model.bin": b"C" * 5000}))
        found = external_models.identify_quick(folder)
        assert found.match == external_models.MATCH_NONE
        assert found.shape.ok

    def test_it_never_reads_the_weights(self, catalogue, tmp_path, monkeypatch):
        model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        monkeypatch.setattr(
            external_models, "_hash_file",
            lambda *a, **k: pytest.fail("identify_quick() read model.bin"),
        )
        assert external_models.identify_quick(folder).match == (
            external_models.MATCH_CANDIDATE)


class TestTheCacheOnDisk:
    def test_copied_files_are_measured_where_they_are(self, catalogue, tmp_path):
        """huggingface_hub without the symlink privilege: the files sit in the
        snapshot and blobs/ holds nothing for them."""
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model, contents)
        assert os.listdir(os.path.join(os.path.dirname(os.path.dirname(snapshot)),
                                       "blobs")) == []
        assert external_models.identify(snapshot).match == (
            external_models.MATCH_VERIFIED)

    def test_symlinked_files_are_measured_at_their_blobs(self, catalogue, tmp_path):
        """The link itself is a few dozen bytes; the blob is the file."""
        _symlinks_or_skip(tmp_path)
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model, contents, link=True)
        assert os.path.islink(os.path.join(snapshot, "model.bin"))

        assert external_models.identify_quick(snapshot).via == (
            external_models.VIA_HF_CACHE)
        assert external_models.identify(snapshot).match == (
            external_models.MATCH_VERIFIED)

    def test_a_name_is_measured_where_it_resolves(self, catalogue, tmp_path,
                                                  monkeypatch):
        """The same property as the test above, on an account that cannot
        create a link: the snapshot entry is left as a stub the size of a link
        and realpath is made to say it leads to the blob — which is all a link
        is, as far as measuring goes. Measuring the entry itself (lstat) reads
        the stub and finds nothing to identify."""
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model, contents)
        entry = os.path.join(snapshot, "model.bin")
        blob = os.path.join(os.path.dirname(os.path.dirname(snapshot)), "blobs",
                            model.model_bin_sha256)
        os.replace(entry, blob)
        with open(entry, "wb") as fh:
            fh.write(os.path.relpath(blob, snapshot).encode())
        real_realpath = os.path.realpath

        def realpath(path, *args, **kwargs):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(entry):
                return blob
            return real_realpath(path, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", realpath)

        assert external_models.identify_quick(snapshot).via == (
            external_models.VIA_HF_CACHE)

    def test_a_link_whose_blob_is_gone_is_a_missing_file(self, catalogue, tmp_path):
        _symlinks_or_skip(tmp_path)
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model, contents, link=True)
        os.remove(os.path.realpath(os.path.join(snapshot, "model.bin")))

        shape = external_models.inspect_folder(snapshot)

        assert shape.missing == ("model.bin",)


class TestVerifyingTheWeights:
    def test_the_digest_is_what_verifies_it(self, catalogue, tmp_path):
        model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        found = external_models.identify(folder)
        assert (found.match, found.model_id) == (external_models.MATCH_VERIFIED, "alpha")

    def test_the_right_sizes_and_other_weights_are_not_the_model(self, catalogue,
                                                                  tmp_path):
        """A fine-tune converted the same way, or a silent corruption: every
        size is the catalogue's and the model is not."""
        model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", _same_length_other_bytes(contents))
        assert external_models.identify_quick(folder).match == (
            external_models.MATCH_CANDIDATE)
        found = external_models.identify(folder)
        assert (found.match, found.model_id) == (
            external_models.MATCH_DIGEST_MISMATCH, "alpha")

    def test_a_cache_snapshot_is_hashed_too(self, catalogue, tmp_path):
        """The shortcut picks the candidate; it does not skip the digest."""
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model,
                                _same_length_other_bytes(contents))
        assert external_models.identify(snapshot).match == (
            external_models.MATCH_DIGEST_MISMATCH)

    def test_progress_counts_the_weights_and_ends_at_their_size(self, catalogue,
                                                                tmp_path, monkeypatch):
        monkeypatch.setattr(external_models, "_CHUNK_BYTES", 1000)
        model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        calls = []

        external_models.identify(folder, progress=lambda d, t: calls.append((d, t)))

        assert len(calls) == 5
        assert calls[-1] == (model.model_bin_bytes, model.model_bin_bytes)
        assert [done for done, _t in calls] == sorted(done for done, _t in calls)

    def test_it_can_be_cancelled_half_way(self, catalogue, tmp_path, monkeypatch):
        monkeypatch.setattr(external_models, "_CHUNK_BYTES", 1000)
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        seen = []

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.identify(
                folder,
                progress=lambda done, _t: seen.append(done),
                should_cancel=lambda: len(seen) >= 2,
            )

        assert caught.value.code == errors.CANCELLED
        assert seen == [1000, 2000]

    def test_a_folder_that_vanishes_before_the_hash_is_missing_not_damaged(
        self, catalogue, tmp_path, monkeypatch
    ):
        """The disk unplugged between the quick look and the long read."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "usb" / "whisper", contents)
        real_quick = external_models.identify_quick

        def quick_then_unplug(path):
            found = real_quick(path)
            shutil.rmtree(tmp_path / "usb")
            return found

        monkeypatch.setattr(external_models, "identify_quick", quick_then_unplug)

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.identify(folder)

        assert caught.value.code == errors.EXTERNAL_MODEL_MISSING


class TestTheTrialLoad:
    @pytest.fixture(autouse=True)
    def _cuda_runtime(self, monkeypatch):
        # These tests inject a model factory; no actual CUDA DLL is required.
        monkeypatch.setattr(faster_whisper_backend.device, "prepare_cuda_libraries",
                            lambda: (True, (), None))

    def test_the_backend_opens_it_offline_and_lets_it_go(self, catalogue, tmp_path):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "custom", dict(contents, **{"model.bin": b"C" * 99}))
        factory = _Factory()
        backend = _backend(factory)

        external_models.trial_load(folder, backend, "cpu", "int8")

        assert factory.loads == [{
            "path": folder, "device": "cpu", "compute_type": "int8",
            "local_files_only": True,
        }]
        gc.collect()
        assert [ref() for ref in factory.built] == [None]

    def test_the_trial_does_not_touch_the_cached_model(self, catalogue, tmp_path):
        """The model the user is transcribing with stays loaded, and the
        trial's model never takes its place."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "custom", contents)
        backend = _backend(_Factory())
        in_use = object()
        backend._model, backend._key = in_use, ("elsewhere", "cuda", "float16")

        external_models.trial_load(folder, backend, "cuda", "float16")

        assert backend._model is in_use
        assert backend._key == ("elsewhere", "cuda", "float16")

    def test_a_load_failure_is_classified_like_any_other(self, catalogue, tmp_path):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "custom", contents)
        backend = _backend(_Factory(error=RuntimeError("CUDA failed with error out of memory")))

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.trial_load(folder, backend, "cuda", "float16")

        assert caught.value.code == errors.INSUFFICIENT_VRAM

    def test_a_folder_that_is_not_there_is_never_handed_to_the_backend(self, tmp_path):
        factory = _Factory()
        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.trial_load(tmp_path / "gone", _backend(factory), "cpu", "int8")
        assert caught.value.code == errors.EXTERNAL_MODEL_MISSING
        assert factory.loads == []

    def test_a_folder_without_a_tokenizer_is_never_handed_to_the_backend(
        self, catalogue, tmp_path
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "custom", contents)
        os.remove(os.path.join(folder, "tokenizer.json"))
        factory = _Factory()

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.trial_load(folder, _backend(factory), "cpu", "int8")

        assert caught.value.code == errors.MODEL_CORRUPTED
        assert factory.loads == []

    def test_the_backend_itself_refuses_a_folder_without_a_tokenizer(self, tmp_path):
        """Its own guard, not only external_models': faster-whisper answers a
        missing tokenizer.json by downloading one."""
        factory = _Factory()
        with pytest.raises(errors.TranscriptionError) as caught:
            _backend(factory).trial_load(str(tmp_path), "cpu", "int8")
        assert caught.value.code == errors.MODEL_CORRUPTED
        assert factory.loads == []

    def test_a_backend_that_cannot_trial_load_says_so(self):
        with pytest.raises(NotImplementedError):
            backend_module.TranscriptionBackend().trial_load("x", "cpu", "int8")


class TestAcceptingACatalogueFolder:
    def test_a_verified_folder_is_remembered_as_that_model(self, catalogue, tmp_path,
                                                           settings, models_root):
        model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)

        outcome = external_models.accept_catalogue_folder(settings, folder, models_root)

        assert outcome.code == external_models.ACCEPT_ADDED
        stored = external_models.load_references(
            app_settings.AppSettings(settings.global_dir))
        assert [(r.path, r.key, r.model_id, r.verified) for r in stored] == [
            (folder, canonical_dir(folder), "alpha", True)]
        assert stored[0].weights_mark == (
            model.model_bin_bytes, os.stat(os.path.join(folder, "model.bin")).st_mtime_ns)

    def test_the_right_sizes_are_not_enough_to_be_remembered(self, catalogue, tmp_path,
                                                             settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", _same_length_other_bytes(contents))

        outcome = external_models.accept_catalogue_folder(settings, folder, models_root)

        assert outcome.code == external_models.ACCEPT_DIGEST_MISMATCH
        assert external_models.load_references(settings) == ()

    def test_a_cache_snapshot_is_not_remembered_on_its_name(self, catalogue, tmp_path,
                                                            settings, models_root):
        model, contents = catalogue["gamma"]
        snapshot = _hf_snapshot(tmp_path / "hub", model,
                                _same_length_other_bytes(contents))
        outcome = external_models.accept_catalogue_folder(settings, snapshot, models_root)
        assert outcome.code == external_models.ACCEPT_DIGEST_MISMATCH
        assert external_models.load_references(settings) == ()

    def test_a_folder_nobody_claims_is_left_for_the_custom_route(self, catalogue,
                                                                 tmp_path, settings,
                                                                 models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", dict(contents, **{"model.bin": b"C" * 5000}))
        outcome = external_models.accept_catalogue_folder(settings, folder, models_root)
        assert outcome.code == external_models.ACCEPT_NOT_IDENTIFIED
        assert external_models.load_references(settings) == ()

    def test_a_cancelled_verification_remembers_nothing(self, catalogue, tmp_path,
                                                        settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.accept_catalogue_folder(
                settings, folder, models_root, should_cancel=lambda: True)
        assert caught.value.code == errors.CANCELLED
        assert external_models.load_references(settings) == ()

    def test_a_folder_that_is_not_a_model_is_refused_with_its_reason(
        self, tmp_path, settings, models_root
    ):
        outcome = external_models.accept_catalogue_folder(
            settings, tmp_path / "gone", models_root)
        assert outcome.code == external_models.REFUSED_FOLDER_MISSING

    def test_a_later_mismatch_stops_a_stored_folder_counting_as_the_model(
        self, catalogue, tmp_path, settings, models_root
    ):
        """The folder is kept, so the user sees it, but unverified — and so no
        longer loaded as the catalogue's model."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        external_models.accept_catalogue_folder(settings, folder, models_root)
        _write(folder, _same_length_other_bytes(contents))

        external_models.accept_catalogue_folder(settings, folder, models_root)

        (reference,) = external_models.load_references(settings)
        assert reference.verified is False
        assert external_models.reference_state(reference) == external_models.REF_UNVERIFIED


class TestAcceptingACustomFolder:
    def test_a_folder_the_backend_opened_is_remembered_as_custom(self, catalogue,
                                                                 tmp_path, settings,
                                                                 models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", dict(contents, **{"model.bin": b"C" * 99}))

        outcome = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8")

        assert outcome.code == external_models.ACCEPT_ADDED
        (reference,) = external_models.load_references(settings)
        assert (reference.model_id, reference.verified, reference.weights_mark[0]) == (
            None, True, 99)
        assert reference.is_custom

    def test_a_failed_trial_remembers_nothing(self, catalogue, tmp_path, settings,
                                              models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", contents)
        backend = _backend(_Factory(error=RuntimeError("unsupported model spec")))

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.accept_custom_folder(
                settings, folder, models_root, backend, "cpu", "int8")

        assert caught.value.code == errors.BACKEND_ERROR
        assert external_models.load_references(settings) == ()

    def test_a_cancel_during_the_load_remembers_nothing(self, catalogue, tmp_path,
                                                        settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", contents)
        factory = _Factory()

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.accept_custom_folder(
                settings, folder, models_root, _backend(factory), "cpu", "int8",
                should_cancel=lambda: bool(factory.loads))

        assert caught.value.code == errors.CANCELLED
        assert external_models.load_references(settings) == ()

    def test_a_digest_mismatch_leaves_a_custom_choice_alone(self, catalogue, tmp_path,
                                                            settings, models_root):
        """The user kept it as custom; it was never claimed to be the
        catalogue's model, so failing that claim changes nothing."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", _same_length_other_bytes(contents))
        external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8")

        external_models.accept_catalogue_folder(settings, folder, models_root)

        (reference,) = external_models.load_references(settings)
        assert (reference.model_id, reference.verified) == (None, True)


class TestOneFolderOneReference:
    def test_a_folder_added_twice_is_refreshed_not_duplicated(self, catalogue, tmp_path,
                                                              settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        first = external_models.accept_catalogue_folder(settings, folder, models_root)
        second = external_models.accept_catalogue_folder(settings, folder, models_root)

        assert second.code == external_models.ACCEPT_UPDATED
        assert second.reference.id == first.reference.id
        assert len(external_models.load_references(settings)) == 1

    @pytest.mark.skipif(
        os.path.normcase("A") != os.path.normcase("a"),
        reason="two spellings are one folder only on a case-folding filesystem",
    )
    def test_another_case_is_the_same_folder(self, catalogue, tmp_path, settings,
                                             models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "Whisper_Mine", contents)
        external_models.accept_catalogue_folder(settings, folder, models_root)
        outcome = external_models.accept_catalogue_folder(
            settings, str(tmp_path / "whisper_mine"), models_root)
        assert outcome.code == external_models.ACCEPT_UPDATED
        assert len(external_models.load_references(settings)) == 1

    def test_a_junction_to_the_same_folder_is_the_same_folder(self, catalogue, tmp_path,
                                                              settings, models_root):
        """What somebody does to reach a folder on another disk from a short
        path. abspath says two folders; canonical_dir says one."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "real", contents)
        link = str(tmp_path / "linked")
        created, detail = _link_directory(folder, link)
        assert created, f"could not create a directory link: {detail}"

        external_models.accept_catalogue_folder(settings, folder, models_root)
        outcome = external_models.accept_catalogue_folder(settings, link, models_root)

        assert outcome.code == external_models.ACCEPT_UPDATED
        assert len(external_models.load_references(settings)) == 1

    def test_winzapps_own_model_folder_is_not_an_external_one(self, catalogue, settings,
                                                              models_root):
        """remove_model() on the root would delete what the user was told is
        theirs — so a model already in the root cannot be referenced."""
        model, contents = catalogue["alpha"]
        folder = _write(model_store.model_dir(models_root, model.id), contents)

        outcome = external_models.accept_catalogue_folder(settings, folder, models_root)

        assert outcome.code == external_models.REFUSED_INSIDE_MODELS_ROOT
        assert external_models.load_references(settings) == ()

    def test_anything_under_the_root_is_refused_too(self, catalogue, settings,
                                                    models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(os.path.join(models_root, "my own", "copy"), contents)
        outcome = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8")
        assert outcome.code == external_models.REFUSED_INSIDE_MODELS_ROOT

    def test_a_folder_under_a_root_chosen_but_not_applied_is_refused(
        self, catalogue, tmp_path, settings, models_root
    ):
        """Browse to X, add X/small, press OK: OK moves WinZapp's models into
        X, and the user's X/small is then where "Remove" deletes."""
        _model, contents = catalogue["alpha"]
        pending = str(tmp_path / "chosen")
        folder = _write(os.path.join(pending, "small"), contents)
        outcome = external_models.accept_catalogue_folder(
            settings, folder, models_root, other_roots=(pending,))
        assert outcome.code == external_models.REFUSED_INSIDE_MODELS_ROOT
        outcome = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8",
            other_roots=(pending,))
        assert outcome.code == external_models.REFUSED_INSIDE_MODELS_ROOT
        assert external_models.load_references(settings) == ()

    def test_a_pending_root_elsewhere_refuses_nothing(
        self, catalogue, tmp_path, settings, models_root
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        outcome = external_models.accept_catalogue_folder(
            settings, folder, models_root, other_roots=(str(tmp_path / "chosen"),))
        assert outcome.code == external_models.ACCEPT_ADDED

    @pytest.mark.skipif(
        os.path.normcase("A") != os.path.normcase("a"),
        reason="two spellings are one folder only on a case-folding filesystem",
    )
    def test_the_root_under_another_spelling_is_still_the_root(self, catalogue, tmp_path,
                                                               settings):
        model, contents = catalogue["alpha"]
        root = str(tmp_path / "WinZapp_Models")
        folder = _write(model_store.model_dir(root, model.id), contents)
        outcome = external_models.accept_catalogue_folder(
            settings, folder, str(tmp_path / "winzapp_models"))
        assert outcome.code == external_models.REFUSED_INSIDE_MODELS_ROOT

    def test_a_junction_that_leads_into_the_root_is_the_root(self, catalogue, tmp_path,
                                                             settings, models_root):
        """A short path outside the root that is really a folder inside it.
        abspath sees a folder elsewhere; remove_model() would still delete it."""
        model, contents = catalogue["alpha"]
        inside = _write(model_store.model_dir(models_root, model.id), contents)
        link = str(tmp_path / "looks-external")
        created, detail = _link_directory(inside, link)
        assert created, f"could not create a directory link: {detail}"

        outcome = external_models.accept_catalogue_folder(settings, link, models_root)

        assert outcome.code == external_models.REFUSED_INSIDE_MODELS_ROOT
        assert external_models.load_references(settings) == ()

    @pytest.mark.parametrize("stored_root", ["", None])
    def test_an_empty_root_is_the_default_root_not_no_root(self, catalogue, tmp_path,
                                                           settings, monkeypatch,
                                                           stored_root):
        """app.json stores "" for "the default models folder". Read as "there
        is no root", it let a model inside the default root be referenced."""
        model, contents = catalogue["alpha"]
        default_root = str(tmp_path / "default_models")
        monkeypatch.setattr(model_store, "default_models_dir", lambda: default_root)
        inside = _write(model_store.model_dir(default_root, model.id), contents)

        outcome = external_models.accept_catalogue_folder(settings, inside, stored_root)

        assert outcome.code == external_models.REFUSED_INSIDE_MODELS_ROOT
        assert external_models.load_references(settings) == ()

    def test_the_app_json_lock_never_waits_on_a_folder(self, catalogue, tmp_path,
                                                       settings, models_root,
                                                       monkeypatch):
        """Inside the lock, stored folders are compared by the key kept at
        accept time. A realpath of each there would hold app.json through an
        SMB timeout for a reference on a share that is down."""
        _model, contents = catalogue["alpha"]
        first = _write(tmp_path / "first", contents)
        external_models.accept_catalogue_folder(settings, first, models_root)
        locked = []
        real_update = settings.update

        def update(key, change):
            def inside_the_lock(current):
                locked.append(True)
                try:
                    return change(current)
                finally:
                    locked.pop()
            return real_update(key, inside_the_lock)

        real_canonical = external_models.canonical_dir

        def canonical(path):
            assert not locked, "canonical_dir() ran with app.json locked"
            return real_canonical(path)

        monkeypatch.setattr(settings, "update", update)
        monkeypatch.setattr(external_models, "canonical_dir", canonical)

        external_models.accept_catalogue_folder(
            settings, _write(tmp_path / "second", contents), models_root)
        again = external_models.accept_catalogue_folder(settings, first, models_root)

        assert again.code == external_models.ACCEPT_UPDATED
        assert len(external_models.load_references(settings)) == 2

    def test_a_record_without_a_key_is_matched_by_its_own_spelling(
        self, catalogue, tmp_path, settings, models_root
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        settings.set(external_models.EXTERNAL_MODELS_SETTING, [
            {"id": "old", "path": folder, "model_id": "alpha", "verified": True}])

        outcome = external_models.accept_catalogue_folder(settings, folder, models_root)

        assert (outcome.code, outcome.reference.id) == (
            external_models.ACCEPT_UPDATED, "old")
        (reference,) = external_models.load_references(settings)
        assert reference.key == canonical_dir(folder)

    def test_a_referenced_folder_is_found_under_any_spelling(self, catalogue, tmp_path,
                                                             settings, models_root):
        """What discovery marks as "already in use": the same rule as the
        duplicate check, through a junction too."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "real", contents)
        link = str(tmp_path / "linked")
        created, detail = _link_directory(folder, link)
        assert created, f"could not create a directory link: {detail}"
        stored = external_models.accept_catalogue_folder(
            settings, folder, models_root).reference
        references = external_models.load_references(settings)

        assert external_models.reference_for_path(references, link) == stored
        assert external_models.reference_for_path(references, folder) == stored
        assert external_models.reference_for_path(references, tmp_path / "other") is None

    def test_two_accounts_adding_at_once_both_land(self, catalogue, tmp_path,
                                                   models_root, monkeypatch):
        """Two processes, one app.json. The reference id is minted inside the
        locked step, so slowing it down holds that step open — which is exactly
        the window a get()-then-set() would lose one of the two in."""
        _model, contents = catalogue["alpha"]
        global_dir = str(tmp_path / "global")
        real_new_id = external_models._new_reference_id

        def slow_new_id():
            time.sleep(0.05)
            return real_new_id()

        monkeypatch.setattr(external_models, "_new_reference_id", slow_new_id)
        folders = [
            _write(tmp_path / f"custom-{n}", dict(contents, **{"model.bin": b"C" * (50 + n)}))
            for n in range(6)
        ]
        failures = []

        def add(folder):
            try:
                external_models.accept_custom_folder(
                    app_settings.AppSettings(global_dir), folder, models_root,
                    _backend(_Factory()), "cpu", "int8")
            except Exception as exc:  # pragma: no cover - reported below
                failures.append(exc)

        threads = [threading.Thread(target=add, args=(folder,)) for folder in folders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert failures == []
        stored = external_models.load_references(app_settings.AppSettings(global_dir))
        assert sorted(r.path for r in stored) == sorted(folders)
        assert len({r.id for r in stored}) == len(folders)


class TestForgettingAReference:
    def test_forgetting_leaves_every_file_where_it_was(self, catalogue, tmp_path,
                                                       settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        (tmp_path / "mine" / "notes of mine.txt").write_text("mine")
        before = _names_in(folder)
        outcome = external_models.accept_catalogue_folder(settings, folder, models_root)

        assert external_models.forget_reference(settings, outcome.reference.id) is True

        assert external_models.load_references(settings) == ()
        assert _names_in(folder) == before
        assert external_models.identify(folder).match == external_models.MATCH_VERIFIED

    def test_forgetting_one_keeps_the_others(self, catalogue, tmp_path, settings,
                                             models_root):
        _model, contents = catalogue["alpha"]
        kept = _write(tmp_path / "kept", contents)
        gone = _write(tmp_path / "gone", contents)
        external_models.accept_catalogue_folder(settings, kept, models_root)
        dropped = external_models.accept_catalogue_folder(settings, gone, models_root)

        external_models.forget_reference(settings, dropped.reference.id)

        assert [r.path for r in external_models.load_references(settings)] == [kept]

    def test_forgetting_what_is_not_there_says_so(self, settings):
        assert external_models.forget_reference(settings, "nothing") is False

    def test_a_folder_that_is_gone_can_still_be_forgotten(self, catalogue, tmp_path,
                                                          settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "usb", contents)
        outcome = external_models.accept_catalogue_folder(settings, folder, models_root)
        shutil.rmtree(folder)
        assert external_models.forget_reference(settings, outcome.reference.id) is True


class TestWhatIsStored:
    def test_the_setting_is_install_wide_under_the_mirrored_name(self, settings):
        assert settings.get(external_models.EXTERNAL_MODELS_SETTING) == []
        assert external_models.EXTERNAL_MODELS_SETTING in app_settings._DEFAULTS

    def test_no_account_carries_its_own_list(self):
        """The files belong to the machine: an account seeing a model its
        sibling cannot is not a state that means anything."""
        section = DEFAULT_SETTINGS.get("transcription", {})
        assert external_models.EXTERNAL_MODELS_SETTING not in section
        assert "external_models" not in section

    def test_it_is_plain_json(self, catalogue, tmp_path, settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        external_models.accept_catalogue_folder(settings, folder, models_root)
        with open(os.path.join(settings.global_dir, "app.json"), encoding="utf-8") as fh:
            (entry,) = json.load(fh)[external_models.EXTERNAL_MODELS_SETTING]
        assert set(entry) == {
            "id", "path", "key", "model_id", "verified", "weights_mark", "backend"
        }
        assert entry["backend"] == backend_module.BACKEND_FASTER_WHISPER
        assert set(entry["weights_mark"]) == {"size", "mtime_ns"}
        assert entry["path"] == folder

    def test_nonsense_in_the_file_is_dropped_not_raised(self, settings):
        settings.set(external_models.EXTERNAL_MODELS_SETTING, [
            "not a dict",
            {"path": "C:\\no id"},
            {"id": "", "path": "C:\\empty id"},
            {"id": "a", "path": "C:\\kept", "verified": "yes",
             "weights_mark": {"size": True, "mtime_ns": 1}},
            {"id": "a", "path": "C:\\same id again"},
        ])
        (reference,) = external_models.load_references(settings)
        assert (reference.id, reference.path) == ("a", "C:\\kept")
        # Only a literal true is trusted: a hand edit must not vouch for a folder.
        assert reference.verified is False
        assert reference.weights_mark is None

    @pytest.mark.parametrize("mark", [
        None, "123", [4096, 1], {"size": 4096}, {"size": 4096, "mtime_ns": "1"},
        {"size": 4096, "mtime_ns": 1.5}, {"size": False, "mtime_ns": 1},
    ])
    def test_a_mark_that_is_not_two_whole_numbers_is_no_mark(self, settings, mark):
        settings.set(external_models.EXTERNAL_MODELS_SETTING, [
            {"id": "a", "path": "C:\\x", "verified": True, "weights_mark": mark}])
        (reference,) = external_models.load_references(settings)
        assert reference.weights_mark is None

    def test_a_value_that_is_not_a_list_reads_as_none(self, settings):
        settings.set(external_models.EXTERNAL_MODELS_SETTING, {"id": "a"})
        assert external_models.load_references(settings) == ()

    def test_no_app_settings_reads_as_none_stored(self):
        assert external_models.load_references(None) == ()

    def test_reading_never_edits_the_shared_default(self, catalogue, tmp_path,
                                                    settings, models_root):
        """End to end: AppSettings hands out a copy of its default when nothing
        is stored, and _parse() builds a new list rather than editing the one
        it is given. Either alone keeps the default intact; this pins that
        adding a reference still does."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        external_models.accept_catalogue_folder(settings, folder, models_root)
        assert app_settings._DEFAULTS[external_models.EXTERNAL_MODELS_SETTING] == []


class TestAFileThatCouldNotBeReadIsNotAnEmptyList:
    """An empty list decides things: a custom choice whose reference is gone
    is rewritten to "automatic" for good. app.json unreadable for a moment
    must not be what decides that."""

    def test_a_readable_file_is_known(self, catalogue, tmp_path, settings, models_root):
        _model, contents = catalogue["alpha"]
        external_models.accept_catalogue_folder(
            settings, _write(tmp_path / "mine", contents), models_root)
        references, known = external_models.read_references(settings)
        assert known is True and len(references) == 1

    def test_nothing_stored_yet_is_known_to_be_nothing(self, settings):
        assert external_models.read_references(settings) == ((), True)

    def test_no_app_settings_at_all_is_known_to_be_nothing(self):
        assert external_models.read_references(None) == ((), True)

    def test_an_unreadable_file_is_not_known(self, settings):
        with open(os.path.join(settings.global_dir, "app.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{ half written")
        assert external_models.read_references(settings) == ((), False)
        # The callers that only show or use the list degrade to "none".
        assert external_models.load_references(settings) == ()

    def test_a_lock_held_past_its_wait_is_not_known_either(self, settings, monkeypatch):
        """LockTimeout escaping here would escape a wx handler."""
        from coord_locks import LockTimeout

        def _held(key):
            raise LockTimeout("app.json")

        monkeypatch.setattr(settings, "get_strict", _held)
        assert external_models.read_references(settings) == ((), False)
        assert external_models.load_references(settings) == ()


class TestTheStateOfAReference:
    def _accepted(self, catalogue, tmp_path, settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "usb" / "whisper", contents)
        return external_models.accept_catalogue_folder(
            settings, folder, models_root).reference

    def test_a_verified_folder_that_is_still_there_is_ready(self, catalogue, tmp_path,
                                                            settings, models_root):
        reference = self._accepted(catalogue, tmp_path, settings, models_root)
        assert external_models.reference_state(reference) == external_models.REF_READY

    def test_a_folder_that_is_gone_is_missing_not_changed(self, catalogue, tmp_path,
                                                          settings, models_root):
        reference = self._accepted(catalogue, tmp_path, settings, models_root)
        shutil.rmtree(tmp_path / "usb")
        assert external_models.reference_state(reference) == (
            external_models.REF_FOLDER_MISSING)

    def test_a_file_that_shrank_is_a_change(self, catalogue, tmp_path, settings,
                                            models_root):
        reference = self._accepted(catalogue, tmp_path, settings, models_root)
        with open(os.path.join(reference.path, "tokenizer.json"), "wb") as fh:
            fh.write(b"{}")
        assert external_models.reference_state(reference) == external_models.REF_CHANGED

    def test_custom_weights_replaced_after_the_trial_are_a_change(
        self, catalogue, tmp_path, settings, models_root
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", dict(contents, **{"model.bin": b"C" * 99}))
        reference = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8").reference
        _write(folder, {"model.bin": b"D" * 120})
        assert external_models.reference_state(reference) == external_models.REF_CHANGED

    def test_a_model_this_version_no_longer_knows_is_unverified(self, tmp_path):
        folder = tmp_path / "old"
        reference = external_models.ExternalReference("r", str(folder), "retired", True)
        _write(folder, _synthetic_model("retired", b"R" * 10)[1])
        assert external_models.reference_state(reference) == (
            external_models.REF_UNVERIFIED)


class TestTheWeightsAreRecognisedByTheirIdentity:
    """Sizes are what the model store believes, because nobody else writes to
    its root. A folder of the user's is rewritten by whatever filled it — the
    reported case is a script re-converting a same-shaped fine-tune into it —
    so after the check the weights are recognised by the identity mark of the
    very file that was checked."""

    def test_a_catalogue_folder_rewritten_at_the_same_size_is_changed(
        self, catalogue, tmp_path, settings, models_root
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "whisper" / "large-v3", contents)
        external_models.accept_catalogue_folder(settings, folder, models_root)

        _rewrite_weights_same_size(folder)

        (reference,) = external_models.load_references(settings)
        assert external_models.identify_quick(folder).match == (
            external_models.MATCH_CANDIDATE)  # every size still the catalogue's
        assert external_models.reference_state(reference) == external_models.REF_CHANGED
        assert external_models.usable_catalogue_ids(models_root, (reference,)) == ()
        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(models_root, "alpha", (reference,))
        assert caught.value.code == errors.EXTERNAL_MODEL_CHANGED

    def test_a_custom_folder_rewritten_at_the_same_size_is_changed(
        self, catalogue, tmp_path, settings, models_root
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", dict(contents, **{"model.bin": b"C" * 99}))
        reference = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8").reference

        _rewrite_weights_same_size(folder)

        assert external_models.reference_state(reference) == external_models.REF_CHANGED
        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(
                models_root, external_models.custom_choice(reference), (reference,))
        assert caught.value.code == errors.EXTERNAL_MODEL_CHANGED

    def test_checking_again_accepts_the_new_weights_as_what_they_are(
        self, catalogue, tmp_path, settings, models_root
    ):
        """The way out the new sentence names: a custom folder whose weights
        changed is trial-loaded again and is ready again, under the same id."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", dict(contents, **{"model.bin": b"C" * 99}))
        first = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8").reference
        _rewrite_weights_same_size(folder)

        again = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8")

        assert (again.code, again.reference.id) == (
            external_models.ACCEPT_UPDATED, first.id)
        (reference,) = external_models.load_references(settings)
        assert external_models.reference_state(reference) == external_models.REF_READY

    def test_a_rewrite_during_the_hash_stores_nothing(self, catalogue, tmp_path,
                                                      settings, models_root,
                                                      monkeypatch):
        """The digest was of the old file and matched; the folder now holds
        another. Storing it — with the new file's mark — would call the new
        weights verified."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "whisper", contents)
        real_hash = external_models._hash_file

        def hash_then_rewrite(*args, **kwargs):
            digest = real_hash(*args, **kwargs)
            _rewrite_weights_same_size(folder)
            return digest

        monkeypatch.setattr(external_models, "_hash_file", hash_then_rewrite)

        outcome = external_models.accept_catalogue_folder(settings, folder, models_root)

        assert outcome.code == external_models.ACCEPT_CHANGED_WHILE_CHECKED
        assert outcome.identification.match == external_models.MATCH_VERIFIED
        assert external_models.load_references(settings) == ()

    def test_a_rewrite_during_the_trial_load_stores_nothing(self, catalogue, tmp_path,
                                                            settings, models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", dict(contents, **{"model.bin": b"C" * 99}))

        class _RewritingFactory(_Factory):
            def __call__(self, path, **kwargs):
                model = super().__call__(path, **kwargs)
                _rewrite_weights_same_size(folder)
                return model

        outcome = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_RewritingFactory()), "cpu", "int8")

        assert outcome.code == external_models.ACCEPT_CHANGED_WHILE_CHECKED
        assert external_models.load_references(settings) == ()

    def test_the_mark_survives_rewriting_the_list(self, catalogue, tmp_path, settings,
                                                  models_root):
        """Every write re-serialises every reference: adding and forgetting
        another one must carry this one's mark and key through untouched."""
        _model, contents = catalogue["alpha"]
        kept = _write(tmp_path / "kept", contents)
        external_models.accept_catalogue_folder(settings, kept, models_root)
        (before,) = external_models.load_references(settings)
        other = external_models.accept_catalogue_folder(
            settings, _write(tmp_path / "other", contents), models_root)
        external_models.forget_reference(settings, other.reference.id)

        (after,) = external_models.load_references(
            app_settings.AppSettings(settings.global_dir))

        assert (after.weights_mark, after.key) == (before.weights_mark, before.key)
        assert after.weights_mark is not None
        assert external_models.reference_state(after) == external_models.REF_READY

    def test_a_reference_without_a_mark_is_never_ready(self, catalogue, tmp_path,
                                                       settings, models_root):
        """Written by hand, or by a version before the mark: nothing says the
        weights are the ones that were checked, whatever `verified` claims."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "whisper", contents)
        custom = _write(tmp_path / "fine-tune", dict(contents, **{"model.bin": b"C" * 99}))
        settings.set(external_models.EXTERNAL_MODELS_SETTING, [
            {"id": "cat", "path": folder, "model_id": "alpha", "verified": True},
            {"id": "own", "path": custom, "model_id": None, "verified": True},
        ])
        catalogue_ref, custom_ref = external_models.load_references(settings)

        assert external_models.reference_state(catalogue_ref) == (
            external_models.REF_UNVERIFIED)
        assert external_models.reference_state(custom_ref) == (
            external_models.REF_UNVERIFIED)
        references = (catalogue_ref, custom_ref)
        assert external_models.usable_catalogue_ids(models_root, references) == ()
        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(
                models_root, external_models.custom_choice(custom_ref), references)
        assert caught.value.code == errors.EXTERNAL_MODEL_CHANGED


class TestWhereAModelIsLoadedFrom:
    """What part 10b swaps in for model_store.ensure_ready()."""

    @pytest.mark.parametrize("root_state", ["installed", "absent", "incomplete"])
    def test_with_no_reference_it_answers_what_the_store_answers(
        self, catalogue, models_root, root_state
    ):
        model, contents = catalogue["alpha"]
        if root_state != "absent":
            folder = _write(model_store.model_dir(models_root, model.id), contents)
            if root_state == "incomplete":
                os.remove(os.path.join(folder, "tokenizer.json"))

        def answer(call):
            try:
                return ("dir", call())
            except errors.TranscriptionError as exc:
                return ("error", exc.code)

        assert answer(lambda: external_models.model_directory(models_root, "alpha")) == (
            answer(lambda: model_store.ensure_ready(models_root, "alpha")))
        assert answer(lambda: external_models.model_directory(models_root, "nope")) == (
            answer(lambda: model_store.ensure_ready(models_root, "nope")))

    def _external(self, catalogue, tmp_path, settings, models_root, name="usb"):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / name / "whisper", contents)
        external_models.accept_catalogue_folder(settings, folder, models_root)
        return folder

    def test_winzapps_own_copy_wins_over_an_external_one(self, catalogue, tmp_path,
                                                         settings, models_root):
        model, contents = catalogue["alpha"]
        own = _write(model_store.model_dir(models_root, model.id), contents)
        self._external(catalogue, tmp_path, settings, models_root)
        references = external_models.load_references(settings)
        assert external_models.model_directory(models_root, "alpha", references) == own

    def test_an_external_copy_is_used_when_the_root_has_none(self, catalogue, tmp_path,
                                                             settings, models_root):
        folder = self._external(catalogue, tmp_path, settings, models_root)
        references = external_models.load_references(settings)
        assert external_models.model_directory(models_root, "alpha", references) == folder

    def test_an_external_copy_beats_an_incomplete_one_in_the_root(
        self, catalogue, tmp_path, settings, models_root
    ):
        model, contents = catalogue["alpha"]
        own = _write(model_store.model_dir(models_root, model.id), contents)
        os.remove(os.path.join(own, "model.bin"))
        folder = self._external(catalogue, tmp_path, settings, models_root)
        references = external_models.load_references(settings)
        assert external_models.model_directory(models_root, "alpha", references) == folder

    def test_an_unplugged_disk_is_its_own_error(self, catalogue, tmp_path, settings,
                                                models_root):
        self._external(catalogue, tmp_path, settings, models_root)
        references = external_models.load_references(settings)
        shutil.rmtree(tmp_path / "usb")

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(models_root, "alpha", references)

        assert caught.value.code == errors.EXTERNAL_MODEL_MISSING

    def test_a_changed_external_copy_asks_to_be_checked_again(
        self, catalogue, tmp_path, settings, models_root
    ):
        """Not MODEL_CORRUPTED: "download the model again" is not what fixes
        a folder somebody else changed."""
        folder = self._external(catalogue, tmp_path, settings, models_root)
        references = external_models.load_references(settings)
        os.remove(os.path.join(folder, "vocabulary.txt"))

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(models_root, "alpha", references)

        assert caught.value.code == errors.EXTERNAL_MODEL_CHANGED

    def test_an_unverified_external_copy_is_never_loaded(self, catalogue, tmp_path,
                                                         models_root):
        """A digest that failed: checking again would fail again, and WinZapp's
        own download is what gives the user that model."""
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", contents)
        references = (external_models.ExternalReference(
            "r", folder, "alpha", False, external_models._weights_mark(folder)),)

        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(models_root, "alpha", references)

        assert caught.value.code == errors.MODEL_NOT_INSTALLED

    def test_a_second_ready_copy_is_used_when_the_first_is_gone(
        self, catalogue, tmp_path, settings, models_root
    ):
        self._external(catalogue, tmp_path, settings, models_root, name="usb")
        second = self._external(catalogue, tmp_path, settings, models_root, name="disk")
        references = external_models.load_references(settings)
        shutil.rmtree(tmp_path / "usb")
        assert external_models.model_directory(models_root, "alpha", references) == second

    def test_a_custom_model_is_loaded_from_its_folder(self, catalogue, tmp_path, settings,
                                                      models_root):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "fine-tune", dict(contents, **{"model.bin": b"C" * 99}))
        reference = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(_Factory()), "cpu", "int8").reference
        choice = external_models.custom_choice(reference)
        references = external_models.load_references(settings)

        assert external_models.custom_reference_id(choice) == reference.id
        assert external_models.model_directory(models_root, choice, references) == folder

        shutil.rmtree(folder)
        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(models_root, choice, references)
        assert caught.value.code == errors.EXTERNAL_MODEL_MISSING

    def test_a_custom_choice_whose_reference_was_forgotten_is_not_installed(
        self, models_root
    ):
        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(models_root, "external:gone", ())
        assert caught.value.code == errors.MODEL_NOT_INSTALLED

    def test_a_catalogue_reference_cannot_be_chosen_as_custom(self, catalogue, tmp_path,
                                                              settings, models_root):
        self._external(catalogue, tmp_path, settings, models_root)
        (reference,) = external_models.load_references(settings)
        with pytest.raises(errors.TranscriptionError) as caught:
            external_models.model_directory(
                models_root, external_models.custom_choice(reference), (reference,))
        assert caught.value.code == errors.MODEL_NOT_INSTALLED

    def test_a_catalogue_id_is_not_a_custom_choice(self):
        assert external_models.custom_reference_id("large-v3") is None
        assert external_models.custom_reference_id(None) is None
        assert external_models.custom_reference_id("external:") is None


class TestWhatTheAutomaticChoiceMaySee:
    def test_the_root_and_ready_external_copies_in_catalogue_order(
        self, catalogue, tmp_path, settings, models_root
    ):
        alpha, alpha_files = catalogue["alpha"]
        gamma, gamma_files = catalogue["gamma"]
        _write(model_store.model_dir(models_root, gamma.id), gamma_files)
        external_models.accept_catalogue_folder(
            settings, _write(tmp_path / "usb" / "a", alpha_files), models_root)
        references = external_models.load_references(settings)

        assert external_models.usable_catalogue_ids(models_root, references) == (
            "alpha", "gamma")

    def test_never_a_custom_model(self, catalogue, tmp_path, settings, models_root):
        """Nothing says how much memory it needs, which is the whole question
        the automatic choice answers."""
        _model, contents = catalogue["alpha"]
        external_models.accept_custom_folder(
            settings, _write(tmp_path / "fine-tune", _same_length_other_bytes(contents)),
            models_root, _backend(_Factory()), "cpu", "int8")
        references = external_models.load_references(settings)
        assert external_models.usable_catalogue_ids(models_root, references) == ()

    def test_never_a_copy_whose_disk_is_unplugged(self, catalogue, tmp_path, settings,
                                                  models_root):
        _model, contents = catalogue["alpha"]
        external_models.accept_catalogue_folder(
            settings, _write(tmp_path / "usb" / "a", contents), models_root)
        references = external_models.load_references(settings)
        shutil.rmtree(tmp_path / "usb")
        assert external_models.usable_catalogue_ids(models_root, references) == ()

    def test_with_no_reference_it_is_what_the_store_lists(self, catalogue, models_root):
        model, contents = catalogue["gamma"]
        _write(model_store.model_dir(models_root, model.id), contents)
        assert external_models.usable_catalogue_ids(models_root) == (
            model_store.list_installed(models_root))


class TestFindingTheHuggingFaceCache:
    @pytest.mark.parametrize("environ,expected", [
        ({"HF_HUB_CACHE": r"D:\hub", "HUGGINGFACE_HUB_CACHE": r"E:\x",
          "HF_HOME": r"F:\y"}, r"D:\hub"),
        ({"HUGGINGFACE_HUB_CACHE": r"E:\legacy", "HF_HOME": r"F:\y"}, r"E:\legacy"),
        ({"HF_HOME": r"F:\hf"}, os.path.join(r"F:\hf", "hub")),
        ({"XDG_CACHE_HOME": r"G:\cache"}, os.path.join(r"G:\cache", "huggingface", "hub")),
    ])
    def test_the_same_precedence_as_huggingface_hub(self, environ, expected):
        assert external_models.hf_cache_dir(environ) == expected

    def test_with_nothing_set_it_is_under_the_profile(self):
        assert external_models.hf_cache_dir({}) == os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface", "hub")

    def test_it_agrees_with_huggingface_hub_itself(self):
        constants = pytest.importorskip("huggingface_hub.constants")
        assert os.path.normcase(external_models.hf_cache_dir()) == os.path.normcase(
            constants.HF_HUB_CACHE)


class TestDiscoveringTheCache:
    def test_whisper_snapshots_are_listed_and_catalogue_ones_recognised(
        self, catalogue, tmp_path
    ):
        hub = tmp_path / "hub"
        gamma, gamma_files = catalogue["gamma"]
        _alpha, alpha_files = catalogue["alpha"]
        recognised = _hf_snapshot(hub, gamma, gamma_files)
        other = _hf_snapshot(hub, gamma, dict(alpha_files, **{"model.bin": b"O" * 77}),
                             repo="someone/whisper-pt-finetune-ct2", revision="1" * 40)

        found = external_models.discover_hf_cache(str(hub))

        assert [(entry.path, entry.model_id) for entry in found] == [
            (recognised, "gamma"), (other, None)]
        assert found[0].repo == gamma.repo and found[0].revision == gamma.revision

    def test_what_is_not_a_whisper_model_is_left_out(self, catalogue, tmp_path):
        hub = tmp_path / "hub"
        gamma, gamma_files = catalogue["gamma"]
        _hf_snapshot(hub, gamma, gamma_files, repo="someone/opus-mt-ct2", revision="2" * 40)
        without_weights = {k: v for k, v in gamma_files.items() if k != "model.bin"}
        _hf_snapshot(hub, gamma, without_weights, repo="someone/whisper-half",
                     revision="3" * 40)
        (hub / "datasets--someone--whisper-audio" / "snapshots" / "4").mkdir(parents=True)
        (hub / "version.txt").write_text("1")

        assert external_models.discover_hf_cache(str(hub)) == ()

    def test_it_reads_repository_names_as_the_path_shortcut_does(self, catalogue,
                                                                  tmp_path):
        """A name with an empty part is not one the Hub wrote. Discovery used
        to rebuild the name on its own and list it as "whisper-org/"."""
        gamma, gamma_files = catalogue["gamma"]
        hub = tmp_path / "hub"
        snapshot = _hf_snapshot(hub, gamma, gamma_files, repo="whisper-org/",
                                revision="5" * 40)
        assert external_models.hf_cache_coordinates(snapshot) is None
        assert external_models.discover_hf_cache(str(hub)) == ()

    def test_a_cache_that_does_not_exist_is_empty(self, tmp_path):
        assert external_models.discover_hf_cache(str(tmp_path / "nowhere")) == ()

    def test_it_never_reads_the_weights(self, catalogue, tmp_path, monkeypatch):
        gamma, gamma_files = catalogue["gamma"]
        hub = tmp_path / "hub"
        _hf_snapshot(hub, gamma, gamma_files)
        monkeypatch.setattr(
            external_models, "_hash_file",
            lambda *a, **k: pytest.fail("discovery read model.bin"),
        )
        assert len(external_models.discover_hf_cache(str(hub))) == 1


class TestTheNewErrorCodes:
    def test_missing_and_changed_are_neither_not_installed_nor_corrupted(self):
        """Both of those sentences say "download the model", which is
        impossible for a custom model and not what fixes an external one."""
        assert errors.EXTERNAL_MODEL_MISSING in errors.ERROR_CODES
        assert errors.EXTERNAL_MODEL_CHANGED in errors.ERROR_CODES
        keys = {errors.error_i18n_key(code) for code in (
            errors.EXTERNAL_MODEL_MISSING, errors.EXTERNAL_MODEL_CHANGED,
            errors.MODEL_NOT_INSTALLED, errors.MODEL_CORRUPTED)}
        assert len(keys) == 4

    @pytest.mark.parametrize("code", [errors.EXTERNAL_MODEL_MISSING,
                                      errors.EXTERNAL_MODEL_CHANGED])
    def test_they_are_not_worth_redoing_on_the_processor(self, code):
        error = errors.TranscriptionError(code)
        from core.transcription import device
        assert device.should_retry_on_cpu(error, device.DEVICE_CUDA) is False


def test_only_the_folder_name_is_what_the_log_keeps(catalogue, tmp_path, caplog):
    """The folder's name is the diagnosis; its full path carries the Windows
    user's name, so it is not logged."""
    import logging
    _model, contents = catalogue["alpha"]
    folder = _write(tmp_path / "mine", contents)
    with caplog.at_level(logging.INFO):
        external_models.identify(folder)
    assert os.path.basename(folder) in caplog.text
    assert folder not in caplog.text


# ── Part 10b: the run, the names and the models folder ───────────────────────


class _CapturingBackend(backend_module.TranscriptionBackend):
    """Records the request a job hands over, and transcribes nothing."""

    id = "capturing"

    def __init__(self):
        self.requests = []

    def is_available(self):
        return True

    def load_model(self, request, should_cancel=None):
        self.requests.append(request)

    def transcribe(self, request, progress=None, should_cancel=None):
        return backend_module.TranscriptionResult(
            text="", language=None, language_probability=None, duration_seconds=1.0)


class TestTheRunLoadsWhereTheReferenceSays:
    """The backend asks model_directory() on every load, with the references
    the run was started with: the model's own folder for a custom choice, and
    for a catalogue id whatever copy is there."""

    def _request(self, models_root, model_id, references, tmp_path):
        return backend_module.TranscriptionRequest(
            audio_path=str(tmp_path / "prepared.wav"), models_root=models_root,
            model_id=model_id, device="cpu", compute_type="int8",
            external_references=tuple(references))

    def test_a_custom_model_is_loaded_from_its_own_folder(
        self, catalogue, tmp_path, settings, models_root
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "mine", dict(contents, **{"model.bin": b"C" * 5000}))
        factory = _Factory()
        reference = external_models.accept_custom_folder(
            settings, folder, models_root, _backend(factory), "cpu", "int8").reference
        factory.loads.clear()

        _backend(factory).load_model(self._request(
            models_root, external_models.custom_choice(reference), [reference], tmp_path))

        assert [load["path"] for load in factory.loads] == [folder]
        assert factory.loads[0]["local_files_only"] is True

    def test_a_catalogue_model_with_only_an_external_copy_is_loaded_from_it(
        self, catalogue, tmp_path, settings, models_root
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "theirs", contents)
        reference = external_models.accept_catalogue_folder(
            settings, folder, models_root).reference
        factory = _Factory()

        _backend(factory).load_model(
            self._request(models_root, "alpha", [reference], tmp_path))

        assert [load["path"] for load in factory.loads] == [folder]

    def test_a_disk_unplugged_since_the_decision_is_said_so_at_load_time(
        self, catalogue, tmp_path, settings, models_root
    ):
        _model, contents = catalogue["alpha"]
        folder = _write(tmp_path / "theirs", contents)
        reference = external_models.accept_catalogue_folder(
            settings, folder, models_root).reference
        shutil.rmtree(folder)
        factory = _Factory()

        with pytest.raises(errors.TranscriptionError) as caught:
            _backend(factory).load_model(
                self._request(models_root, "alpha", [reference], tmp_path))

        assert caught.value.code == errors.EXTERNAL_MODEL_MISSING
        assert factory.loads == []

    def test_a_job_hands_the_references_to_the_backend(self, tmp_path):
        from core.transcription import audio_prep, device, job as job_module

        reference = external_models.ExternalReference("r", "/x", None, True, (1, 2), "/x")
        capture = _CapturingBackend()
        job = job_module.TranscriptionJob(
            None, None, str(tmp_path / "models"), "external:r",
            backend=capture, prepared=audio_prep.PreparedAudio(str(tmp_path / "a.wav"), 1.0),
            external_references=[reference],
            probe=lambda: device.HardwareProbe(total_ram_mb=16384, available_ram_mb=8192),
        )
        job.start()
        job.join(10)
        assert capture.requests[0].external_references == (reference,)
        assert capture.requests[0].model_id == "external:r"


class TestTheNamesOfTheModels:
    def test_a_folder_is_named_by_its_last_component(self):
        assert external_models.folder_name("/disk/stuff/my-model") == "my-model"
        assert external_models.folder_name("/disk/stuff/my-model/") == "my-model"

    def test_a_root_has_no_last_component_and_is_named_whole(self):
        assert external_models.folder_name("/") == "/"

    def test_a_cache_snapshot_is_named_by_its_repository_not_its_commit(self):
        path = "/c/hub/models--Systran--faster-whisper-small/snapshots/" + "e" * 40
        assert external_models.folder_name(path) == "faster-whisper-small"

    def test_a_catalogue_id_is_its_own_name(self):
        assert external_models.model_name("large-v3", ()) == "large-v3"

    def test_a_custom_choice_is_its_folders_name_and_nothing_once_forgotten(self):
        reference = external_models.ExternalReference("abc", "/d/mine", None, True)
        assert external_models.model_name("external:abc", (reference,)) == "mine"
        assert external_models.model_name("external:abc", ()) is None

    def test_only_custom_references_are_custom_ids(self):
        custom = external_models.ExternalReference("a", "/x", None, True)
        known = external_models.ExternalReference("b", "/y", "large-v3", True)
        assert external_models.custom_reference_backends((custom, known)) == {
            "a": custom.backend}


class TestAModelsFolderCannotSwallowAReference:
    """remove_model() deletes inside the models folder: a reference there would
    make "Remove" delete the user's own files."""

    def _reference(self, path):
        return external_models.ExternalReference(
            "r", str(path), None, True, (1, 2), canonical_dir(str(path)))

    def test_a_folder_that_contains_a_reference_is_refused(self, tmp_path):
        inner = tmp_path / "all" / "models" / "mine"
        inner.mkdir(parents=True)
        reference = self._reference(inner)
        assert external_models.references_inside_root(
            (reference,), str(tmp_path / "all")) == (reference,)

    def test_the_same_folder_counts_and_a_sibling_does_not(self, tmp_path):
        mine = tmp_path / "mine"
        mine.mkdir()
        (tmp_path / "mine-too").mkdir()
        reference = self._reference(mine)
        assert external_models.references_inside_root((reference,), str(mine)) == (reference,)
        assert external_models.references_inside_root(
            (reference,), str(tmp_path / "mine-too")) == ()

    def test_a_folder_beside_it_is_fine(self, tmp_path):
        mine = tmp_path / "mine"
        mine.mkdir()
        (tmp_path / "elsewhere").mkdir()
        assert external_models.references_inside_root(
            (self._reference(mine),), str(tmp_path / "elsewhere")) == ()

    def test_no_references_are_nothing_to_refuse(self, tmp_path):
        assert external_models.references_inside_root((), str(tmp_path)) == ()


class TestTheAutomaticChoiceAsksTheListingEveryoneAsks:
    def test_the_models_folder_is_listed_through_list_installed(
        self, catalogue, models_root, monkeypatch
    ):
        """One listing, the same function every caller and every test of "what
        is installed" already goes through."""
        monkeypatch.setattr(model_store, "list_installed", lambda root: ("gamma",))
        assert external_models.usable_catalogue_ids(models_root) == ("gamma",)
