"""core/wa_version_refresh.py: the staged refresh of the wa-version catalogue.

No network, no windows: ``fetch`` and ``clock`` are injected and the
node_modules tree is a tmp_path. Covers version selection, integrity, the
tarball safety rules, the stage/apply split, the rollback, the lock, the
throttle and the startup hooks.
"""

import base64
import hashlib
import io
import json
import os
import tarfile
import threading
import time

import pytest

import core.wa_version_refresh as wr

REGISTRY = "https://registry.npmjs.org/@wppconnect/wa-version"
TARBALL = "https://registry.npmjs.org/@wppconnect/wa-version/-/wa-version-1.5.4964.tgz"


# --------------------------------------------------------------- fixtures ---

def _package_json(version, deps=None):
    return {"name": wr.PACKAGE, "version": version, "main": "dist/index.js",
            "dependencies": deps if deps is not None else {"semver": "^7.8.5", "node-fetch": "^2.7.0"}}


def _write_package(root, version, build="2.3000.1048298845-alpha", deps=None):
    os.makedirs(os.path.join(root, "dist"), exist_ok=True)
    with open(os.path.join(root, "package.json"), "w") as fh:
        json.dump(_package_json(version, deps), fh)
    with open(os.path.join(root, "versions.json"), "w") as fh:
        json.dump({"versions": [{"version": build}]}, fh)
    with open(os.path.join(root, "dist", "index.js"), "w") as fh:
        fh.write("module.exports = {};")
    os.makedirs(os.path.join(root, "html"), exist_ok=True)
    with open(os.path.join(root, "html", f"{build}.html"), "w") as fh:
        fh.write("<html></html>")


def _tgz(members):
    """members: [(name, bytes | ('dir',) | ('link', target))]"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in members:
            info = tarfile.TarInfo(name)
            if body == ("dir",):
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif isinstance(body, tuple) and body[0] == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = body[1]
                tf.addfile(info)
            else:
                info.size = len(body)
                tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _good_tarball(version="1.5.4964", build="2.3000.1048960956-alpha", deps=None):
    return _tgz([
        ("package/package.json", json.dumps(_package_json(version, deps)).encode()),
        ("package/versions.json", json.dumps({"versions": [{"version": build}]}).encode()),
        ("package/dist/index.js", b"module.exports = {};"),
        (f"package/html/{build}.html", b"<html>pinned</html>"),
    ])


def _sri(data):
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


@pytest.fixture(autouse=True)
def _settle_discards():
    """Old copies are deleted on background threads; never leak one."""
    yield
    wr.join_discards()


@pytest.fixture
def tree(tmp_path):
    """node_modules with wa-version 1.5.4490 (nested semver), node-fetch,
    wppconnect declaring ^1.5.4472, plus a state dir."""
    nm = tmp_path / "node_modules"
    target = nm / "@wppconnect" / "wa-version"
    _write_package(str(target), "1.5.4490")
    nested = target / "node_modules" / "semver"
    nested.mkdir(parents=True)
    (nested / "package.json").write_text(json.dumps({"version": "7.8.5"}))
    fetch_dir = nm / "node-fetch"
    fetch_dir.mkdir()
    (fetch_dir / "package.json").write_text(json.dumps({"version": "2.7.0"}))
    wpp = nm / "@wppconnect-team" / "wppconnect"
    wpp.mkdir(parents=True)
    (wpp / "package.json").write_text(json.dumps(
        {"dependencies": {"@wppconnect/wa-version": "^1.5.4472"}}))
    state = tmp_path / "state"
    return type("Tree", (), {"nm": str(nm), "target": str(target), "state": str(state),
                             "root": tmp_path})


def _fetcher(tarball=None, latest=None, calls=None, fail_on=None):
    tarball = tarball if tarball is not None else _good_tarball()
    latest = latest if latest is not None else {
        "version": "1.5.4964",
        "dependencies": {"semver": "^7.8.5", "node-fetch": "^2.7.0"},
        "dist": {"tarball": TARBALL, "integrity": _sri(tarball)},
    }

    def fetch(url, max_bytes, timeout=8):
        if calls is not None:
            calls.append((url, max_bytes))
        if fail_on and fail_on in url:
            raise OSError("offline")
        if url.endswith("/latest"):
            return json.dumps(latest).encode()
        if url == TARBALL:
            return tarball
        raise AssertionError(f"unexpected url {url}")
    return fetch


def _installed(tree):
    return json.load(open(os.path.join(tree.target, "package.json")))["version"]


# ---------------------------------------------------------- version logic ---

class TestVersionSelection:
    def test_parse_rejects_prereleases(self):
        assert wr.parse_version("1.5.4964") == (1, 5, 4964)
        assert wr.parse_version("1.6.0-beta.1") is None
        assert wr.parse_version("garbage") is None

    def test_newer_same_major_is_picked(self):
        assert wr.pick_version("1.5.4490", ["1.5.4964"]) == "1.5.4964"

    def test_the_newest_of_several(self):
        assert wr.pick_version("1.5.4490", ["1.5.4600", "1.5.4964", "1.5.4700"]) == "1.5.4964"

    @pytest.mark.parametrize("candidate", ["1.5.4490", "1.5.4000"])
    def test_same_or_older_is_not(self, candidate):
        assert wr.pick_version("1.5.4490", [candidate]) is None

    @pytest.mark.parametrize("text", [
        "1.5.4964\n", " 1.5.4964", "1.5.4964 ", "1.5.04964", "\u0661.5.1", "1.5", None, 15,
    ])
    def test_parse_is_strict(self, text):
        assert wr.parse_version(text) is None

    def test_pick_returns_the_canonical_string(self):
        assert wr.pick_version("1.5.4490", ["1.5.4964"]) == "1.5.4964"
        assert wr.pick_version("1.5.4490", ["1.5.4964\n"]) is None

    def test_prerelease_is_not(self):
        assert wr.pick_version("1.5.4490", ["1.5.5000-beta.1"]) is None

    def test_a_major_bump_is_not(self):
        assert wr.pick_version("1.5.4490", ["2.0.0"]) is None

    def test_the_declared_range_is_respected(self):
        assert wr.pick_version("1.5.4490", ["1.5.4964", "1.6.1"], "~1.5.4472") == "1.5.4964"
        assert wr.pick_version("1.5.4490", ["1.5.4964"], "~1.4.0") is None

    def test_an_unreadable_range_means_same_major_only(self):
        assert wr.pick_version("1.5.4490", ["1.5.4964", "2.1.0"], ">1.5 <2 || >3") == "1.5.4964"

    def test_range_forms(self):
        assert wr.satisfies_range("7.8.5", "^7.8.5") is True
        assert wr.satisfies_range("7.8.4", "^7.8.5") is False
        assert wr.satisfies_range("8.0.0", "^7.8.5") is False
        assert wr.satisfies_range("2.7.0", "^2.7.0") is True
        assert wr.satisfies_range("0.2.5", "^0.2.1") is True
        assert wr.satisfies_range("0.3.0", "^0.2.1") is False
        assert wr.satisfies_range("1.2.9", "~1.2.3") is True
        assert wr.satisfies_range("1.3.0", "~1.2.3") is False
        assert wr.satisfies_range("1.3.0", "*") is True
        assert wr.satisfies_range("1.3.0", "1 - 2") is None


# -------------------------------------------------------------- integrity ---

class TestIntegrity:
    def test_matching_sri_passes(self):
        assert wr.verify_integrity(b"abc", _sri(b"abc")) is True

    def test_mismatch_is_rejected(self):
        assert wr.verify_integrity(b"abc", _sri(b"abd")) is False

    def test_integrity_wins_over_a_matching_shasum(self):
        sha1 = hashlib.sha1(b"abc").hexdigest()
        assert wr.verify_integrity(b"abc", _sri(b"zzz"), sha1) is False

    def test_shasum_only_when_integrity_is_absent(self):
        assert wr.verify_integrity(b"abc", None, hashlib.sha1(b"abc").hexdigest()) is True
        assert wr.verify_integrity(b"abc", None, "0" * 40) is False

    def test_nothing_to_compare_is_a_failure(self):
        assert wr.verify_integrity(b"abc") is False

    def test_an_unsupported_algorithm_is_a_failure(self):
        assert wr.verify_integrity(b"abc", "md5-AAAA") is False


class TestTarballHost:
    def test_only_https_registry(self):
        assert wr.tarball_url_allowed(TARBALL)
        assert not wr.tarball_url_allowed("http://registry.npmjs.org/x.tgz")
        assert not wr.tarball_url_allowed("https://evil.example/x.tgz")
        assert not wr.tarball_url_allowed("https://registry.npmjs.org.evil.example/x.tgz")
        assert not wr.tarball_url_allowed("https://user@registry.npmjs.org/x.tgz")
        assert not wr.tarball_url_allowed(None)

    def test_default_fetch_refuses_plain_http(self):
        with pytest.raises(ValueError):
            wr.default_fetch("http://registry.npmjs.org/x", 10)


# ------------------------------------------------------------- extraction ---

class TestSafeExtract:
    def test_extracts_and_strips_package_prefix(self, tmp_path):
        dest = str(tmp_path / "out")
        wr.safe_extract(_good_tarball(), dest)
        assert os.path.isfile(os.path.join(dest, "package.json"))
        assert os.path.isfile(os.path.join(dest, "dist", "index.js"))

    @pytest.mark.parametrize("name", [
        "package/../evil.txt", "package/a/../../evil.txt", "/abs/evil.txt",
        "evil.txt", "package\\evil.txt", "package/C:/evil.txt",
    ])
    def test_bad_names_are_rejected(self, tmp_path, name):
        with pytest.raises(wr.UnsafeArchive):
            wr.safe_extract(_tgz([(name, b"x")]), str(tmp_path / "out"))
        assert not (tmp_path / "evil.txt").exists()

    def test_links_are_rejected(self, tmp_path):
        data = _tgz([("package/link", ("link", "/etc/passwd"))])
        with pytest.raises(wr.UnsafeArchive):
            wr.safe_extract(data, str(tmp_path / "out"))

    def test_oversize_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wr, "MAX_EXTRACTED_BYTES", 10)
        with pytest.raises(wr.UnsafeArchive):
            wr.safe_extract(_tgz([("package/a", b"x" * 11)]), str(tmp_path / "out"))

    def test_too_many_members_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wr, "MAX_MEMBERS", 2)
        data = _tgz([("package/a", b"1"), ("package/b", b"1"), ("package/c", b"1")])
        with pytest.raises(wr.UnsafeArchive):
            wr.safe_extract(data, str(tmp_path / "out"))

    def test_the_caps_leave_room_over_the_measured_package(self):
        measured_packed, measured_on_disk = 42.6e6, 161e6  # 2026-10
        assert wr.MAX_TARBALL_BYTES >= 2 * measured_packed
        assert wr.MAX_EXTRACTED_BYTES >= 2 * measured_on_disk
        assert wr.MAX_VERSION_DOC_BYTES <= 64 * 1024

    def test_duplicate_members_are_rejected(self, tmp_path):
        data = _tgz([("package/a", b"1"), ("package/a", b"2")])
        with pytest.raises(wr.UnsafeArchive):
            wr.safe_extract(data, str(tmp_path / "out"))

    def test_case_colliding_members_are_rejected(self, tmp_path):
        data = _tgz([("package/File.js", b"1"), ("package/file.JS", b"2")])
        with pytest.raises(wr.UnsafeArchive):
            wr.safe_extract(data, str(tmp_path / "out"))


# -------------------------------------------------------------- the stage ---

def _stage(tree, **kw):
    kw.setdefault("clock", lambda: 1000.0)
    return wr.check_and_stage(tree.nm, tree.state, **kw)


def _staged(tree, version="1.5.4964"):
    return f"{tree.target}.staged-{version}"


class TestStage:
    def test_stages_without_touching_the_live_package(self, tree):
        assert _stage(tree, fetch=_fetcher()) == "staged"
        assert os.path.isdir(_staged(tree))
        assert _installed(tree) == "1.5.4490"
        assert not wr._siblings(tree.target, "staging")

    def test_only_latest_is_asked_never_the_packument(self, tree):
        calls = []
        _stage(tree, fetch=_fetcher(calls=calls))
        urls = [u for u, _ in calls]
        assert urls == [f"{REGISTRY}/latest", TARBALL]
        assert dict(calls)[f"{REGISTRY}/latest"] == wr.MAX_VERSION_DOC_BYTES
        assert dict(calls)[TARBALL] == wr.MAX_TARBALL_BYTES

    def test_nothing_newer_downloads_no_tarball(self, tree):
        calls = []
        latest = {"version": "1.5.4490", "dist": {}}
        assert _stage(tree, fetch=_fetcher(latest=latest, calls=calls)) == "current"
        assert len(calls) == 1

    def test_a_major_bump_is_left_to_the_reinstall(self, tree):
        latest = {"version": "2.0.0", "dist": {"tarball": TARBALL}}
        assert _stage(tree, fetch=_fetcher(latest=latest)) == "unsupported"
        assert not os.path.isdir(_staged(tree, "2.0.0"))

    def test_hash_mismatch_is_rejected_and_leaves_nothing(self, tree):
        tarball = _good_tarball()
        latest = {"version": "1.5.4964", "dependencies": {},
                  "dist": {"tarball": TARBALL, "integrity": _sri(b"other")}}
        assert _stage(tree, fetch=_fetcher(tarball, latest)) == "rejected"
        assert not os.path.isdir(_staged(tree))
        assert not wr._siblings(tree.target, "staging")

    def test_a_foreign_tarball_host_is_rejected_before_downloading(self, tree):
        calls = []
        latest = {"version": "1.5.4964", "dependencies": {},
                  "dist": {"tarball": "https://evil.example/a.tgz", "integrity": "sha512-x"}}
        assert _stage(tree, fetch=_fetcher(latest=latest, calls=calls)) == "rejected"
        assert len(calls) == 1

    def test_a_traversal_tarball_is_rejected(self, tree):
        bad = _tgz([("package/../evil", b"x")])
        assert _stage(tree, fetch=_fetcher(bad, None)) == "rejected"
        assert not (tree.root / "node_modules" / "@wppconnect" / "evil").exists()

    def test_a_package_that_is_not_the_one_asked_for_is_rejected(self, tree):
        wrong = _good_tarball(version="1.5.4965")
        assert _stage(tree, fetch=_fetcher(wrong, None)) == "rejected"
        assert not os.path.isdir(_staged(tree))

    def test_a_package_without_a_usable_catalogue_is_rejected(self, tree):
        bad = _tgz([
            ("package/package.json", json.dumps(_package_json("1.5.4964")).encode()),
            ("package/versions.json", b"{not json"),
            ("package/dist/index.js", b""),
        ])
        assert _stage(tree, fetch=_fetcher(bad, None)) == "rejected"

    def test_a_package_without_the_html_of_its_newest_build_is_rejected(self, tree):
        no_html = _tgz([
            ("package/package.json", json.dumps(_package_json("1.5.4964")).encode()),
            ("package/versions.json", json.dumps({"versions": [{"version": "2.3000.1-alpha"}]}).encode()),
            ("package/dist/index.js", b""),
            ("package/html/some-other-build.html", b"<html></html>"),
        ])
        assert _stage(tree, fetch=_fetcher(no_html, None)) == "rejected"
        assert not os.path.isdir(_staged(tree))

    def test_an_empty_html_file_is_rejected(self, tree):
        empty = _tgz([
            ("package/package.json", json.dumps(_package_json("1.5.4964")).encode()),
            ("package/versions.json", json.dumps({"versions": [{"version": "2.3000.1-alpha"}]}).encode()),
            ("package/dist/index.js", b""),
            ("package/html/2.3000.1-alpha.html", b""),
        ])
        assert _stage(tree, fetch=_fetcher(empty, None)) == "rejected"

    def test_the_download_budgets_are_passed_to_the_fetcher(self, tree):
        seen = []

        def fetch(url, max_bytes, deadline=None):
            seen.append(deadline)
            return _fetcher()(url, max_bytes, deadline)
        _stage(tree, fetch=fetch)
        assert seen == [wr.LATEST_DEADLINE_SECONDS, wr.TARBALL_DEADLINE_SECONDS]
        assert wr.LATEST_DEADLINE_SECONDS <= 15 and wr.TARBALL_DEADLINE_SECONDS <= 180

    def test_a_missing_dependency_is_skipped_before_the_download(self, tree):
        calls = []
        latest = {"version": "1.5.4964", "dependencies": {"left-pad": "^1.0.0"},
                  "dist": {"tarball": TARBALL, "integrity": "sha512-x"}}
        assert _stage(tree, fetch=_fetcher(latest=latest, calls=calls)) == "skipped-deps"
        assert len(calls) == 1

    def test_a_dependency_range_the_install_cannot_meet_is_skipped(self, tree):
        latest = {"version": "1.5.4964", "dependencies": {"semver": "^8.0.0"},
                  "dist": {"tarball": TARBALL, "integrity": "sha512-x"}}
        assert _stage(tree, fetch=_fetcher(latest=latest)) == "skipped-deps"

    def test_offline_is_swallowed_and_not_remembered(self, tree):
        assert _stage(tree, fetch=_fetcher(fail_on="/latest")) == "offline"
        assert not os.path.exists(os.path.join(tree.state, "wa_version_refresh.json"))

    def test_a_download_cut_short_is_swallowed_and_retried_next_time(self, tree):
        assert _stage(tree, fetch=_fetcher(fail_on=".tgz")) == "offline"
        assert _stage(tree, fetch=_fetcher(), clock=lambda: 1001.0) == "staged"

    def test_a_timeout_is_swallowed(self, tree):
        def fetch(url, max_bytes, timeout=8):
            raise TimeoutError("slow")
        assert _stage(tree, fetch=fetch) == "offline"

    def test_an_unexpected_error_never_raises(self, tree):
        def fetch(url, max_bytes, timeout=8):
            raise RuntimeError("boom")
        assert _stage(tree, fetch=fetch) == "error"

    def test_no_installed_package_is_not_an_error(self, tree):
        import shutil
        shutil.rmtree(tree.target)
        assert _stage(tree, fetch=_fetcher()) == "missing"

    def test_a_half_finished_staging_dir_is_cleaned_and_never_applied(self, tree):
        half = f"{tree.target}.staging-1"
        os.makedirs(os.path.join(half, "dist"))
        assert wr.apply_staged(tree.nm, tree.state) == "nothing-staged"
        assert _installed(tree) == "1.5.4490"
        _stage(tree, fetch=_fetcher())
        assert not os.path.exists(half)

    def test_an_already_staged_version_is_not_downloaded_again(self, tree):
        _stage(tree, fetch=_fetcher())
        calls = []
        assert _stage(tree, fetch=_fetcher(calls=calls),
                      clock=lambda: 1000.0 + 7 * 3600) == "staged"
        assert len(calls) == 1


class TestThrottle:
    def test_a_second_check_inside_six_hours_does_not_touch_the_network(self, tree):
        _stage(tree, fetch=_fetcher())
        calls = []
        assert _stage(tree, fetch=_fetcher(calls=calls), clock=lambda: 1000.0 + 3600) == "throttled"
        assert calls == []

    def test_after_six_hours_it_checks_again(self, tree):
        _stage(tree, fetch=_fetcher())
        calls = []
        _stage(tree, fetch=_fetcher(calls=calls), clock=lambda: 1000.0 + 6 * 3600 + 1)
        assert calls

    def test_the_timestamp_lives_in_the_state_dir(self, tree):
        _stage(tree, fetch=_fetcher())
        state = json.load(open(os.path.join(tree.state, "wa_version_refresh.json")))
        assert state['checked'] == 1000.0
        assert state['installed'] == '1.5.4490'
        assert len(state['identity']) == 64

    def test_a_clock_that_went_backwards_does_not_throttle_forever(self, tree):
        _stage(tree, fetch=_fetcher(), clock=lambda: 5000.0)
        assert _stage(tree, fetch=_fetcher(), clock=lambda: 10.0) != "throttled"


class TestLocks:
    def test_the_stage_gives_way_when_the_fetch_lock_is_held(self, tree):
        calls = []
        with wr.exclusive_lock(os.path.join(tree.state, "wa_version_refresh.lock")) as held:
            assert held
            assert _stage(tree, fetch=_fetcher(calls=calls)) == "locked"
        assert calls == []

    def test_apply_gives_way_when_the_apply_lock_is_held(self, tree):
        _stage(tree, fetch=_fetcher())
        with wr.exclusive_lock(os.path.join(tree.state, "wa_version_apply.lock")) as held:
            assert held
            assert wr.apply_staged(tree.nm, tree.state) == "locked"
        assert _installed(tree) == "1.5.4490"
        assert wr.apply_staged(tree.nm, tree.state) == "updated"

    def test_a_stalled_stage_never_makes_apply_report_locked(self, tree):
        _stage(tree, fetch=_fetcher())
        with wr.exclusive_lock(os.path.join(tree.state, "wa_version_refresh.lock")) as held:
            assert held  # a download that never ends holds only this lock
            assert wr.apply_staged(tree.nm, tree.state) == "updated"

    def test_the_lock_is_released_afterwards(self, tree):
        path = os.path.join(tree.state, "x.lock")
        with wr.exclusive_lock(path) as first:
            assert first
        with wr.exclusive_lock(path) as second:
            assert second


# ------------------------------------------------------------- the apply ---

class TestApply:
    def test_swaps_in_the_staged_package_and_carries_the_nested_modules(self, tree):
        _stage(tree, fetch=_fetcher())
        assert wr.apply_staged(tree.nm, tree.state) == "updated"
        assert _installed(tree) == "1.5.4964"
        assert os.path.isfile(os.path.join(tree.target, "node_modules", "semver", "package.json"))
        wr.join_discards()
        assert not os.path.exists(_staged(tree))
        assert not wr._siblings(tree.target, "old")

    def test_nothing_staged_is_a_quiet_no_op(self, tree):
        assert wr.apply_staged(tree.nm, tree.state) == "nothing-staged"
        assert _installed(tree) == "1.5.4490"

    def test_another_accounts_live_node_leaves_the_package_staged(self, tree):
        _stage(tree, fetch=_fetcher())
        assert wr.apply_staged(tree.nm, tree.state, busy=lambda: True) == "busy"
        assert _installed(tree) == "1.5.4490"
        assert os.path.isdir(_staged(tree))
        assert wr.apply_staged(tree.nm, tree.state, busy=lambda: False) == "updated"

    def test_the_old_copy_is_renamed_aside_not_deleted_inline(self, tree, monkeypatch):
        _stage(tree, fetch=_fetcher())
        deleted_inline = []
        real = wr.shutil.rmtree
        main = threading.current_thread()

        def spy(path, *a, **kw):
            if ".old-" in str(path) and threading.current_thread() is main:
                deleted_inline.append(path)
            return real(path, *a, **kw)
        monkeypatch.setattr(wr.shutil, "rmtree", spy)
        assert wr.apply_staged(tree.nm, tree.state) == "updated"
        wr.join_discards()
        assert deleted_inline == []
        assert not wr._siblings(tree.target, "old")

    def test_apply_is_not_throttled_and_never_fetches(self, tree):
        _stage(tree, fetch=_fetcher())
        # the check just ran (throttled for six hours); the apply still goes
        assert wr.apply_staged(tree.nm, tree.state, clock=lambda: 1001.0) == "updated"

    def test_a_dependency_missing_at_apply_time_is_skipped_and_dropped(self, tree):
        _stage(tree, fetch=_fetcher())
        import shutil
        shutil.rmtree(os.path.join(tree.nm, "node-fetch"))
        assert wr.apply_staged(tree.nm, tree.state) == "skipped-deps"
        wr.join_discards()
        assert _installed(tree) == "1.5.4490"
        assert not os.path.exists(_staged(tree))

    def test_a_staged_package_not_newer_than_the_installed_one_is_dropped(self, tree):
        _stage(tree, fetch=_fetcher())
        # a reinstall brought the install past the staged version meanwhile
        _write_package(tree.target, "1.5.5000")
        assert wr.apply_staged(tree.nm, tree.state) == "nothing-staged"
        wr.join_discards()
        assert not os.path.exists(_staged(tree))

    def test_a_corrupted_staged_package_is_rejected(self, tree):
        _stage(tree, fetch=_fetcher())
        os.remove(os.path.join(_staged(tree), "versions.json"))
        assert wr.apply_staged(tree.nm, tree.state) == "rejected"
        assert _installed(tree) == "1.5.4490"

    def test_a_failed_second_rename_restores_the_old_package(self, tree, monkeypatch):
        _stage(tree, fetch=_fetcher())
        real = os.rename
        calls = []

        def flaky(src, dst):
            calls.append((src, dst))
            if len(calls) == 2:  # the new package into place
                raise PermissionError("in use")
            return real(src, dst)
        monkeypatch.setattr(wr.os, "rename", flaky)
        assert wr.apply_staged(tree.nm, tree.state) == "error"
        monkeypatch.undo()
        assert _installed(tree) == "1.5.4490"
        assert os.path.isfile(os.path.join(tree.target, "node_modules", "semver", "package.json"))
        # still staged, so the next launch can retry
        assert os.path.isdir(_staged(tree))
        assert wr.apply_staged(tree.nm, tree.state) == "updated"

    def test_a_failed_first_rename_changes_nothing(self, tree, monkeypatch):
        _stage(tree, fetch=_fetcher())
        monkeypatch.setattr(wr.os, "rename", lambda *a: (_ for _ in ()).throw(PermissionError("in use")))
        assert wr.apply_staged(tree.nm, tree.state) == "error"
        monkeypatch.undo()
        assert _installed(tree) == "1.5.4490"

    def test_swap_directories_rolls_back_directly(self, tmp_path):
        target, new = tmp_path / "t", tmp_path / "n"
        target.mkdir()
        (target / "old.txt").write_text("old")
        new.mkdir()
        real = os.rename
        n = []

        def rename(a, b):
            n.append(1)
            if len(n) == 2:
                raise OSError("boom")
            real(a, b)
        with pytest.raises(OSError):
            wr.swap_directories(str(target), str(new), 1, rename=rename)
        assert (target / "old.txt").read_text() == "old"
        assert new.exists()

    def test_a_crash_between_the_renames_is_repaired_on_the_next_apply(self, tree):
        os.rename(tree.target, f"{tree.target}.old-5")
        assert not os.path.isdir(tree.target)
        wr.apply_staged(tree.nm, tree.state)
        assert _installed(tree) == "1.5.4490"

    def test_a_leftover_old_copy_is_swept(self, tree):
        leftover = f"{tree.target}.old-5"
        os.makedirs(leftover)
        wr.apply_staged(tree.nm, tree.state)
        wr.join_discards()
        assert not os.path.exists(leftover)

    def test_the_declared_range_gates_the_staged_package_too(self, tree):
        _stage(tree, fetch=_fetcher())
        wpp = os.path.join(tree.nm, "@wppconnect-team", "wppconnect", "package.json")
        json.dump({"dependencies": {"@wppconnect/wa-version": "~1.4.0"}}, open(wpp, "w"))
        assert wr.apply_staged(tree.nm, tree.state) == "nothing-staged"


# ----------------------------------------------------------- startup hooks ---

class TestStartupHooks:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(wr, "_thread", None)
        monkeypatch.setattr(wr, "_paths", None)

    def test_it_starts_once_per_process(self, tree):
        calls = []
        assert wr.start_in_background(tree.nm, tree.state, fetch=_fetcher(calls=calls)) is True
        assert wr.start_in_background(tree.nm, tree.state, fetch=_fetcher(calls=calls)) is False
        wr._thread.join(5)
        assert [u for u, _ in calls].count(f"{REGISTRY}/latest") == 1

    def test_the_wait_is_bounded_and_the_thread_is_abandoned(self, tree):
        release = threading.Event()

        def slow(url, max_bytes, timeout=8):
            release.wait(10)
            raise OSError("late")
        wr.start_in_background(tree.nm, tree.state, fetch=slow)
        started = time.monotonic()
        wr.wait_for_refresh(timeout=0.2)
        assert time.monotonic() - started < 2
        assert wr._thread.is_alive() and wr._thread.daemon
        release.set()
        wr._thread.join(5)

    def test_a_download_that_wins_the_wait_is_applied_at_once(self, tree):
        wr.start_in_background(tree.nm, tree.state, fetch=_fetcher())
        wr.wait_for_refresh(timeout=10)
        assert _installed(tree) == "1.5.4964"

    def test_a_staged_package_is_applied_even_without_a_new_check(self, tree):
        _stage(tree, fetch=_fetcher())
        wr.start_in_background(tree.nm, tree.state, check=False)
        assert wr._thread is None
        wr.wait_for_refresh(timeout=0.1)
        assert _installed(tree) == "1.5.4964"

    def test_a_download_that_loses_the_wait_does_not_swap_under_node(self, tree):
        release = threading.Event()
        inner = _fetcher()

        def gated(url, max_bytes, timeout=8):
            if url == TARBALL:
                release.wait(10)
            return inner(url, max_bytes, timeout)
        wr.start_in_background(tree.nm, tree.state, fetch=gated)
        wr.wait_for_refresh(timeout=0.2)
        assert _installed(tree) == "1.5.4490"
        release.set()
        wr._thread.join(5)
        assert os.path.isdir(_staged(tree))  # waits for the next launch
        assert _installed(tree) == "1.5.4490"

    def test_without_registered_paths_the_wait_does_nothing(self):
        wr.wait_for_refresh(timeout=0.1)


# ------------------------------------------------------------ default_fetch ---

class _Resp:
    def __init__(self, chunks, length=None):
        self._chunks = list(chunks)
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, resp):
        self.resp = resp
        self.timeout = None

    def open(self, req, timeout=None):
        self.timeout = timeout
        return self.resp


class TestDefaultFetch:
    def test_it_streams_and_joins_the_chunks(self):
        opener = _Opener(_Resp([b"ab", b"cd"]))
        assert wr.default_fetch("https://registry.npmjs.org/x", 100, opener=opener) == b"abcd"
        assert opener.timeout == wr.TIMEOUT_SECONDS

    def test_the_size_cap_is_enforced_while_streaming(self):
        opener = _Opener(_Resp([b"x" * 6, b"x" * 6]))
        with pytest.raises(ValueError):
            wr.default_fetch("https://registry.npmjs.org/x", 10, opener=opener)

    def test_a_declared_length_over_the_cap_is_refused_before_reading(self):
        resp = _Resp([b"x"], length=999)
        with pytest.raises(ValueError):
            wr.default_fetch("https://registry.npmjs.org/x", 10, opener=_Opener(resp))
        assert resp._chunks == [b"x"]

    def test_an_absolute_deadline_stops_a_trickling_server(self):
        now = [0.0]

        def clock():
            now[0] += 6.0
            return now[0]
        opener = _Opener(_Resp([b"a"] * 50))
        with pytest.raises(TimeoutError):
            wr.default_fetch("https://registry.npmjs.org/x", 1000, deadline=15, opener=opener,
                             clock=clock)

    def test_plain_http_is_refused_even_with_an_opener(self):
        with pytest.raises(ValueError):
            wr.default_fetch("http://registry.npmjs.org/x", 10, opener=_Opener(_Resp([])))

    def test_redirects_are_refused(self):
        import urllib.request
        handler = wr._NoRedirect()
        req = urllib.request.Request("https://registry.npmjs.org/x")
        assert handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/") is None


# ------------------------------------------------------- busy: leases ---

class TestOtherAccountsNode:
    def _patch(self, monkeypatch, leases=None, boom=False):
        import node_coord
        import update_coord

        def live(gd, is_alive):
            if boom:
                raise OSError("x")
            return leases
        monkeypatch.setattr(node_coord, "live_node_leases", live)
        monkeypatch.setattr(update_coord, "lease_alive", lambda *a: True)

    def test_only_my_own_lease_is_not_busy(self, monkeypatch):
        self._patch(monkeypatch, [{"account_id": "me"}])
        assert wr.other_accounts_node_alive("gd", "me") is False

    def test_another_live_lease_is_busy(self, monkeypatch):
        self._patch(monkeypatch, [{"account_id": "me"}, {"account_id": "other"}])
        assert wr.other_accounts_node_alive("gd", "me") is True

    def test_a_corrupt_lease_or_a_failed_lookup_fails_closed(self, monkeypatch):
        self._patch(monkeypatch, [{"account_id": "me", "_corrupt": True}])
        assert wr.other_accounts_node_alive("gd", "me") is True
        self._patch(monkeypatch, boom=True)
        assert wr.other_accounts_node_alive("gd", "me") is True

    def test_nobody_to_ask_is_not_busy(self):
        assert wr.other_accounts_node_alive(None, None) is False


# ------------------------------------------------------------- the glue ---

class TestGlue:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(wr, "_thread", None)
        monkeypatch.setattr(wr, "_paths", None)
        monkeypatch.setattr(wr, "_busy", None)

    def test_start_at_launch_registers_and_checks_when_no_node_is_up(self, tree, monkeypatch):
        started = []
        monkeypatch.setattr(wr, "check_and_stage", lambda *a, **k: started.append(a))
        window = type("W", (), {"global_dir": "gd", "account_id": "me",
                                "_is_wpp_running": lambda self: False})()
        assert wr.start_at_launch(window, tree.nm, tree.state) is True
        wr._thread.join(5)
        assert started == [(tree.nm, tree.state)]
        assert wr._paths == (tree.nm, tree.state) and callable(wr._busy)

    def test_start_at_launch_does_not_download_for_an_adopted_node(self, tree):
        window = type("W", (), {"global_dir": "gd", "account_id": "me",
                                "_is_wpp_running": lambda self: True})()
        assert wr.start_at_launch(window, tree.nm, tree.state) is False
        assert wr._thread is None and wr._paths is not None

    def test_the_wait_passes_the_busy_check_to_apply(self, tree, monkeypatch):
        _stage(tree, fetch=_fetcher())
        wr.start_in_background(tree.nm, tree.state, check=False, busy=lambda: True)
        wr.wait_for_refresh(timeout=0.1)
        assert _installed(tree) == "1.5.4490"

    def test_main_starts_the_refresh_between_the_version_check_and_node(self):
        import inspect
        import main
        src = inspect.getsource(main.MainWindow.__init__)
        a = src.index("self.ensure_wpp_version()")
        b = src.index("start_at_launch(")
        c = src.index("self.ensure_wpp_running()")
        assert a < b < c

    def test_the_spawn_itself_no_longer_waits_on_the_ui_thread(self):
        import inspect
        from main import MainWindow
        assert "wait_for_refresh" not in inspect.getsource(MainWindow._start_wpp_background)

    def test_foreground_spawn_waits_on_a_worker_then_spawns_on_the_ui_thread(self, monkeypatch):
        import main_window.wpp_server as ws
        from main import MainWindow
        events = []
        main_thread = threading.current_thread()
        done = threading.Event()

        def fake_wait(*a, **k):
            events.append(("wait", threading.current_thread() is main_thread))

        def call_after(fn, *a):
            events.append(("call_after", fn.__name__))
            fn()
            done.set()
        monkeypatch.setattr(wr, "wait_for_refresh", fake_wait)
        monkeypatch.setattr(ws.wx, "CallAfter", call_after)
        stub = type("S", (), {})()
        stub._start_wpp_background = lambda: events.append(("spawn", None))
        MainWindow._start_wpp_background_after_catalogue(stub)
        assert done.wait(5)
        assert events[0] == ("wait", False)  # a worker, not the UI thread
        assert [e[0] for e in events] == ["wait", "call_after", "spawn"]

    def test_the_spawn_still_happens_when_the_refresh_blows_up(self, monkeypatch):
        import main_window.wpp_server as ws
        from main import MainWindow
        done = threading.Event()
        spawned = []

        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(wr, "wait_for_refresh", boom)
        monkeypatch.setattr(ws.wx, "CallAfter", lambda fn, *a: fn())
        stub = type("S", (), {})()
        stub._start_wpp_background = lambda: (spawned.append(1), done.set())
        MainWindow._start_wpp_background_after_catalogue(stub)
        assert done.wait(5) and spawned == [1]

    def test_the_other_spawn_paths_wait_first(self):
        import inspect
        from main import MainWindow
        bg = inspect.getsource(MainWindow.ensure_wpp_running)
        assert bg.index("wait_for_refresh()") < bg.index("self._start_wpp_background()\n            deadline")
        cancelled = inspect.getsource(MainWindow._restart_wpp_after_cancelled_shutdown)
        assert cancelled.index("wait_for_refresh()") < cancelled.index("self._start_wpp_background()")
