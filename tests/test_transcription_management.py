"""The settings tab's management actions, before any wx touches them.

Every failure pinned here is one a blind user hears rather than sees:

* **A flood of progress.** model_store reports every megabyte — ~3000 times
  for large-v3. Forwarded as is, that is thousands of `wx.CallAfter`s and a
  screen reader reading numbers for the whole download. The throttle moves the
  bar on an interval and speaks only at new quarters; a regression is never
  announced as if the download had gone backwards.

* **"100%" before the end.** The CUDA install reports its last byte and only
  then loads cuBLAS to find out whether the card can use it; a user told
  "100%" there hears, a moment later, that it still cannot. The end is never
  spoken by the progress — the finished report says it.

* **Agreeing to a download blind, or being refused one that fits.** The
  summary has to quote what the gate will really demand. A download
  interrupted at 90% of model.bin used to be quoted as the whole model, so a
  user with room for the remaining tenth would have been told there was no
  space; the summary and the gate now share model_store's own figure, and the
  test below builds a real `.part` to hold them to it.

* **A report that never comes, or comes twice.** The tab holds a progress
  dialog open until it hears back, so every action, on every path — success,
  a coded error, a cancellation, a bug — ends in exactly one finished report.

* **The probe on the UI thread.** 0.67 s of NVDA unable to read the tab
  control, measured; the probe is delivered by callback from another thread.

* **The wrong sentence.** Libraries left behind by a mapped DLL are not
  "removed", libraries the card still cannot use are not a success, a
  half-finished move has to say where each model is now, and a cancellation is
  never an error — nor, for the CUDA install, advice to go "download it in the
  settings", which is the screen the user is on.

No wx, no network, no GPU: model_store and cuda_runtime are replaced by fakes
wherever an action would reach past the disk.
"""

import dataclasses
import errno
import json
import logging
import os
import re
import threading
import time

from tests.locales import load_strings, registered_locale_codes
import pytest

from core.transcription import (
    cuda_runtime,
    device,
    errors,
    management,
    management_whisper_cpp,
    model_catalog,
    model_store,
    whisper_cpp_builds,
    whisper_cpp_catalog,
    whisper_cpp_runtime,
)
from tests.conftest import words_found_in


LOCALES = registered_locale_codes()

_JOIN_TIMEOUT = 10


def _sized_file(path, size):
    """A file of exactly `size` bytes without writing them (extended, not filled)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


# ── The throttle ─────────────────────────────────────────────────────────────


def _sweep(throttle, total, step, start_at=0.0, dt=0.0):
    """Feed 0..total in `step`s; return every tick."""
    ticks = []
    now = start_at
    for done in range(0, total + 1, step):
        ticks.append(throttle.update(done, total, now))
        now += dt
    return ticks


class TestProgressThrottle:
    def test_the_first_report_moves_the_bar_and_says_nothing(self):
        tick = management.ProgressThrottle().update(0, 1000, now=0.0)
        assert tick.update_bar is True
        assert tick.speak is False
        assert tick.percent == 0

    def test_the_bar_moves_only_once_per_interval(self):
        throttle = management.ProgressThrottle(bar_interval=0.5, milestones=())
        assert throttle.update(1, 1000, now=10.0).update_bar is True
        assert throttle.update(2, 1000, now=10.1).update_bar is False
        assert throttle.update(3, 1000, now=10.49).update_bar is False
        assert throttle.update(4, 1000, now=10.5).update_bar is True
        assert throttle.update(5, 1000, now=10.6).update_bar is False

    def test_the_clock_is_the_argument_not_the_machine(self):
        # Two reports a "second" apart by the argument, microseconds apart in
        # reality: only an interval read off `now` can tell them apart.
        throttle = management.ProgressThrottle(bar_interval=1.0, milestones=())
        throttle.update(1, 1000, now=0.0)
        assert throttle.update(2, 1000, now=1.0).update_bar is True

    def test_a_megabyte_by_megabyte_download_speaks_three_times(self):
        ticks = _sweep(management.ProgressThrottle(), total=3000, step=1)
        assert [t.percent for t in ticks if t.speak] == [25, 50, 75]

    def test_the_end_is_not_a_milestone(self):
        # The finished report is the announcement of the end; see B2 in the
        # module docstring of management.py.
        assert 100 not in management.SPEECH_MILESTONES

    def test_a_milestone_is_spoken_once_even_when_reports_linger_on_it(self):
        throttle = management.ProgressThrottle()
        spoken = [throttle.update(250, 1000, now=float(i)).speak for i in range(5)]
        assert spoken == [True, False, False, False, False]

    def test_a_jump_over_several_milestones_is_said_once_with_the_real_figure(self):
        throttle = management.ProgressThrottle()
        throttle.update(0, 1000, now=0.0)
        tick = throttle.update(900, 1000, now=0.0)
        assert tick.speak is True
        assert tick.percent == 90
        assert throttle.update(950, 1000, now=0.0).speak is False

    def test_a_jump_straight_to_the_end_says_nothing(self):
        # A same-volume move is one rename: 0% to 100% in a single report.
        throttle = management.ProgressThrottle()
        throttle.update(0, 1000, now=0.0)
        tick = throttle.update(1000, 1000, now=0.0)
        assert tick.speak is False
        assert tick.update_bar is True

    def test_the_end_is_silent_even_for_a_caller_that_asks_for_it(self):
        throttle = management.ProgressThrottle(milestones=(50, 100))
        spoken = [t.percent for t in _sweep(throttle, total=100, step=1) if t.speak]
        assert spoken == [50]

    def test_speaking_moves_the_bar_even_inside_the_interval(self):
        throttle = management.ProgressThrottle(bar_interval=60.0)
        throttle.update(0, 1000, now=0.0)
        tick = throttle.update(250, 1000, now=0.01)
        assert tick.speak and tick.update_bar

    def test_the_bar_reaches_the_end_even_inside_the_interval(self):
        # A frozen clock: no interval ever elapses, and the gauge must still
        # not be left at 97% beside the sentence that says it finished.
        throttle = management.ProgressThrottle(bar_interval=60.0)
        ticks = _sweep(throttle, total=3000, step=1)
        bars = [t.percent for t in ticks if t.update_bar]
        assert bars[-1] == 100
        assert ticks[-1].update_bar is True
        assert throttle.update(3000, 3000, now=0.0).update_bar is False

    def test_the_last_tenth_of_a_percent_is_not_the_end(self):
        # cuda_runtime's bar sits at ~99.9% before its final report.
        throttle = management.ProgressThrottle()
        total = cuda_runtime.INSTALL_BYTES
        almost = total - total // 2000
        throttle.update(0, total, now=0.0)
        # 25, 50 and 75 were crossed on the way, so this does speak — but the
        # figure it speaks is 99, never the 100 that would mean "finished".
        assert throttle.update(almost, total, now=1.0).percent == 99
        end = throttle.update(total, total, now=2.0)
        assert end.percent == 100 and end.speak is False

    @pytest.mark.parametrize("total", [None, 0, -5, "not a number"])
    def test_an_unknown_total_never_divides_and_never_speaks(self, total):
        throttle = management.ProgressThrottle(bar_interval=0.5)
        ticks = [throttle.update(done, total, now=done * 0.1) for done in range(0, 40)]
        assert all(t.percent is None and t.total is None for t in ticks)
        assert not any(t.speak for t in ticks)
        # Still moving on the interval — an indeterminate bar is not a frozen one.
        assert sum(t.update_bar for t in ticks) >= 7

    def test_a_regression_moves_the_bar_at_once_and_is_never_spoken(self):
        throttle = management.ProgressThrottle(bar_interval=60.0)
        throttle.update(0, 1000, now=0.0)
        assert throttle.update(600, 1000, now=0.1).speak is True  # 50 crossed
        back = throttle.update(100, 1000, now=0.2)
        assert back.update_bar is True
        assert back.speak is False
        assert back.percent == 10

    def test_after_a_regression_nothing_already_said_is_said_again(self):
        throttle = management.ProgressThrottle()
        throttle.update(600, 1000, now=0.0)          # says 60 (past 25 and 50)
        throttle.update(0, 1000, now=1.0)            # the file started over
        again = [throttle.update(d, 1000, now=2.0 + d).speak for d in range(0, 700, 10)]
        assert not any(again)
        assert throttle.update(750, 1000, now=900.0).speak is True

    def test_done_past_total_is_one_hundred_not_more(self):
        tick = management.ProgressThrottle().update(1200, 1000, now=0.0)
        assert tick.percent == 100

    def test_progress_percent_rounds_down(self):
        assert management.progress_percent(999, 1000) == 99
        assert management.progress_percent(1000, 1000) == 100
        assert management.progress_percent(0, 1000) == 0
        assert management.progress_percent(5, 0) is None
        assert management.progress_percent(5, None) is None


# ── The summary before a download ────────────────────────────────────────────


def _no_card():
    return device.HardwareProbe(total_ram_mb=16000, available_ram_mb=8000)


def _card(libraries_ok=True):
    return device.HardwareProbe(
        cuda_available=True,
        cuda_device_count=1,
        compute_capability=(8, 6),
        total_vram_mb=12000,
        free_vram_mb=10000,
        total_ram_mb=16000,
        available_ram_mb=8000,
        cuda_libraries_ok=libraries_ok,
    )


def _gate_demand(monkeypatch, root, model):
    """The bytes download_model()'s own gate asks for, stopped right there."""
    asked = []

    def record(_root, needed_bytes):
        asked.append(needed_bytes)
        raise errors.TranscriptionError(errors.NO_DISK_SPACE, "stopped by the test")

    monkeypatch.setattr(model_store, "ensure_free_space", record)
    with pytest.raises(errors.TranscriptionError):
        # The gate runs before any session is used, so none is ever touched.
        model_store.download_model(model, root, session=object())
    return asked[0]


class TestModelDownloadSummary:
    def test_names_sizes_destination_and_device(self, tmp_path):
        model = model_catalog.get_model("small")
        summary = management.model_download_summary(
            "small", str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        assert summary.subject == management.SUBJECT_MODEL
        assert summary.model_id == "small"
        assert summary.download_bytes == model.download_bytes
        assert summary.installed_bytes == model.disk_bytes
        assert summary.required_free_bytes == model_store.required_free_bytes(
            model.download_bytes
        )
        assert summary.destination == model_store.model_dir(str(tmp_path), "small")
        assert summary.enough_space is True
        assert summary.resumable is True
        assert summary.freed_bytes == 0

    def test_on_a_machine_without_a_card_it_runs_on_the_processor(self, tmp_path):
        summary = management.model_download_summary(
            "tiny", str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        assert summary.device == device.DEVICE_CPU
        assert summary.device_reason == device.REASON_NO_CUDA_FOUND

    def test_with_a_usable_card_it_runs_on_cuda(self, tmp_path):
        summary = management.model_download_summary(
            "tiny", str(tmp_path), _card(), free_bytes=10 ** 12
        )
        assert summary.device == device.DEVICE_CUDA
        assert summary.device_reason == device.REASON_CUDA_SELECTED

    def test_the_preference_is_honoured(self, tmp_path):
        summary = management.model_download_summary(
            "tiny", str(tmp_path), _card(), free_bytes=10 ** 12,
            device_preference=device.PREFERENCE_CPU,
        )
        assert summary.device == device.DEVICE_CPU

    @pytest.mark.parametrize("delta, fits", [(0, True), (-1, False)])
    def test_enough_space_agrees_with_the_real_gate(self, tmp_path, monkeypatch, delta, fits):
        # The summary and the gate the download really passes through must give
        # the same answer at the boundary, or the user is told "it fits" and
        # then refused.
        model = model_catalog.get_model("base")
        free = model_store.required_free_bytes(model.download_bytes) + delta
        summary = management.model_download_summary(
            "base", str(tmp_path), _no_card(), free_bytes=free
        )
        monkeypatch.setattr(model_store, "free_bytes", lambda _root: free)
        try:
            model_store.ensure_free_space(str(tmp_path), model.download_bytes)
            gate_allows = True
        except errors.TranscriptionError:
            gate_allows = False
        assert summary.enough_space is fits
        assert gate_allows is fits

    def test_the_slack_is_model_stores_own(self, tmp_path, monkeypatch):
        monkeypatch.setattr(model_store, "_FREE_SPACE_SLACK_BYTES", 7)
        model = model_catalog.get_model("tiny")
        summary = management.model_download_summary(
            "tiny", str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        assert summary.required_free_bytes == model.download_bytes + 7

    def test_unmeasurable_free_space_is_unknown_not_enough(self, tmp_path):
        summary = management.model_download_summary(
            "tiny", str(tmp_path), _no_card(), free_bytes=None
        )
        assert summary.enough_space is None

    def test_a_download_interrupted_in_model_bin_is_quoted_as_the_gate_counts_it(
            self, tmp_path, monkeypatch):
        # The common case: model.bin is 95-99% of a model, and a `.part` of it
        # is never counted by InstallState.present_bytes. Quoting the whole
        # model here once told a user with room for the remaining tenth that
        # there was no space.
        root = str(tmp_path)
        model = model_catalog.get_model("tiny")
        directory = model_store.model_dir(root, model.id)
        part_bytes = model.model_bin_bytes * 9 // 10
        _sized_file(os.path.join(directory, "model.bin.part"), part_bytes)
        for name, size in model.files:
            if name != "model.bin":
                _sized_file(os.path.join(directory, name), size)

        summary = management.model_download_summary(
            model.id, root, _no_card(), free_bytes=10 ** 12
        )
        gate = _gate_demand(monkeypatch, root, model)

        assert summary.download_bytes == gate
        assert summary.download_bytes == model.model_bin_bytes - part_bytes
        assert summary.required_free_bytes == model_store.required_free_bytes(gate)
        # Well under a tenth of the model, where the old quote was all of it.
        assert summary.download_bytes < model.download_bytes // 9

    def test_nothing_on_disk_is_quoted_whole_by_both(self, tmp_path, monkeypatch):
        model = model_catalog.get_model("tiny")
        summary = management.model_download_summary(
            model.id, str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        assert summary.download_bytes == _gate_demand(monkeypatch, str(tmp_path), model)
        assert summary.download_bytes == model.download_bytes

    def test_memory_fit_is_reported_and_unknown_when_unmeasured(self, tmp_path):
        big = management.model_download_summary(
            "large-v3", str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        small = management.model_download_summary(
            "tiny", str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        unmeasured = management.model_download_summary(
            "tiny", str(tmp_path), device.HardwareProbe(), free_bytes=10 ** 12
        )
        assert big.fits_memory is False
        assert small.fits_memory is True
        assert unmeasured.fits_memory is None

    def test_an_unknown_model_has_no_summary(self, tmp_path):
        assert management.model_download_summary(
            "retired-model", str(tmp_path), _no_card(), free_bytes=10 ** 12
        ) is None


class TestRepairSummary:
    """repair_model() deletes everything and fetches the whole model."""

    def _installed_tiny(self, root):
        model = model_catalog.get_model("tiny")
        directory = model_store.model_dir(root, model.id)
        for name, size in model.files:
            _sized_file(os.path.join(directory, name), size)
        return model

    def test_a_model_with_every_file_present_is_quoted_whole(self, tmp_path):
        # Every size right, the digest wrong: the state a repair exists for.
        # As a plain download it would be quoted at 0 bytes.
        model = self._installed_tiny(str(tmp_path))
        plain = management.model_download_summary(
            model.id, str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        repair = management.model_download_summary(
            model.id, str(tmp_path), _no_card(), free_bytes=10 ** 12, repair=True
        )
        assert plain.download_bytes == 0
        assert repair.download_bytes == model.download_bytes
        assert repair.freed_bytes == model.disk_bytes

    @pytest.mark.parametrize("delta, fits", [(0, True), (-1, False)])
    def test_what_the_removal_frees_counts_as_free(self, tmp_path, delta, fits):
        model = self._installed_tiny(str(tmp_path))
        required = model_store.required_free_bytes(model.download_bytes)
        free = required - model.disk_bytes + delta
        summary = management.model_download_summary(
            model.id, str(tmp_path), _no_card(), free_bytes=free, repair=True
        )
        assert summary.required_free_bytes == required
        assert summary.enough_space is fits


class TestCudaRuntimeDownloadSummary:
    def test_sizes_and_the_peak_the_gate_measures(self, tmp_path):
        summary = management.cuda_runtime_download_summary(
            _card(libraries_ok=False), free_bytes=10 ** 12, directory=str(tmp_path)
        )
        assert summary.subject == management.SUBJECT_CUDA_RUNTIME
        assert summary.model_id is None
        assert summary.download_bytes == cuda_runtime.WHEEL_BYTES
        assert summary.installed_bytes == cuda_runtime.EXTRACTED_BYTES
        assert summary.required_free_bytes == model_store.required_free_bytes(
            cuda_runtime.INSTALL_BYTES
        )
        assert summary.destination == str(tmp_path)
        assert summary.enough_space is True
        assert summary.fits_memory is None

    def test_it_cannot_be_resumed_and_says_so(self, tmp_path):
        summary = management.cuda_runtime_download_summary(
            _card(libraries_ok=False), free_bytes=10 ** 12, directory=str(tmp_path)
        )
        assert summary.resumable is False
        assert summary.download_bytes > 550 * 1000 * 1000

    def test_not_enough_space(self, tmp_path):
        summary = management.cuda_runtime_download_summary(
            _card(libraries_ok=False), free_bytes=cuda_runtime.INSTALL_BYTES,
            directory=str(tmp_path),
        )
        assert summary.enough_space is False

    def test_the_device_is_the_one_after_the_install(self, tmp_path):
        # The libraries are missing *now*; the question is where a run goes
        # once they are not.
        summary = management.cuda_runtime_download_summary(
            _card(libraries_ok=False), free_bytes=10 ** 12, directory=str(tmp_path)
        )
        assert summary.device == device.DEVICE_CUDA
        assert summary.device_reason == device.REASON_CUDA_SELECTED

    def test_without_a_card_it_still_says_processor(self, tmp_path):
        summary = management.cuda_runtime_download_summary(
            _no_card(), free_bytes=10 ** 12, directory=str(tmp_path)
        )
        assert summary.device == device.DEVICE_CPU

    def test_with_the_processor_chosen_it_says_processor(self, tmp_path):
        summary = management.cuda_runtime_download_summary(
            _card(libraries_ok=False), free_bytes=10 ** 12, directory=str(tmp_path),
            device_preference=device.PREFERENCE_CPU,
        )
        assert summary.device == device.DEVICE_CPU
        assert summary.device_reason == device.REASON_CPU_REQUESTED

    def test_the_default_destination_is_cuda_runtimes_own(self, monkeypatch):
        monkeypatch.setattr(cuda_runtime, "default_cuda_runtime_dir", lambda: "X:\\runtime")
        summary = management.cuda_runtime_download_summary(_no_card(), free_bytes=None)
        assert summary.destination == "X:\\runtime"
        assert summary.enough_space is None

    @pytest.mark.parametrize("delta, fits", [(0, True), (-1, False)])
    def test_a_repair_counts_the_installed_files_as_freed(self, tmp_path, delta, fits):
        sizes = dict(zip(cuda_runtime.INSTALLED_FILES, (1000, 2000, 30)))
        for name, size in sizes.items():
            _sized_file(os.path.join(str(tmp_path), name), size)
        freed = sum(sizes.values())
        free = model_store.required_free_bytes(cuda_runtime.INSTALL_BYTES) - freed + delta
        summary = management.cuda_runtime_download_summary(
            _card(), free_bytes=free, directory=str(tmp_path), repair=True
        )
        assert summary.freed_bytes == freed
        assert summary.enough_space is fits
        plain = management.cuda_runtime_download_summary(
            _card(), free_bytes=free, directory=str(tmp_path)
        )
        assert plain.freed_bytes == 0
        assert plain.enough_space is False


def _card_of(capability):
    return dataclasses.replace(_card(), compute_capability=capability)


_GGML_ID = "ggml-tiny-q5_1"
_CPU = whisper_cpp_builds.BUILD_CPU
_CUDA = whisper_cpp_builds.BUILD_CUDA
# What the recorders replace, for the tests that need the real one back.
_REAL_ENSURE_VAD = management_whisper_cpp.ensure_vad


class TestGgmlModelSummary:
    def test_the_voice_activity_model_is_quoted_with_the_first_ggml_model(self, tmp_path):
        # Nothing else ever fetches it, so the bytes the user agrees to have to
        # include it — 885 KB, but "32 MB" followed by a second download is a
        # figure the user was not told.
        model = whisper_cpp_catalog.get_model(_GGML_ID)
        summary = management.model_download_summary(
            _GGML_ID, str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        vad = whisper_cpp_catalog.VAD_MODEL
        assert summary.model_id == _GGML_ID
        assert summary.download_bytes == model.download_bytes + vad.download_bytes
        assert summary.required_free_bytes == model_store.required_free_bytes(
            summary.download_bytes
        )

    def test_a_voice_activity_model_already_there_is_not_quoted_again(
        self, tmp_path, monkeypatch
    ):
        vad = whisper_cpp_catalog.VAD_MODEL
        real = model_store.remaining_download_bytes
        monkeypatch.setattr(
            model_store, "remaining_download_bytes",
            lambda root, model: 0 if model is vad else real(root, model),
        )
        summary = management.model_download_summary(
            _GGML_ID, str(tmp_path), _no_card(), free_bytes=10 ** 12
        )
        assert summary.download_bytes == whisper_cpp_catalog.get_model(_GGML_ID).download_bytes

    def test_a_repair_quotes_the_voice_activity_model_whole(self, tmp_path):
        summary = management.model_download_summary(
            _GGML_ID, str(tmp_path), _no_card(), free_bytes=10 ** 12, repair=True
        )
        assert summary.download_bytes == (
            whisper_cpp_catalog.get_model(_GGML_ID).download_bytes
            + whisper_cpp_catalog.VAD_MODEL.download_bytes
        )

    @pytest.mark.parametrize("capability, installed, expected", [
        ((8, 6), True, (device.DEVICE_CUDA, device.REASON_CUDA_SELECTED)),
        ((8, 6), False, (device.DEVICE_CPU, device.REASON_CUDA_BUILD_MISSING)),
        ((12, 0), True, (device.DEVICE_CPU, device.REASON_CUDA_BUILD_UNSUPPORTED)),
        (None, True, (device.DEVICE_CPU, device.REASON_CUDA_BUILD_UNSUPPORTED)),
    ])
    def test_the_device_is_whisper_cpps_own(self, tmp_path, capability, installed, expected):
        summary = management.model_download_summary(
            _GGML_ID, str(tmp_path), _card_of(capability), free_bytes=10 ** 12,
            whisper_cpp_cuda_installed=installed,
        )
        assert (summary.device, summary.device_reason) == expected

    def test_the_voice_activity_model_is_never_a_model_of_its_own(self, tmp_path):
        assert management.model_download_summary(
            whisper_cpp_catalog.VAD_MODEL.id, str(tmp_path), _no_card(), free_bytes=None
        ) is None


class TestWhisperCppDownloadSummary:
    def test_the_processor_build_alone(self, tmp_path):
        summary = management_whisper_cpp.whisper_cpp_download_summary(
            _CPU.id, _no_card(), free_bytes=10 ** 12, directory=str(tmp_path)
        )
        assert summary.subject == management.SUBJECT_WHISPER_CPP
        assert summary.build_id == _CPU.id
        assert summary.download_bytes == _CPU.archive_bytes
        assert summary.includes_cpu_build is False
        assert summary.resumable is False
        assert summary.destination == whisper_cpp_runtime.build_dir(str(tmp_path), _CPU)
        assert summary.device == device.DEVICE_CPU

    def test_the_graphics_build_without_the_processor_one_quotes_both(self, tmp_path):
        summary = management_whisper_cpp.whisper_cpp_download_summary(
            _CUDA.id, _card(), free_bytes=10 ** 12, directory=str(tmp_path)
        )
        assert summary.includes_cpu_build is True
        assert summary.download_bytes == _CUDA.archive_bytes + _CPU.archive_bytes
        # The device after the install, which is what the user is deciding.
        assert (summary.device, summary.device_reason) == (
            device.DEVICE_CUDA, device.REASON_CUDA_SELECTED
        )

    def test_the_graphics_build_beside_the_processor_one_quotes_itself(self, tmp_path):
        summary = management_whisper_cpp.whisper_cpp_download_summary(
            _CUDA.id, _card(), free_bytes=10 ** 12, installed_build_ids=(_CPU.id,),
            directory=str(tmp_path),
        )
        assert summary.includes_cpu_build is False
        assert summary.download_bytes == _CUDA.archive_bytes

    @pytest.mark.parametrize("delta, fits", [(0, True), (-1, False)])
    def test_the_space_is_the_installs_own_gate(self, tmp_path, delta, fits):
        required = model_store.required_free_bytes(
            _CPU.archive_bytes * whisper_cpp_runtime.INSTALL_SPACE_FACTOR
        )
        summary = management_whisper_cpp.whisper_cpp_download_summary(
            _CPU.id, _no_card(), free_bytes=required + delta, directory=str(tmp_path)
        )
        assert summary.required_free_bytes == required
        assert summary.enough_space is fits

    def test_an_unknown_build_has_no_summary(self):
        assert management_whisper_cpp.whisper_cpp_download_summary(
            "nonsense", _no_card(), free_bytes=None
        ) is None


# ── The executor ─────────────────────────────────────────────────────────────


_TARGETS = {
    management.ACTION_DOWNLOAD_MODEL: (model_store, "download_model"),
    management.ACTION_REPAIR_MODEL: (model_store, "repair_model"),
    management.ACTION_VERIFY_MODEL: (model_store, "verify_model"),
    management.ACTION_REMOVE_MODEL: (model_store, "remove_model"),
    management.ACTION_MOVE_MODELS: (model_store, "move_models"),
    management.ACTION_INSTALL_CUDA_RUNTIME: (cuda_runtime, "install_cuda_runtime"),
    management.ACTION_REPAIR_CUDA_RUNTIME: (cuda_runtime, "repair_cuda_runtime"),
    management.ACTION_VERIFY_CUDA_RUNTIME: (cuda_runtime, "verify_installation"),
    management.ACTION_REMOVE_CUDA_RUNTIME: (cuda_runtime, "remove_cuda_runtime"),
    management.ACTION_INSTALL_WHISPER_CPP: (whisper_cpp_runtime, "install_build"),
    management.ACTION_REPAIR_WHISPER_CPP: (whisper_cpp_runtime, "repair_build"),
    management.ACTION_VERIFY_WHISPER_CPP: (whisper_cpp_runtime, "verify_build"),
    management.ACTION_REMOVE_WHISPER_CPP: (whisper_cpp_runtime, "remove_build"),
}

#: The build each whisper.cpp action acts on in the executor tests: one that
#: makes it a single call. Removing the processor build takes the graphics
#: one with it, so the removal acts on the graphics build; the others on the
#: processor build, which installing never has to put anything under.
_BUILDS = {
    management.ACTION_INSTALL_WHISPER_CPP: whisper_cpp_builds.BUILD_CPU.id,
    management.ACTION_REPAIR_WHISPER_CPP: whisper_cpp_builds.BUILD_CPU.id,
    management.ACTION_VERIFY_WHISPER_CPP: whisper_cpp_builds.BUILD_CPU.id,
    management.ACTION_REMOVE_WHISPER_CPP: whisper_cpp_builds.BUILD_CUDA.id,
}


def test_every_action_has_a_target_and_a_fallback():
    assert set(_TARGETS) == set(management.ACTIONS)
    assert set(management._FALLBACK_CODES) == set(management.ACTIONS)


class _Fake:
    """Stands in for one model_store / cuda_runtime function."""

    def __init__(self, returns=None, raises=None, wait_for_cancel=False, reports=()):
        self.returns = returns
        self.raises = raises
        self.wait_for_cancel = wait_for_cancel
        self.reports = reports
        self.calls = []
        self.started = threading.Event()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.started.set()
        progress = kwargs.get("progress")
        for done, total in self.reports:
            progress(done, total)
        if self.wait_for_cancel:
            should_cancel = kwargs["should_cancel"]
            deadline = time.monotonic() + _JOIN_TIMEOUT
            while not should_cancel():
                if time.monotonic() > deadline:
                    raise AssertionError("the cancel never reached the action")
                time.sleep(0.005)
            # What the real functions raise, from their own _check_cancel().
            raise errors.TranscriptionError(errors.CANCELLED, "cancelled by the user")
        if self.raises is not None:
            raise self.raises
        return self.returns


def _install(monkeypatch, action, fake):
    module, name = _TARGETS[action]
    monkeypatch.setattr(module, name, fake)
    return fake


class _Harness:
    def __init__(self, tmp_path, action, **kwargs):
        self.reports = []
        self.ticks = []
        options = dict(
            models_root=str(tmp_path / "models"),
            model_id="tiny",
            new_models_root=str(tmp_path / "new-models"),
            cuda_directory=str(tmp_path / "cuda"),
            build_id=_BUILDS.get(action),
            runtime_root=str(tmp_path / "runtime"),
            on_progress=self.ticks.append,
            on_finished=lambda result, error: self.reports.append((result, error)),
            clock=lambda: 0.0,
        )
        options.update(kwargs)
        self.job = management.ManagementJob(action, **options)

    def run(self):
        self.job.start()
        self.job.join(_JOIN_TIMEOUT)
        assert not self.job.is_alive(), "the action never finished"
        return self


_RESULTS = {
    management.ACTION_DOWNLOAD_MODEL: "models/tiny",
    management.ACTION_REPAIR_MODEL: "models/tiny",
    management.ACTION_VERIFY_MODEL: None,
    management.ACTION_REMOVE_MODEL: True,
    management.ACTION_MOVE_MODELS: ("tiny",),
    management.ACTION_INSTALL_CUDA_RUNTIME: (True, (), None),
    management.ACTION_REPAIR_CUDA_RUNTIME: (True, (), None),
    management.ACTION_VERIFY_CUDA_RUNTIME: None,
    management.ACTION_REMOVE_CUDA_RUNTIME: (),
    management.ACTION_INSTALL_WHISPER_CPP: None,
    management.ACTION_REPAIR_WHISPER_CPP: None,
    management.ACTION_VERIFY_WHISPER_CPP: None,
    management.ACTION_REMOVE_WHISPER_CPP: (),
}


class TestManagementJob:
    @pytest.fixture(autouse=True)
    def no_vad_download(self, monkeypatch):
        # The program's install also fetches the voice-activity model, over
        # the network; what it does is pinned in TestTheVoiceActivityModel.
        monkeypatch.setattr(management_whisper_cpp, "ensure_vad", lambda *a, **k: None)

    @pytest.mark.parametrize("action", management.ACTIONS)
    def test_success_is_reported_once_with_the_result(self, tmp_path, monkeypatch, action):
        fake = _install(monkeypatch, action, _Fake(returns=_RESULTS[action]))
        harness = _Harness(tmp_path, action).run()
        assert harness.reports == [(_RESULTS[action], None)]
        assert len(fake.calls) == 1
        assert callable(fake.calls[0][1]["should_cancel"])

    @pytest.mark.parametrize("action", management.ACTIONS)
    def test_a_coded_error_is_reported_once_as_it_is(self, tmp_path, monkeypatch, action):
        failure = errors.TranscriptionError(errors.MODELS_BUSY, "held elsewhere")
        _install(monkeypatch, action, _Fake(raises=failure))
        harness = _Harness(tmp_path, action).run()
        assert harness.reports == [(None, failure)]

    @pytest.mark.parametrize("action", management.ACTIONS)
    def test_a_cancel_mid_action_is_reported_once_as_cancelled(self, tmp_path, monkeypatch, action):
        fake = _install(monkeypatch, action, _Fake(wait_for_cancel=True))
        harness = _Harness(tmp_path, action)
        harness.job.start()
        assert fake.started.wait(_JOIN_TIMEOUT)
        harness.job.cancel()
        harness.job.join(_JOIN_TIMEOUT)
        assert not harness.job.is_alive()
        assert len(harness.reports) == 1
        result, error = harness.reports[0]
        assert result is None and error.code == errors.CANCELLED
        assert harness.job.cancelled is True

    @pytest.mark.parametrize("action", management.ACTIONS)
    def test_a_cancel_before_the_thread_runs_touches_nothing(self, tmp_path, monkeypatch, action):
        fake = _install(monkeypatch, action, _Fake(returns=_RESULTS[action]))
        harness = _Harness(tmp_path, action)
        harness.job.cancel()
        harness.run()
        assert fake.calls == []
        assert [e.code for _r, e in harness.reports] == [errors.CANCELLED]

    @pytest.mark.parametrize("action", management.ACTIONS)
    def test_an_unexpected_exception_becomes_the_actions_own_code(
            self, tmp_path, monkeypatch, action):
        _install(monkeypatch, action,
                 _Fake(raises=KeyError(r"C:\somewhere\3EB0FEEDFACE0099.wzmedia")))
        harness = _Harness(tmp_path, action).run()
        assert len(harness.reports) == 1
        result, error = harness.reports[0]
        assert result is None
        assert error.code == management._FALLBACK_CODES[action]
        # The technical text is for log.log, never for the sentence — and
        # even there without a media file's name, the message id
        # (errors.scrub_media_names()). The folder stays: it names nobody.
        assert "somewhere" not in str(error)
        assert "KeyError" in error.log_line
        assert "3EB0FEEDFACE0099" not in error.log_line
        assert "somewhere" in error.log_line and "<message id>.wzmedia" in error.log_line

    @pytest.mark.parametrize("action", [
        management.ACTION_VERIFY_MODEL, management.ACTION_REMOVE_MODEL,
        management.ACTION_VERIFY_CUDA_RUNTIME, management.ACTION_REMOVE_CUDA_RUNTIME,
    ])
    def test_a_bug_in_a_check_or_a_removal_never_blames_the_files(self, action):
        # "Your files are damaged, download them again" after a check that
        # never finished costs up to 3 GB for nothing.
        assert management._FALLBACK_CODES[action] not in (
            errors.MODEL_CORRUPTED, errors.CUDA_RUNTIME_CORRUPTED,
            errors.MODEL_DOWNLOAD_FAILED, errors.CUDA_RUNTIME_DOWNLOAD_FAILED,
        )

    def test_progress_reaches_the_ui_throttled(self, tmp_path, monkeypatch):
        reports = [(done, 3000) for done in range(0, 3001)]
        _install(monkeypatch, management.ACTION_DOWNLOAD_MODEL,
                 _Fake(returns="dir", reports=reports))
        harness = _Harness(tmp_path, management.ACTION_DOWNLOAD_MODEL).run()
        # The frozen clock keeps the interval from ever elapsing: what gets
        # through is the first report, the three spoken quarters and the bar
        # reaching the end.
        assert [t.percent for t in harness.ticks] == [0, 25, 50, 75, 100]
        assert [t.percent for t in harness.ticks if t.speak] == [25, 50, 75]
        assert len(harness.reports) == 1

    @pytest.mark.parametrize("reports", [
        # Straight to the end, as a same-volume move or an install that
        # finds everything already present reports it.
        lambda total: [(0, total), (total, total)],
        # A whole transfer, megabyte by megabyte.
        lambda total: [(done, total) for done in range(0, total + 1, total // 400)]
                      + [(total, total)],
    ])
    def test_the_end_is_not_spoken_while_the_install_is_still_probing(
            self, tmp_path, monkeypatch, reports):
        # cuda_runtime reports INSTALL_BYTES/INSTALL_BYTES and only then
        # registers the directory and loads cuBLAS. Nothing may say "100%"
        # in that window — the finished report is the end.
        total = cuda_runtime.INSTALL_BYTES
        reported = threading.Event()
        release = threading.Event()

        def install(*_args, progress=None, should_cancel=None, **_kwargs):
            for done, of in reports(total):
                progress(done, of)
            reported.set()
            release.wait(_JOIN_TIMEOUT)  # the probe loading cuBLAS
            return (False, ("cublas64_12.dll",), "could not load")

        monkeypatch.setattr(cuda_runtime, "install_cuda_runtime", install)
        harness = _Harness(tmp_path, management.ACTION_INSTALL_CUDA_RUNTIME)
        harness.job.start()
        try:
            assert reported.wait(_JOIN_TIMEOUT)
            before = list(harness.ticks)
            assert harness.reports == []
            assert not any(t.speak and t.percent == 100 for t in before)
            assert before[-1].percent == 100 and before[-1].update_bar
        finally:
            release.set()
            harness.job.join(_JOIN_TIMEOUT)
        assert len(harness.reports) == 1
        assert harness.ticks == before

    def test_a_raising_progress_callback_costs_neither_the_action_nor_the_report(
            self, tmp_path, monkeypatch):
        escaped = []
        monkeypatch.setattr(threading, "excepthook", escaped.append)
        _install(monkeypatch, management.ACTION_VERIFY_MODEL,
                 _Fake(returns=None, reports=[(0, 10), (10, 10)]))

        def explode(_tick):
            raise RuntimeError("the UI broke")

        harness = _Harness(tmp_path, management.ACTION_VERIFY_MODEL, on_progress=explode).run()
        assert harness.reports == [(None, None)]
        assert escaped == []

    def test_a_raising_finished_callback_is_swallowed(self, tmp_path, monkeypatch):
        # An exception escaping a thread is only a warning to pytest, so the
        # hook is what proves the guard is there.
        escaped = []
        monkeypatch.setattr(threading, "excepthook", escaped.append)
        _install(monkeypatch, management.ACTION_REMOVE_CUDA_RUNTIME, _Fake(returns=()))
        calls = []

        def explode(result, error):
            calls.append((result, error))
            raise RuntimeError("the UI broke")

        harness = _Harness(tmp_path, management.ACTION_REMOVE_CUDA_RUNTIME, on_finished=explode)
        harness.run()
        assert calls == [((), None)]
        assert escaped == []

    def test_the_arguments_reach_the_store(self, tmp_path, monkeypatch):
        fake = _install(monkeypatch, management.ACTION_DOWNLOAD_MODEL, _Fake(returns="d"))
        session = object()
        _Harness(tmp_path, management.ACTION_DOWNLOAD_MODEL, model_id="base",
                 session=session).run()
        args, kwargs = fake.calls[0]
        assert args == (model_catalog.get_model("base"), str(tmp_path / "models"))
        assert kwargs["session"] is session
        assert callable(kwargs["progress"])

    def test_the_move_goes_from_the_old_root_to_the_new(self, tmp_path, monkeypatch):
        fake = _install(monkeypatch, management.ACTION_MOVE_MODELS, _Fake(returns=()))
        _Harness(tmp_path, management.ACTION_MOVE_MODELS).run()
        args, _kwargs = fake.calls[0]
        assert args == (str(tmp_path / "models"), str(tmp_path / "new-models"))

    def test_the_cuda_directory_reaches_the_runtime(self, tmp_path, monkeypatch):
        fake = _install(monkeypatch, management.ACTION_REMOVE_CUDA_RUNTIME, _Fake(returns=()))
        _Harness(tmp_path, management.ACTION_REMOVE_CUDA_RUNTIME).run()
        assert fake.calls[0][0] == (str(tmp_path / "cuda"),)

    @pytest.mark.parametrize("action", [
        management.ACTION_DOWNLOAD_MODEL, management.ACTION_REPAIR_MODEL,
        management.ACTION_VERIFY_MODEL,
    ])
    def test_an_unknown_model_id_is_not_installed_rather_than_a_network_fault(
            self, tmp_path, monkeypatch, action):
        fake = _install(monkeypatch, action, _Fake(returns="d"))
        harness = _Harness(tmp_path, action, model_id="retired-model").run()
        assert fake.calls == []
        assert [e.code for _r, e in harness.reports] == [errors.MODEL_NOT_INSTALLED]

    def test_the_move_remembers_what_was_in_the_old_folder(self, tmp_path, monkeypatch):
        old = tmp_path / "models"
        (old / "tiny").mkdir(parents=True)
        (old / "tiny" / "config.json").write_bytes(b"x")  # an incomplete model
        (old / "base").mkdir()
        (old / "base" / "model.bin").write_bytes(b"x")
        _install(monkeypatch, management.ACTION_MOVE_MODELS, _Fake(returns=("tiny", "base")))
        harness = _Harness(tmp_path, management.ACTION_MOVE_MODELS).run()
        assert set(harness.job.models_before_move) == {"tiny", "base"}

    def test_an_unknown_action_is_refused_on_the_callers_thread(self):
        with pytest.raises(ValueError):
            management.ManagementJob("format_the_disk")

    def test_nothing_is_retried_on_its_own(self, tmp_path, monkeypatch):
        fake = _install(
            monkeypatch, management.ACTION_INSTALL_CUDA_RUNTIME,
            _Fake(raises=errors.TranscriptionError(errors.CUDA_RUNTIME_DOWNLOAD_FAILED)),
        )
        _Harness(tmp_path, management.ACTION_INSTALL_CUDA_RUNTIME).run()
        assert len(fake.calls) == 1


# ── Probing off the UI thread ────────────────────────────────────────────────


class _Recorder:
    """Every call to the patched functions, in the order they were made."""

    def __init__(self):
        self.calls = []

    def fake(self, name, returns=None):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return returns(*args) if callable(returns) else returns
        return record

    def names(self):
        return [(name, args[0]) for name, args, _kwargs in self.calls]


def _runtime_state(installed):
    def state(build, root=None):
        if build in installed:
            return whisper_cpp_runtime.RuntimeState(whisper_cpp_runtime.STATE_INSTALLED)
        return whisper_cpp_runtime.RuntimeState(whisper_cpp_runtime.STATE_ABSENT)
    return state


class TestWhisperCppSteps:
    """The program's builds: the processor one is the floor under the other."""

    @pytest.fixture
    def recorder(self, monkeypatch):
        recorder = _Recorder()
        for name in ("install_build", "repair_build", "verify_build"):
            monkeypatch.setattr(whisper_cpp_runtime, name, recorder.fake(name))
        monkeypatch.setattr(whisper_cpp_runtime, "remove_build",
                            recorder.fake("remove_build", lambda build, *_: (build.id,)))
        # Kept apart from the program's calls, which the order tests compare.
        recorder.vad = []
        monkeypatch.setattr(
            management_whisper_cpp, "ensure_vad",
            lambda root, session, cancel, check=False, strict=True: recorder.vad.append(
                (root, check, strict, [name for name, *_ in recorder.calls])),
        )
        return recorder

    def _run(self, tmp_path, action, build, **kwargs):
        harness = _Harness(tmp_path, action, build_id=build.id, **kwargs).run()
        (report,) = harness.reports
        return report

    def test_the_graphics_build_installs_the_processor_one_first(
        self, tmp_path, monkeypatch, recorder
    ):
        monkeypatch.setattr(whisper_cpp_runtime, "installation_state", _runtime_state(()))
        result, error = self._run(tmp_path, management.ACTION_INSTALL_WHISPER_CPP, _CUDA,
                                  compute_capability=(8, 6))
        assert error is None and result is None
        assert recorder.names() == [("install_build", _CPU), ("install_build", _CUDA)]
        # The card reaches both installs: the runtime refuses the graphics
        # build on a card it cannot run on.
        assert {kwargs["compute_capability"] for *_, kwargs in recorder.calls} == {(8, 6)}

    def test_the_graphics_build_beside_the_processor_one_installs_alone(
        self, tmp_path, monkeypatch, recorder
    ):
        monkeypatch.setattr(whisper_cpp_runtime, "installation_state",
                            _runtime_state((_CPU,)))
        self._run(tmp_path, management.ACTION_INSTALL_WHISPER_CPP, _CUDA)
        assert recorder.names() == [("install_build", _CUDA)]

    def test_a_repair_of_the_graphics_build_installs_a_missing_processor_one(
        self, tmp_path, monkeypatch, recorder
    ):
        monkeypatch.setattr(whisper_cpp_runtime, "installation_state", _runtime_state(()))
        self._run(tmp_path, management.ACTION_REPAIR_WHISPER_CPP, _CUDA)
        assert recorder.names() == [("install_build", _CPU), ("repair_build", _CUDA)]

    def test_the_progress_of_two_installs_is_one_bar(self, tmp_path, monkeypatch):
        monkeypatch.setattr(whisper_cpp_runtime, "installation_state", _runtime_state(()))
        monkeypatch.setattr(management_whisper_cpp, "ensure_vad", lambda *a, **k: None)

        def install(build, root=None, progress=None, **_kwargs):
            progress(build.archive_bytes * 2, build.archive_bytes * 2)

        monkeypatch.setattr(whisper_cpp_runtime, "install_build", install)
        harness = _Harness(tmp_path, management.ACTION_INSTALL_WHISPER_CPP,
                           build_id=_CUDA.id)
        seen = []
        harness.job._report_progress = lambda done, total: seen.append((done, total))
        harness.run()
        total = (_CPU.archive_bytes + _CUDA.archive_bytes) * 2
        # Never back to zero between the two: a bar that empties halfway is
        # heard as a download that started over.
        assert seen == [(_CPU.archive_bytes * 2, total), (total, total)]

    def test_removing_the_processor_build_removes_the_graphics_one_first(
        self, tmp_path, recorder
    ):
        result, error = self._run(tmp_path, management.ACTION_REMOVE_WHISPER_CPP, _CPU)
        assert error is None
        assert recorder.names() == [("remove_build", _CUDA), ("remove_build", _CPU)]
        # What stayed open in either is reported, so the restart advice covers both.
        assert result == (_CUDA.id, _CPU.id)

    def test_removing_the_graphics_build_leaves_the_processor_one(self, tmp_path, recorder):
        self._run(tmp_path, management.ACTION_REMOVE_WHISPER_CPP, _CUDA)
        assert recorder.names() == [("remove_build", _CUDA)]

    def test_a_check_touches_only_the_build_checked(self, tmp_path, recorder):
        self._run(tmp_path, management.ACTION_VERIFY_WHISPER_CPP, _CUDA)
        assert recorder.names() == [("verify_build", _CUDA)]

    def test_an_unknown_build_is_not_installed(self, tmp_path, recorder):
        harness = _Harness(tmp_path, management.ACTION_INSTALL_WHISPER_CPP,
                           build_id="nonsense").run()
        ((_result, error),) = harness.reports
        assert error.code == errors.WHISPER_CPP_NOT_INSTALLED
        assert recorder.calls == [] and recorder.vad == []

    @pytest.mark.parametrize("action, check", [
        (management.ACTION_INSTALL_WHISPER_CPP, False),
        (management.ACTION_REPAIR_WHISPER_CPP, True),
    ])
    def test_installing_the_program_brings_the_voice_activity_model_after_it(
        self, tmp_path, monkeypatch, recorder, action, check
    ):
        # A user who only adds a GGML file of their own never downloads a
        # GGML model, and the filter would otherwise never arrive. After the
        # program and not strict: see the next test.
        monkeypatch.setattr(whisper_cpp_runtime, "installation_state",
                            _runtime_state((_CPU,)))
        self._run(tmp_path, action, _CPU)
        step = "install_build" if action == management.ACTION_INSTALL_WHISPER_CPP \
            else "repair_build"
        assert recorder.vad == [(str(tmp_path / "models"), check, False, [step])]

    def test_hugging_face_out_of_reach_does_not_stop_the_install(
        self, tmp_path, monkeypatch, recorder, caplog
    ):
        """The program comes from GitHub, the filter from Hugging Face. A
        network that blocks the second must not cost a user who brings their
        own GGML file the first."""
        monkeypatch.setattr(management_whisper_cpp, "ensure_vad", _REAL_ENSURE_VAD)
        monkeypatch.setattr(whisper_cpp_runtime, "installation_state",
                            _runtime_state((_CPU,)))
        monkeypatch.setattr(model_store, "installation_state",
                            lambda root, model: model_store.InstallState(
                                model_store.STATE_ABSENT))

        def unreachable(model, root, **_kwargs):
            raise errors.TranscriptionError(errors.MODEL_DOWNLOAD_FAILED, "blocked")

        monkeypatch.setattr(model_store, "download_model", unreachable)
        with caplog.at_level(logging.WARNING):
            result, error = self._run(tmp_path, management.ACTION_INSTALL_WHISPER_CPP, _CPU)
        assert error is None and result is None
        assert recorder.names() == [("install_build", _CPU)]
        assert "voice-activity model was left as it is" in caplog.text

    def test_a_cancel_while_fetching_the_filter_is_still_a_cancel(
        self, tmp_path, monkeypatch, recorder
    ):
        monkeypatch.setattr(management_whisper_cpp, "ensure_vad", _REAL_ENSURE_VAD)
        monkeypatch.setattr(whisper_cpp_runtime, "installation_state",
                            _runtime_state((_CPU,)))
        monkeypatch.setattr(model_store, "installation_state",
                            lambda root, model: model_store.InstallState(
                                model_store.STATE_ABSENT))

        def cancelled(model, root, **_kwargs):
            raise errors.TranscriptionError(errors.CANCELLED, "by the user")

        monkeypatch.setattr(model_store, "download_model", cancelled)
        _result, error = self._run(tmp_path, management.ACTION_INSTALL_WHISPER_CPP, _CPU)
        assert error.code == errors.CANCELLED

    def test_a_check_of_an_older_install_fetches_it_on_the_side(self, tmp_path, recorder):
        self._run(tmp_path, management.ACTION_VERIFY_WHISPER_CPP, _CPU)
        assert recorder.vad == [(str(tmp_path / "models"), True, False, ["verify_build"])]

    def test_removing_the_program_leaves_it(self, tmp_path, recorder):
        self._run(tmp_path, management.ACTION_REMOVE_WHISPER_CPP, _CPU)
        assert recorder.vad == []


class TestGgmlSteps:
    """A GGML model brings the voice-activity model with it, first."""

    @pytest.fixture
    def recorder(self, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr(model_store, "download_model", recorder.fake("download_model"))
        monkeypatch.setattr(model_store, "repair_model", recorder.fake("repair_model"))
        monkeypatch.setattr(model_store, "verify_model", recorder.fake("verify_model"))
        return recorder

    def _run(self, tmp_path, action):
        harness = _Harness(tmp_path, action, model_id=_GGML_ID).run()
        ((_result, error),) = harness.reports
        assert error is None

    @staticmethod
    def _vad_present(monkeypatch, present):
        vad = whisper_cpp_catalog.VAD_MODEL
        monkeypatch.setattr(model_store, "is_installed",
                            lambda root, model: present and model is vad)
        state = model_store.STATE_INSTALLED if present else model_store.STATE_ABSENT
        monkeypatch.setattr(model_store, "installation_state",
                            lambda root, model: model_store.InstallState(state))

    def test_a_download_fetches_the_voice_activity_model_first(
        self, tmp_path, monkeypatch, recorder
    ):
        self._vad_present(monkeypatch, False)
        self._run(tmp_path, management.ACTION_DOWNLOAD_MODEL)
        vad, model = whisper_cpp_catalog.VAD_MODEL, whisper_cpp_catalog.get_model(_GGML_ID)
        assert [(name, args[0]) for name, args, _ in recorder.calls] == [
            ("download_model", vad), ("download_model", model)
        ]

    def test_a_download_does_not_fetch_it_twice(self, tmp_path, monkeypatch, recorder):
        self._vad_present(monkeypatch, True)
        self._run(tmp_path, management.ACTION_DOWNLOAD_MODEL)
        assert [(name, args[0]) for name, args, _ in recorder.calls] == [
            ("download_model", whisper_cpp_catalog.get_model(_GGML_ID))
        ]

    def test_a_repair_repairs_both(self, tmp_path, monkeypatch, recorder):
        self._vad_present(monkeypatch, True)
        self._run(tmp_path, management.ACTION_REPAIR_MODEL)
        vad, model = whisper_cpp_catalog.VAD_MODEL, whisper_cpp_catalog.get_model(_GGML_ID)
        assert [(name, args[0]) for name, args, _ in recorder.calls] == [
            ("repair_model", vad), ("repair_model", model)
        ]

    @pytest.mark.parametrize("present", [True, False])
    def test_a_check_checks_the_voice_activity_model_or_fetches_it(
        self, tmp_path, monkeypatch, recorder, present
    ):
        self._vad_present(monkeypatch, present)
        self._run(tmp_path, management.ACTION_VERIFY_MODEL)
        vad, model = whisper_cpp_catalog.VAD_MODEL, whisper_cpp_catalog.get_model(_GGML_ID)
        calls = [(name, args[1] if name == "verify_model" else args[0])
                 for name, args, _ in recorder.calls]
        assert calls == ([("verify_model", model), ("verify_model", vad)] if present
                         else [("verify_model", model), ("download_model", vad)])

    def test_a_damaged_filter_is_not_the_checked_model_being_damaged(
        self, tmp_path, monkeypatch, recorder
    ):
        self._vad_present(monkeypatch, True)
        vad = whisper_cpp_catalog.VAD_MODEL
        record = recorder.fake("verify_model")

        def verify(root, model, **kwargs):
            record(root, model, **kwargs)
            if model is vad:
                raise errors.TranscriptionError(errors.MODEL_CORRUPTED, "vad digest")

        monkeypatch.setattr(model_store, "verify_model", verify)
        self._run(tmp_path, management.ACTION_VERIFY_MODEL)
        # Mended on the side; the check of the model itself reports success.
        assert ("repair_model", vad) in [(name, args[0]) for name, args, _ in recorder.calls]


class TestTheVoiceActivityModel:
    """ensure_vad(): fetch it when absent, mend it when a check finds it
    damaged — and, for a check, never turn a missing filter into a failure."""

    @pytest.fixture
    def store(self, monkeypatch):
        recorder = _Recorder()
        recorder.state = model_store.STATE_ABSENT
        recorder.download_error = None
        recorder.verify_error = None
        monkeypatch.setattr(model_store, "installation_state",
                            lambda root, model: model_store.InstallState(recorder.state))

        def download(model, root, **kwargs):
            recorder.calls.append(("download_model", (model, root), kwargs))
            if recorder.download_error is not None:
                raise recorder.download_error

        def verify(root, model, **kwargs):
            recorder.calls.append(("verify_model", (model, root), kwargs))
            if recorder.verify_error is not None:
                raise recorder.verify_error

        monkeypatch.setattr(model_store, "download_model", download)
        monkeypatch.setattr(model_store, "verify_model", verify)
        monkeypatch.setattr(model_store, "repair_model", recorder.fake("repair_model"))
        return recorder

    @staticmethod
    def _names(store):
        return [name for name, *_ in store.calls]

    def test_absent_it_is_downloaded_into_the_models_folder(self, tmp_path, store):
        management_whisper_cpp.ensure_vad(str(tmp_path), None, lambda: False)
        ((name, (model, root), _kwargs),) = store.calls
        assert (name, model, root) == (
            "download_model", whisper_cpp_catalog.VAD_MODEL, str(tmp_path))

    def test_present_and_not_asked_to_check_nothing_happens(self, tmp_path, store):
        store.state = model_store.STATE_INSTALLED
        management_whisper_cpp.ensure_vad(str(tmp_path), None, lambda: False)
        assert store.calls == []

    def test_a_check_mends_a_damaged_one(self, tmp_path, store):
        store.state = model_store.STATE_INSTALLED
        store.verify_error = errors.TranscriptionError(errors.MODEL_CORRUPTED, "digest")
        management_whisper_cpp.ensure_vad(str(tmp_path), None, lambda: False, check=True)
        assert self._names(store) == ["verify_model", "repair_model"]

    def test_offline_a_check_carries_on_and_a_model_download_says_so(self, tmp_path, store):
        store.download_error = errors.TranscriptionError(errors.MODEL_DOWNLOAD_FAILED, "offline")
        management_whisper_cpp.ensure_vad(str(tmp_path), None, lambda: False,
                                          check=True, strict=False)
        with pytest.raises(errors.TranscriptionError) as caught:
            management_whisper_cpp.ensure_vad(str(tmp_path), None, lambda: False)
        assert caught.value.code == errors.MODEL_DOWNLOAD_FAILED

    def test_a_cancel_is_a_cancel_even_for_a_check(self, tmp_path, store):
        store.download_error = errors.TranscriptionError(errors.CANCELLED, "by the user")
        with pytest.raises(errors.TranscriptionError) as caught:
            management_whisper_cpp.ensure_vad(str(tmp_path), None, lambda: False,
                                              check=True, strict=False)
        assert caught.value.code == errors.CANCELLED

    def test_without_a_models_folder_nothing_is_touched(self, store):
        management_whisper_cpp.ensure_vad(None, None, lambda: False)
        assert store.calls == []


class TestProbeInBackground:
    def test_the_answer_arrives_by_callback_from_another_thread(self):
        answer = _card()
        received = []
        done = threading.Event()

        def on_done(probe):
            received.append((probe, threading.get_ident()))
            done.set()

        thread = management.probe_in_background(on_done, probe=lambda: answer)
        assert done.wait(_JOIN_TIMEOUT)
        thread.join(_JOIN_TIMEOUT)
        assert received[0][0] is answer
        assert received[0][1] != threading.get_ident()
        assert len(received) == 1

    def test_the_caller_is_not_held_while_the_probe_runs(self):
        release = threading.Event()

        def slow_probe():
            release.wait(_JOIN_TIMEOUT)
            return _no_card()

        received = []
        thread = management.probe_in_background(received.append, probe=slow_probe)
        # Back here while the probe is still blocked — the UI thread is free.
        assert received == []
        release.set()
        thread.join(_JOIN_TIMEOUT)
        assert len(received) == 1

    def test_a_probe_that_raises_still_answers_once_with_unknowns(self):
        def broken():
            raise OSError(errno.EIO, "driver fault")

        received = []
        thread = management.probe_in_background(received.append, probe=broken)
        thread.join(_JOIN_TIMEOUT)
        assert len(received) == 1
        assert isinstance(received[0], device.HardwareProbe)
        assert received[0].cuda_device_count == 0
        assert received[0].available_ram_mb is None

    def test_a_raising_callback_does_not_escape_the_thread(self, monkeypatch):
        escaped = []
        monkeypatch.setattr(threading, "excepthook", escaped.append)

        def explode(_probe):
            raise RuntimeError("the UI broke")

        thread = management.probe_in_background(explode, probe=_no_card)
        thread.join(_JOIN_TIMEOUT)
        assert not thread.is_alive()
        assert escaped == []

    def test_the_default_is_the_real_probe(self, monkeypatch):
        monkeypatch.setattr(device, "probe_hardware", lambda: "measured")
        received = []
        management.probe_in_background(received.append).join(_JOIN_TIMEOUT)
        assert received == ["measured"]


# ── Outcome to sentence ──────────────────────────────────────────────────────


def _move_error(code, moved):
    exc = errors.TranscriptionError(code, "detail")
    exc.moved = tuple(moved)
    return exc


class TestAnnouncement:
    @pytest.mark.parametrize("action, result, key", [
        (management.ACTION_DOWNLOAD_MODEL, "dir", management.MODEL_DOWNLOADED_I18N_KEY),
        (management.ACTION_REPAIR_MODEL, "dir", management.MODEL_REPAIRED_I18N_KEY),
        (management.ACTION_VERIFY_MODEL, None, management.MODEL_VERIFIED_I18N_KEY),
        (management.ACTION_REMOVE_MODEL, True, management.MODEL_REMOVED_I18N_KEY),
        (management.ACTION_REMOVE_MODEL, False, management.MODEL_NOTHING_TO_REMOVE_I18N_KEY),
        (management.ACTION_MOVE_MODELS, ("tiny",), management.MODELS_MOVED_I18N_KEY),
        (management.ACTION_MOVE_MODELS, (), management.MODELS_NOTHING_TO_MOVE_I18N_KEY),
        (management.ACTION_INSTALL_CUDA_RUNTIME, (True, (), None), management.CUDA_INSTALLED_I18N_KEY),
        (management.ACTION_REPAIR_CUDA_RUNTIME, (True, (), None), management.CUDA_INSTALLED_I18N_KEY),
        (management.ACTION_VERIFY_CUDA_RUNTIME, None, management.CUDA_VERIFIED_I18N_KEY),
        (management.ACTION_REMOVE_CUDA_RUNTIME, (), management.CUDA_REMOVED_I18N_KEY),
    ])
    def test_every_success(self, action, result, key):
        said = management.announcement(action, result, None, model_id="tiny")
        assert said.i18n_key == key
        assert said.outcome == management.OUTCOME_DONE

    def test_the_model_is_named(self):
        said = management.announcement(management.ACTION_DOWNLOAD_MODEL, "d", None,
                                       model_id="large-v3")
        assert said.values == {"model": "large-v3"}

    def test_libraries_that_resisted_removal_ask_for_a_restart(self):
        said = management.announcement(
            management.ACTION_REMOVE_CUDA_RUNTIME, ("cublas64_12.dll", "cublasLt64_12.dll")
        )
        assert said.i18n_key == management.CUDA_REMOVE_NEEDS_RESTART_I18N_KEY
        assert said.outcome == management.OUTCOME_WARNING

    @pytest.mark.parametrize("action", [
        management.ACTION_INSTALL_CUDA_RUNTIME, management.ACTION_REPAIR_CUDA_RUNTIME,
    ])
    def test_installed_but_still_unusable_has_its_own_sentence(self, action):
        said = management.announcement(
            action, (False, ("cublas64_12.dll",), "could not load")
        )
        assert said.i18n_key == management.CUDA_INSTALLED_NOT_USABLE_I18N_KEY
        assert said.outcome == management.OUTCOME_WARNING

    def test_an_unanswerable_probe_is_not_claimed_as_working(self):
        said = management.announcement(management.ACTION_INSTALL_CUDA_RUNTIME, (None, (), None))
        assert said.i18n_key == management.CUDA_INSTALLED_UNVERIFIED_I18N_KEY

    #: The stems each locale uses for "settings" — every one its own file uses,
    #: which is why pt-PT carries both its dialog's word and its menu's, and ro
    #: both "setări" and "configurare". Keyed by locale for the same reason as
    #: _DOWNLOAD_WORD below.
    _SETTINGS_WORDS = {
        "pt-BR": ("configura",), "pt-PT": ("definições", "configura"),
        "en-US": ("settings",), "es-ES": ("configura",), "pl": ("ustawieni",),
        "ro": ("setăr", "configur"), "tr-TR": ("ayar",), "ar": ("إعدادات", "اعدادات"),
        "it": ("impostazion",),
    }

    @pytest.mark.parametrize("locale", LOCALES)
    def test_the_unusable_sentence_does_not_send_the_user_back_to_the_settings(self, locale):
        # The circular advice: "download them in the transcription settings" is
        # the screen the user is on and the button they just pressed.
        assert locale in self._SETTINGS_WORDS, (
            f"{locale} is new here: add the words it uses for the settings"
        )
        table = load_strings(locale)
        text = table[management.CUDA_INSTALLED_NOT_USABLE_I18N_KEY]
        assert words_found_in(text, self._SETTINGS_WORDS[locale]) == []
        assert text.lower() != table["transcription_device_cuda_libraries_missing"].lower()

    #: The stem each locale uses for "downloaded". Keyed by locale and checked
    #: against language_map rather than parametrized over a written-out list,
    #: which is what let ro and tr-TR arrive without being checked at all.
    _DOWNLOAD_WORD = {
        "pt-BR": "baixad", "pt-PT": "transferid", "en-US": "download",
        "es-ES": "descargad", "pl": "pobran", "ro": "descărca", "tr-TR": "indir", "ar": "تنزيل", "it": "scaric",
    }

    @pytest.mark.parametrize("locale", LOCALES)
    def test_the_unusable_sentence_does_not_claim_a_download(self, locale):
        # install_cuda_runtime() downloads nothing when the libraries are
        # already on disk and the probe still says no.
        assert locale in self._DOWNLOAD_WORD, (
            f"{locale} is new here: add the word it uses for a download"
        )
        text = load_strings(locale)[management.CUDA_INSTALLED_NOT_USABLE_I18N_KEY]
        assert words_found_in(text, (self._DOWNLOAD_WORD[locale],)) == []

    def test_the_word_search_sees_a_turkish_capital_i(self):
        # The hole the two checks above had with str.lower(): "İ".lower() keeps
        # a combining dot, so a sentence opening "İndir…" never contained
        # "indir". Pinned here so the helper cannot regress to lower() quietly.
        assert "indir" not in "İndirildi".lower()
        assert words_found_in("İndirildi.", ("indir",)) == ["indir"]
        assert words_found_in("DEFINIÇÕES", ("definições",)) == ["definições"]

    def test_a_partial_move_says_what_went_and_what_stayed(self):
        said = management.announcement(
            management.ACTION_MOVE_MODELS,
            error=_move_error(errors.MODEL_MOVE_FAILED, ["tiny", "base"]),
            models_before_move=("tiny", "base", "small", "large-v3"),
        )
        assert said.i18n_key == management.MODELS_MOVE_PARTIAL_I18N_KEY
        assert said.outcome == management.OUTCOME_FAILED
        assert said.values == {"moved": "tiny, base", "remaining": "small, large-v3"}

    def test_the_voice_activity_model_moves_unnamed(self):
        vad = whisper_cpp_catalog.VAD_MODEL.id
        said = management.announcement(management.ACTION_MOVE_MODELS, (vad, "ggml-small"))
        assert said.values == {"models": "ggml-small"}
        # A folder that held only the filter had no models to move.
        alone = management.announcement(management.ACTION_MOVE_MODELS, (vad,))
        assert alone.i18n_key == management.MODELS_NOTHING_TO_MOVE_I18N_KEY
        partial = management.announcement(
            management.ACTION_MOVE_MODELS,
            error=_move_error(errors.MODEL_MOVE_FAILED, [vad, "tiny"]),
            models_before_move=(vad, "tiny", "ggml-base"),
        )
        assert partial.values == {"moved": "tiny", "remaining": "ggml-base"}

    def test_a_partial_move_out_of_space_repeats_the_reason(self):
        said = management.announcement(
            management.ACTION_MOVE_MODELS,
            error=_move_error(errors.NO_DISK_SPACE, ["tiny"]),
            models_before_move=("tiny", "large-v3"),
        )
        assert said.i18n_key == management.MODELS_MOVE_PARTIAL_NO_SPACE_I18N_KEY
        assert said.values == {"moved": "tiny", "remaining": "large-v3"}

    def test_a_move_out_of_space_does_not_talk_about_a_download(self):
        said = management.announcement(
            management.ACTION_MOVE_MODELS,
            error=_move_error(errors.NO_DISK_SPACE, []),
            models_before_move=("tiny",),
        )
        assert said.i18n_key == management.MODELS_MOVE_NO_SPACE_I18N_KEY
        assert said.outcome == management.OUTCOME_FAILED

    def test_a_download_out_of_space_keeps_the_stock_sentence(self):
        said = management.announcement(
            management.ACTION_DOWNLOAD_MODEL,
            error=errors.TranscriptionError(errors.NO_DISK_SPACE),
        )
        assert said.i18n_key == "transcription_error_no_disk_space"

    def test_a_cancelled_partial_move_is_cancelled_and_still_says_where_things_are(self):
        said = management.announcement(
            management.ACTION_MOVE_MODELS,
            error=_move_error(errors.CANCELLED, ["tiny"]),
            models_before_move=("tiny", "medium"),
        )
        assert said.i18n_key == management.MODELS_MOVE_CANCELLED_PARTIAL_I18N_KEY
        assert said.outcome == management.OUTCOME_CANCELLED
        assert said.values == {"moved": "tiny", "remaining": "medium"}

    def test_a_move_that_moved_nothing_keeps_the_errors_own_sentence(self):
        said = management.announcement(
            management.ACTION_MOVE_MODELS,
            error=_move_error(errors.MODEL_MOVE_FAILED, []),
            models_before_move=("tiny",),
        )
        assert said.i18n_key == errors.error_i18n_key(errors.MODEL_MOVE_FAILED)

    def test_a_move_that_moved_everything_and_raised_is_not_told_they_stayed(self):
        said = management.announcement(
            management.ACTION_MOVE_MODELS,
            error=_move_error(errors.MODEL_MOVE_FAILED, ["tiny"]),
            models_before_move=("tiny",),
        )
        assert said.i18n_key == management.FAILED_I18N_KEY

    @pytest.mark.parametrize("action", [
        management.ACTION_INSTALL_CUDA_RUNTIME, management.ACTION_REPAIR_CUDA_RUNTIME,
    ])
    def test_libraries_in_use_ask_for_a_restart(self, action):
        said = management.announcement(
            action, error=errors.TranscriptionError(errors.CUDA_RUNTIME_IN_USE, "mapped")
        )
        assert said.i18n_key == "transcription_error_cuda_runtime_in_use"
        assert said.outcome == management.OUTCOME_FAILED

    @pytest.mark.parametrize("action", management.ACTIONS)
    def test_cancelled_is_never_a_failure(self, action):
        said = management.announcement(
            action, error=errors.TranscriptionError(errors.CANCELLED)
        )
        assert said.i18n_key == management.CANCELLED_I18N_KEY
        assert said.outcome == management.OUTCOME_CANCELLED
        # And never the transcription's own "transcription cancelled".
        assert said.i18n_key != errors.error_i18n_key(errors.CANCELLED)

    def test_an_internal_error_does_not_mention_a_transcription(self):
        said = management.announcement(
            management.ACTION_VERIFY_MODEL,
            error=errors.TranscriptionError(errors.BACKEND_ERROR, "boom"),
        )
        assert said.i18n_key == management.FAILED_I18N_KEY
        assert said.outcome == management.OUTCOME_FAILED

    def test_the_job_passes_its_own_context(self, tmp_path):
        job = management.ManagementJob(
            management.ACTION_MOVE_MODELS, models_root=str(tmp_path), new_models_root="x"
        )
        job.models_before_move = ("tiny", "base")
        said = job.announcement(None, _move_error(errors.MODEL_MOVE_FAILED, ["tiny"]))
        assert said.values["remaining"] == "base"


class TestAnnouncementTranslations:
    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_key_exists_in_every_locale(self, locale):
        table = load_strings(locale)
        keys = management.ANNOUNCEMENT_I18N_KEYS + management_whisper_cpp.ANNOUNCEMENT_I18N_KEYS
        missing = [k for k in keys if not table.get(k, "").strip()]
        assert missing == [], f"{locale} catalog would read these key names aloud: {missing}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_sentence_formats_with_the_values_it_is_given(self, locale):
        # str.format() raises KeyError on a placeholder nobody passes, which in
        # the tab would be a handler dying after the action already finished.
        table = load_strings(locale)
        samples = [
            management.announcement(management.ACTION_DOWNLOAD_MODEL, "d", model_id="tiny"),
            management.announcement(management.ACTION_REPAIR_MODEL, "d", model_id="tiny"),
            management.announcement(management.ACTION_VERIFY_MODEL, None, model_id="tiny"),
            management.announcement(management.ACTION_REMOVE_MODEL, True, model_id="tiny"),
            management.announcement(management.ACTION_REMOVE_MODEL, False, model_id="tiny"),
            management.announcement(management.ACTION_MOVE_MODELS, ("tiny",)),
            management.announcement(management.ACTION_MOVE_MODELS, ()),
            management.announcement(
                management.ACTION_MOVE_MODELS,
                error=_move_error(errors.MODEL_MOVE_FAILED, ["tiny"]),
                models_before_move=("tiny", "base"),
            ),
            management.announcement(
                management.ACTION_MOVE_MODELS,
                error=_move_error(errors.CANCELLED, ["tiny"]),
                models_before_move=("tiny", "base"),
            ),
            management.announcement(
                management.ACTION_MOVE_MODELS,
                error=_move_error(errors.NO_DISK_SPACE, []),
                models_before_move=("tiny",),
            ),
            management.announcement(
                management.ACTION_MOVE_MODELS,
                error=_move_error(errors.NO_DISK_SPACE, ["tiny"]),
                models_before_move=("tiny", "base"),
            ),
            management.announcement(management.ACTION_INSTALL_CUDA_RUNTIME, (True, (), None)),
            management.announcement(management.ACTION_INSTALL_CUDA_RUNTIME, (False, (), None)),
            management.announcement(management.ACTION_INSTALL_CUDA_RUNTIME, (None, (), None)),
            management.announcement(management.ACTION_VERIFY_CUDA_RUNTIME, None),
            management.announcement(management.ACTION_REMOVE_CUDA_RUNTIME, ()),
            management.announcement(management.ACTION_REMOVE_CUDA_RUNTIME, ("a.dll",)),
            management.announcement(
                management.ACTION_VERIFY_MODEL, error=errors.TranscriptionError(errors.CANCELLED)
            ),
            management.announcement(
                management.ACTION_VERIFY_MODEL, error=errors.TranscriptionError(errors.BACKEND_ERROR)
            ),
        ] + [
            management.announcement(action, result, build_id=build.id)
            for build in whisper_cpp_builds.BUILDS
            for action, result in (
                (management.ACTION_INSTALL_WHISPER_CPP, None),
                (management.ACTION_REPAIR_WHISPER_CPP, None),
                (management.ACTION_VERIFY_WHISPER_CPP, None),
                (management.ACTION_REMOVE_WHISPER_CPP, ()),
                (management.ACTION_REMOVE_WHISPER_CPP, ("whisper-cli.exe",)),
            )
        ]
        covered = {s.i18n_key for s in samples}
        assert set(management.ANNOUNCEMENT_I18N_KEYS
                   + management_whisper_cpp.ANNOUNCEMENT_I18N_KEYS) <= covered
        for said in samples:
            values = dict(said.values)
            if "build" in values:
                # An i18n key, which the tab translates before formatting.
                values["build"] = table[values["build"]]
            text = table[said.i18n_key].format(**values)
            assert not re.search(r"[{}]", text), f"{locale}: {said.i18n_key}"
