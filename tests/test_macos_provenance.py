"""Commit provenance of the macOS updater (macos/winzapp_mac/provenance.py).

The module is pure standard library, so it is imported by path and tested on
every platform with a fake GitHub API; nothing here touches the network.
The attacker modelled is the owner of the releases repository: they control
the zip and the provenance file, and a fork's commits are reachable through
the official repository's /commits/<sha> API.
"""

import hashlib
import importlib.util
import io
import json
import pathlib
import urllib.error
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = _ROOT / "macos" / "winzapp_mac" / "provenance.py"

pytestmark = pytest.mark.skipif(not _PATH.is_file(), reason="no macOS layer in this checkout")

_spec = importlib.util.spec_from_file_location("winzapp_provenance_under_test", _PATH)
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


@pytest.fixture(autouse=True)
def _no_rate_limit_or_real_retry_wait(monkeypatch):
    monkeypatch.setattr(P, "_RATE_LIMITS", {})
    monkeypatch.setattr(P.time, "sleep", lambda seconds: None)

COMMIT = "a" * 40
OTHER = "b" * 40
TAG_OBJ = "c" * 40
TAG = "v2.0.0.5"
ZIP = "WinZapp-macOS-arm64.zip"
ZIP_BYTES = b"zip bytes"
ZIP_SHA = hashlib.sha256(ZIP_BYTES).hexdigest()


def _prov(**over):
    data = {"schema": 1, "version": TAG, "source_repo": P.OFFICIAL_REPO,
            "source_commit": COMMIT, "artifacts": {ZIP: ZIP_SHA}}
    data.update(over)
    return json.dumps(data).encode()


class Api:
    """The official repository's API: refs and tag objects by url."""

    def __init__(self, tags=None, tag_objects=None):
        self.tags = tags if tags is not None else {TAG: ("commit", COMMIT)}
        self.tag_objects = tag_objects or {}
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        base = f"https://api.github.com/repos/{P.OFFICIAL_REPO}/"
        assert url.startswith(base)
        path = url[len(base):]
        if path.startswith("git/ref/tags/"):
            tag = path.rsplit("/", 1)[1]
            if tag not in self.tags:
                raise P.ProvenanceError("HTTP 404")
            kind, sha = self.tags[tag]
            return json.dumps({"ref": f"refs/tags/{tag}", "object": {"type": kind, "sha": sha}}).encode()
        if path.startswith("git/tags/"):
            sha = path.rsplit("/", 1)[1]
            kind, target = self.tag_objects[sha]
            return json.dumps({"sha": sha, "object": {"type": kind, "sha": target}}).encode()
        raise AssertionError(url)


def _verify(raw=None, tag=TAG, running="v2.0.0.4", api=None, **kw):
    return P.verify_release(raw if raw is not None else _prov(), tag, running,
                            api or Api(), **kw)


# -- accepted -----------------------------------------------------------------

def test_a_valid_provenance_is_accepted():
    api = Api()
    assert _verify(api=api) == (True, "ok")
    assert api.urls == [f"https://api.github.com/repos/{P.OFFICIAL_REPO}/git/ref/tags/{TAG}"]


def test_an_annotated_tag_is_peeled_to_its_commit():
    api = Api(tags={TAG: ("tag", TAG_OBJ)}, tag_objects={TAG_OBJ: ("commit", COMMIT)})
    assert _verify(api=api)[0]


def test_a_tag_of_a_tag_is_peeled_and_a_chain_that_never_ends_is_refused():
    t2 = "d" * 40
    api = Api(tags={TAG: ("tag", TAG_OBJ)},
              tag_objects={TAG_OBJ: ("tag", t2), t2: ("commit", COMMIT)})
    assert _verify(api=api)[0]
    loop = Api(tags={TAG: ("tag", TAG_OBJ)}, tag_objects={TAG_OBJ: ("tag", TAG_OBJ)})
    assert not _verify(api=loop)[0]


def test_an_alpha_tag_is_a_release_like_any_other():
    tag = "v2.0.0.3895alpha"
    api = Api(tags={tag: ("commit", COMMIT)})
    assert _verify(_prov(version=tag), tag=tag, running="v2.0.0.3894alpha", api=api)[0]


# -- the tag is the proof -----------------------------------------------------

def test_a_tag_that_points_elsewhere_is_refused():
    ok, detail = _verify(api=Api(tags={TAG: ("commit", OTHER)}))
    assert not ok and "another commit" in detail


def test_a_tag_missing_from_the_official_repository_is_refused():
    ok, detail = _verify(api=Api(tags={}))
    assert not ok and "404" in detail


def test_a_commit_that_only_exists_in_a_fork_is_refused():
    """The /commits/<sha> API answers 200 for a fork's commit; the verifier
    never asks it, and the tag (which a fork owner cannot create) decides."""
    class ForkApi(Api):
        def __call__(self, url):
            if "/commits/" in url or "/compare/" in url:
                return json.dumps({"sha": COMMIT}).encode()      # a fork commit "exists"
            return super().__call__(url)
    api = ForkApi(tags={})
    assert not _verify(api=api)[0]
    assert not any("/commits/" in u or "/compare/" in u for u in api.urls)
    # even if the official tag resolves to the real main commit
    assert not _verify(api=ForkApi(tags={TAG: ("commit", OTHER)}))[0]


def test_a_malformed_api_answer_fails_closed():
    for body in (b"not json", b"[]", b'{"ref": "refs/tags/v2.0.0.5"}',
                 json.dumps({"ref": "refs/tags/v9.9.9.9",
                             "object": {"type": "commit", "sha": COMMIT}}).encode(),
                 json.dumps({"ref": f"refs/tags/{TAG}",
                             "object": {"type": "commit", "sha": "ABC"}}).encode()):
        assert not _verify(api=lambda url, body=body: body)[0]


def test_api_errors_and_timeouts_fail_closed():
    def boom(url):
        raise TimeoutError("slow")
    ok, detail = _verify(api=boom)
    assert not ok and "could not verify" in detail

    def limited(url):
        raise P.ProvenanceError("HTTP 403")
    assert not _verify(api=limited)[0]


# -- the file, the version, the replay ----------------------------------------

def test_a_provenance_of_another_release_is_refused():
    ok, detail = _verify(_prov(version="v2.0.0.4"), tag=TAG)
    assert not ok and "another release" in detail


def test_a_stale_genuine_provenance_does_not_pass_as_a_newer_tag():
    """Version and commit of an older release, relabelled v2.0.0.6: the
    official tag v2.0.0.6 is a different commit."""
    api = Api(tags={"v2.0.0.4": ("commit", COMMIT), "v2.0.0.6": ("commit", OTHER)})
    assert not _verify(_prov(version="v2.0.0.6"), tag="v2.0.0.6", api=api)[0]


@pytest.mark.parametrize("running", ["v2.0.0.5", "v2.0.0.9", "garbage", "", "2.0.0.4", "v2.0.0.4\n"])
def test_a_release_that_is_not_newer_is_refused(running):
    ok, detail = _verify(running=running)
    assert not ok


def test_an_alpha_is_older_than_its_stable():
    assert P.tag_is_newer("v2.0.0.5", "v2.0.0.5alpha")
    assert not P.tag_is_newer("v2.0.0.5alpha", "v2.0.0.5")
    assert P.tag_is_newer("v2.0.0.10", "v2.0.0.9")
    assert not P.tag_is_newer("v2.0.0.5", "v2.0.0.5")


def test_an_old_genuine_tag_is_a_downgrade_for_a_running_tag():
    """client/version.py says 2.0.0.0 in every tagged commit; the running
    version is the tag the build was made from, so an older release is
    refused however genuine its provenance."""
    old = "v2.0.0.3875alpha"
    api = Api(tags={old: ("commit", COMMIT)})
    assert _verify(_prov(version=old), tag=old, running="v2.0.0.3895alpha", api=api)[0] is False
    assert _verify(_prov(version=old), tag=old, running="v2.0.0.3875alpha", api=api)[0] is False
    assert _verify(_prov(version=old), tag=old, running="v2.0.0.3874alpha", api=api)[0] is True


def test_the_running_version_is_the_plist_release_tag():
    assert P.running_release_tag({P.RELEASE_TAG_KEY: "v2.0.0.5alpha"}) == "v2.0.0.5alpha"
    for info in ({}, {P.RELEASE_TAG_KEY: ""}, {P.RELEASE_TAG_KEY: "2.0.0.5"},
                 {P.RELEASE_TAG_KEY: "v2.0.0.5\n"}, {P.RELEASE_TAG_KEY: None},
                 {P.RELEASE_TAG_KEY: 5}):
        assert P.running_release_tag(info) == ""


def test_the_version_of_a_release_tag_drops_only_the_v():
    assert P.tag_version("v2.1.0.4050alpha") == "2.1.0.4050alpha"
    assert P.tag_version("v2.1.0.5") == "2.1.0.5"
    for tag in ("", "2.1.0.5", "v2.1.0.5\n", "v2.1.0", None):
        assert P.tag_version(tag) == ""


def _sparkle_order(version):
    """How Sparkle's standard comparator orders a purely numeric
    CFBundleVersion: part by part, as integers."""
    return tuple(int(p) for p in version.split("."))


@pytest.mark.parametrize("older,newer", [
    ("v2.1.0.4258alpha", "v2.1.0.4258beta"), ("v2.1.0.4258beta", "v2.1.0.4258"),
    ("v2.1.0.4258", "v2.1.0.4259alpha"), ("v2.1.0.0", "v2.1.0.4258alpha"),
    ("v2.0.0.9", "v2.0.0.10"), ("v2.0.9.0", "v2.1.0.0alpha"), ("v1.9.9.999999999", "v2.0.0.0alpha"),
])
def test_the_bundle_version_orders_like_the_tags(older, newer):
    """Sparkle compares CFBundleVersion (and refuses a downgrade by it at
    install time); if its order and tag_is_newer() ever disagreed, Sparkle
    would offer what provenance refuses, or the reverse."""
    assert P.tag_is_newer(newer, older)
    assert _sparkle_order(P.bundle_version(newer)) > _sparkle_order(P.bundle_version(older))


def test_the_bundle_version_is_four_integers_or_nothing():
    assert P.bundle_version("v2.1.0.4258alpha") == "2.1.0.42581"
    assert P.bundle_version("v2.1.0.0") == "2.1.0.3"
    for tag in ("", "2.1.0.5", "v2.1.0.5\n", "v2.1.0", None, "v2.1.0.5rc"):
        assert P.bundle_version(tag) == ""


def test_the_commit_that_is_already_running_is_not_an_update():
    assert not _verify(running_commit=COMMIT)[0]
    assert _verify(running_commit=OTHER)[0]


@pytest.mark.parametrize("over", [
    {"schema": 2}, {"schema": True}, {"schema": "1"},
    {"source_repo": "rocco-labs/WinZapp_Python"},
    {"source_repo": "GabrielHHaber/WinZapp_Python"},
    {"source_commit": "A" * 40}, {"source_commit": "a" * 39}, {"source_commit": "main"},
    {"source_commit": None},
    {"version": "v2.0.0"}, {"version": "../v2.0.0.5"}, {"version": "v2.0.0.5/../x"},
    {"version": "v2.0.0.5dev"},
    {"artifacts": {}}, {"artifacts": []}, {"artifacts": {ZIP: "x" * 64}},
    {"artifacts": {ZIP: "A" * 64}}, {"artifacts": {"../evil": ZIP_SHA}},
    {"artifacts": {f"a{i}.zip": ZIP_SHA for i in range(P.MAX_ARTIFACTS + 1)}},
])
def test_an_invalid_provenance_is_refused(over):
    assert not _verify(_prov(**over))[0]


def test_unknown_missing_or_oversized_fields_are_refused():
    data = json.loads(_prov())
    assert not _verify(json.dumps({**data, "extra": 1}).encode())[0]
    data.pop("artifacts")
    assert not _verify(json.dumps(data).encode())[0]
    assert not _verify(b"x" * (P.MAX_PROVENANCE_BYTES + 1))[0]
    assert not _verify(b"\xff\xfe")[0]
    assert not _verify(b"[]")[0]


# -- the zip ------------------------------------------------------------------

def test_the_zip_must_match_its_provenance_hash(tmp_path):
    f = tmp_path / ZIP
    f.write_bytes(ZIP_BYTES)
    assert P.check_artifact(_prov(), ZIP, str(f)) == (True, "ok")
    f.write_bytes(ZIP_BYTES + b"!")
    assert not P.check_artifact(_prov(), ZIP, str(f))[0]
    assert not P.check_artifact(_prov(), "WinZapp-macOS-x86_64.zip", str(f))[0]
    assert not P.check_artifact(_prov(), ZIP, str(tmp_path / "missing"))[0]


def test_build_provenance_round_trips_and_refuses_what_the_updater_would():
    raw = P.build_provenance(TAG, COMMIT, {ZIP: ZIP_SHA})
    assert P.parse_provenance(raw)["source_commit"] == COMMIT
    with pytest.raises(P.ProvenanceError):
        P.build_provenance("main", COMMIT, {ZIP: ZIP_SHA})
    with pytest.raises(P.ProvenanceError):
        P.build_provenance(TAG, "abc", {ZIP: ZIP_SHA})


def test_one_provenance_file_per_architecture():
    assert P.provenance_name("arm64") != P.provenance_name("x86_64")


# -- the repository is pinned ---------------------------------------------------

def test_the_official_repository_is_pinned_in_the_code():
    assert P.OFFICIAL_REPO == "gabrielhhaber/WinZapp_Python"
    assert P.API_HOST == "api.github.com"


@pytest.mark.parametrize("url", [
    "https://api.github.com/repos/rocco-labs/WinZapp_Python/git/ref/tags/v1.0.0.1",
    "http://api.github.com/repos/gabrielhhaber/WinZapp_Python/git/ref/tags/v1.0.0.1",
    "https://evil.example/repos/gabrielhhaber/WinZapp_Python/x",
    "https://api.github.com.evil.example/repos/gabrielhhaber/WinZapp_Python/x",
    "https://api.github.com/repos/gabrielhhaber/WinZapp_Python2/x",
])
def test_the_official_fetcher_refuses_any_other_address(url):
    with pytest.raises(P.ProvenanceError):
        P.fetch_official(url)


def test_release_assets_only_from_github_com():
    for url in ("http://github.com/x", "https://evil.example/x", "https://github.com.evil/x"):
        with pytest.raises(P.ProvenanceError):
            P.fetch_release_asset(url)


# -- HTTP ---------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __init__(self, body=b"{}", status=200, url=None):
        super().__init__(body)
        self.status, self._url = status, url

    def geturl(self):
        return self._url


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.last = None

    def open(self, req, timeout=None):
        assert timeout and timeout <= 30
        out = self.outcomes.pop(0) if self.outcomes else self.last
        self.last = out
        if isinstance(out, Exception):
            raise out
        out._url = out._url or req.full_url
        return out


GOOD_URL = f"https://api.github.com/repos/{P.OFFICIAL_REPO}/git/ref/tags/{TAG}"


def test_the_api_fetcher_returns_the_body(monkeypatch):
    monkeypatch.setattr(P, "_API_OPENER", FakeOpener(FakeResponse(b"body")))
    assert P.fetch_official(GOOD_URL) == b"body"


def test_a_redirect_is_refused_not_followed():
    handler = P._NoRedirect()
    req = urllib.request.Request(GOOD_URL)
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/") is None


def test_a_redirect_to_another_host_is_refused(monkeypatch):
    monkeypatch.setattr(P, "_API_OPENER", FakeOpener(
        FakeResponse(b"{}", url="https://evil.example/repos/gabrielhhaber/WinZapp_Python/x")))
    with pytest.raises(P.ProvenanceError):
        P.fetch_official(GOOD_URL)
    monkeypatch.setattr(P, "_API_OPENER", FakeOpener(
        urllib.error.HTTPError(GOOD_URL, 302, "Found", {}, None)))
    with pytest.raises(P.ProvenanceError, match="302"):
        P.fetch_official(GOOD_URL)


def test_the_asset_redirect_handler_follows_https_only():
    handler = P._HttpsRedirectOnly()
    req = urllib.request.Request("https://github.com/a/b/releases/download/v1/x")
    assert handler.redirect_request(req, None, 302, "Found", {}, "http://objects.example/x") is None
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://objects.example/x")


@pytest.mark.parametrize("outcome", [
    FakeResponse(b"{}", status=403), FakeResponse(b"{}", status=429),
    FakeResponse(b"x" * (P.MAX_API_BYTES + 1)),
    urllib.error.HTTPError(GOOD_URL, 403, "rate limited", {}, None),
    urllib.error.URLError("offline"), TimeoutError("slow"),
])
def test_http_failures_fail_closed(monkeypatch, outcome):
    monkeypatch.setattr(P, "_API_OPENER", FakeOpener(outcome))
    with pytest.raises(P.ProvenanceError):
        P.fetch_official(GOOD_URL)


def test_an_asset_larger_than_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(P, "_ASSET_OPENER", FakeOpener(FakeResponse(b"x" * (P.MAX_PROVENANCE_BYTES + 1))))
    with pytest.raises(P.ProvenanceError):
        P.fetch_release_asset("https://github.com/o/r/releases/download/v1.0.0.1/p.json")


# -- build time ---------------------------------------------------------------

def _git(head=COMMIT, describe=TAG, dirty=""):
    def git(*args):
        if args[0] == "describe":
            if describe is None:
                raise RuntimeError("no tag")
            return describe
        if args[0] == "rev-parse":
            return head
        if args[0] == "status":
            return dirty
        raise AssertionError(args)
    return git


def test_a_build_at_the_official_tag_gets_provenance():
    assert P.resolve_build_source(_git(), Api()) == (TAG, COMMIT)


def test_the_ci_tag_hint_is_used_when_git_has_no_tags():
    assert P.resolve_build_source(_git(describe=None), Api(), tag_hint=TAG) == (TAG, COMMIT)


def test_an_untagged_build_gets_no_provenance_and_asks_nothing():
    api = Api()
    assert P.resolve_build_source(_git(describe=None), api) is None
    assert api.urls == []


@pytest.mark.parametrize("git,api", [
    (_git(head=OTHER), Api()),                       # HEAD is not what the tag points at
    (_git(dirty=" M client/main.py"), Api()),        # modified tracked file
    (_git(), Api(tags={})),                          # tag not in the official repository
    (_git(describe="main"), Api()),                  # not a release tag
])
def test_a_tag_that_does_not_hold_stops_the_build(git, api):
    with pytest.raises(P.ProvenanceError):
        P.resolve_build_source(git, api)


# -- the Mac maintainer's own build on top of a release -------------------------

def _local_git(head=OTHER, dirty="", contains=True):
    def git(*args):
        if args[0] == "rev-parse":
            return head
        if args[0] == "status":
            return dirty
        if args[0] == "merge-base":
            assert args[1:] == ("--is-ancestor", COMMIT, head)
            if not contains:
                raise RuntimeError("not an ancestor")
            return ""
        raise AssertionError(args)
    return git


def test_a_local_build_names_the_release_it_contains_and_its_own_commit():
    assert P.resolve_local_build_source(_local_git(), Api(), TAG) == (TAG, OTHER)


@pytest.mark.parametrize("git,api,tag", [
    (_local_git(contains=False), Api(), TAG),          # HEAD does not contain the release
    (_local_git(dirty=" M client/main.py"), Api(), TAG),
    (_local_git(), Api(tags={}), TAG),                 # not an official tag
    (_local_git(), Api(), "main"),                     # not a release tag
])
def test_a_local_build_that_does_not_hold_stops(git, api, tag):
    with pytest.raises(P.ProvenanceError):
        P.resolve_local_build_source(git, api, tag)


def test_a_local_build_still_takes_every_newer_release_and_only_those():
    # running tag = the release it was built on; the commit differs from any release
    assert _verify(running=TAG, tag="v2.0.0.6", raw=_prov(version="v2.0.0.6"),
                   api=Api(tags={"v2.0.0.6": ("commit", COMMIT)}), running_commit=OTHER)[0]
    assert not _verify(running=TAG, running_commit=OTHER)[0]   # its own base is not newer


# -- the updater uses it --------------------------------------------------------

def test_the_updater_verifies_before_downloading_and_the_hash_before_extracting():
    """updater_mac needs PyObjC and wx, so its order of steps is read from
    source here (macos/tests run on a Mac only)."""
    import ast
    src = (_ROOT / "macos" / "winzapp_mac" / "updater_mac.py").read_text(encoding="utf-8")
    work = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "_work")
    first = {}
    for n in ast.walk(work):
        if isinstance(n, ast.Call):
            name = ast.unparse(n.func)
            first[name] = min(first.get(name, n.lineno), n.lineno)
    order = [first[c] for c in ("release_provenance", "_download",
                                "provenance.check_artifact", "verify_app")]
    assert order == sorted(order)
    assert 'enabled()' in src and "releases_repo(_info())" in src
    # the official repository is never taken from the plist or the provenance
    assert "OFFICIAL_REPO" not in src


# -- strictness -----------------------------------------------------------------

@pytest.mark.parametrize("value", ["v2.0.0.5\n", "v\u06632.0.0.5", "v2.0.0.5 ", "V2.0.0.5", "v2.0.0.5\x00"])
def test_a_tag_must_be_plain_ascii_without_trailing_characters(value):
    assert not P.is_release_tag(value)
    assert not _verify(_prov(version=value), tag=value)[0]


def test_a_commit_or_hash_with_a_trailing_newline_is_refused():
    assert not P.is_commit(COMMIT + "\n")
    assert not _verify(_prov(source_commit=COMMIT + "\n"))[0]
    assert not _verify(_prov(artifacts={ZIP: ZIP_SHA + "\n"}))[0]
    assert not _verify(_prov(artifacts={ZIP + "\n": ZIP_SHA}))[0]
    assert not _verify(_prov(artifacts={"\u0663.zip": ZIP_SHA}))[0]


def test_deeply_nested_json_is_a_refusal_not_a_crash():
    raw = b"[" * P.MAX_PROVENANCE_BYTES
    with pytest.raises(P.ProvenanceError):
        P.parse_provenance(raw)
    assert not _verify(raw)[0]
    assert not _verify(b'{"a":' * 3000 + b"1" + b"}" * 3000)[0]


def test_an_annotated_tag_answer_for_another_object_is_refused():
    """The tag object returned must be the one asked for: without that check
    a mirror could answer for a different, genuine tag object."""
    class Swapped(Api):
        def __call__(self, url):
            if "/git/tags/" in url:
                return json.dumps({"sha": OTHER, "object": {"type": "commit", "sha": COMMIT}}).encode()
            return super().__call__(url)
    ok, detail = _verify(api=Swapped(tags={TAG: ("tag", TAG_OBJ)}))
    assert not ok and "does not match" in detail


def test_offline_rate_limits_and_server_errors_keep_distinct_causes(monkeypatch):
    for outcome, code in ((urllib.error.HTTPError(GOOD_URL, 403, "x", {}, None), "access_denied"),
                          (urllib.error.HTTPError(GOOD_URL, 429, "x", {}, None), "rate_limited"),
                          (urllib.error.URLError("offline"), "connection"), (TimeoutError("slow"), "timeout"),
                          (urllib.error.HTTPError(GOOD_URL, 503, "x", {}, None), "server")):
        P._RATE_LIMITS.clear()
        monkeypatch.setattr(P, "_API_OPENER", FakeOpener(outcome))
        with pytest.raises(P.ProvenanceError) as caught:
            P.fetch_official(GOOD_URL)
        assert caught.value.code == code


def test_a_slow_trickle_hits_the_total_deadline(monkeypatch):
    clock = iter([0, 0, 1000, 1000, 1000])
    monkeypatch.setattr(P.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(P, "_API_OPENER", FakeOpener(FakeResponse(b"x" * 10)))
    with pytest.raises(P.ProvenanceError, match="try again later"):
        P.fetch_official(GOOD_URL)


# -- the real opener, against a local server -------------------------------------

def _serve(handler_cls):
    import http.server
    import threading
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _ask_locally(status):
    """The verifier's real opener against a 127.0.0.1 server answering
    *status* (the production https-only check is in fetch_official; _read is
    the part that opens and judges the answer)."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "https://evil.example/x")
            self.end_headers()
            if status == 200:
                self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    server = _serve(Handler)
    try:
        return P._read(P._API_OPENER, f"http://127.0.0.1:{server.server_address[1]}/", 1000, {})
    finally:
        server.shutdown()
        server.server_close()


def test_the_real_api_opener_refuses_a_redirect():
    with pytest.raises(P.ProvenanceError, match="HTTP 302"):
        _ask_locally(302)


def test_the_real_opener_refuses_an_answer_that_is_not_https():
    with pytest.raises(P.ProvenanceError, match="HTTPS"):
        _ask_locally(200)


def test_the_running_version_comes_from_the_plist_not_from_version_py():
    updater = (_ROOT / "macos" / "winzapp_mac" / "updater_mac.py").read_text(encoding="utf-8")
    build = (_ROOT / "macos" / "build_app.py").read_text(encoding="utf-8")
    assert "__version__" not in updater and "from version" not in updater
    assert "running_release_tag" in updater
    assert "WinZappReleaseTag" in build and "WinZappSourceCommit" in build
    assert P.RELEASE_TAG_KEY == "WinZappReleaseTag"


# -- the budgets are real -------------------------------------------------------

class _Sock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class _Raw:
    def __init__(self, sock):
        self._sock = sock


class _Fp:
    def __init__(self, sock):
        self.raw = _Raw(sock)


class SlowResponse(FakeResponse):
    """Every read1 takes *step* seconds of a fake clock and returns a byte."""

    def __init__(self, clock, step, body=b"x" * 100000, headers=None):
        super().__init__(body)
        self.clock, self.step = clock, step
        self.sock = _Sock()
        self.fp = _Fp(self.sock)
        self.headers = headers or {}
        self.status = 200

    def read1(self, n=-1):
        self.clock[0] += self.step
        return super().read(1)


def _fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(P.time, "monotonic", lambda: clock[0])
    return clock


def test_a_read_that_outlives_the_deadline_aborts_and_each_read_gets_only_what_is_left(monkeypatch):
    clock = _fake_clock(monkeypatch)
    resp = SlowResponse(clock, step=14)
    monkeypatch.setattr(P, "_API_OPENER", FakeOpener(resp))
    with pytest.raises(P.ProvenanceError, match="try again later"):
        P.fetch_official(GOOD_URL)
    # 30 s budget, 14 s per byte: the second read may wait 16 s at most, never the full 15 s
    assert resp.sock.timeouts and all(t <= P.TIMEOUT for t in resp.sock.timeouts)
    assert resp.sock.timeouts[1] == pytest.approx(16) or resp.sock.timeouts[1] <= 16


def test_download_aborts_at_the_size_cap_and_leaves_no_partial_file(monkeypatch, tmp_path):
    clock = _fake_clock(monkeypatch)
    dest = tmp_path / ZIP
    monkeypatch.setattr(P, "_ASSET_OPENER", FakeOpener(SlowResponse(clock, step=0, body=b"x" * 5000)))
    with pytest.raises(P.ProvenanceError, match="larger"):
        P.download_file("https://github.com/o/r/releases/download/v1/z.zip", str(dest), limit=1000)
    assert not dest.exists()


def test_download_refuses_a_declared_size_over_the_cap(monkeypatch, tmp_path):
    clock = _fake_clock(monkeypatch)
    dest = tmp_path / ZIP
    resp = SlowResponse(clock, step=0, headers={"content-length": "5000"})
    monkeypatch.setattr(P, "_ASSET_OPENER", FakeOpener(resp))
    with pytest.raises(P.ProvenanceError, match="larger"):
        P.download_file("https://github.com/o/r/z.zip", str(dest), limit=1000)
    assert not dest.exists()


def test_download_aborts_at_the_deadline_and_leaves_no_partial_file(monkeypatch, tmp_path):
    clock = _fake_clock(monkeypatch)
    dest = tmp_path / ZIP
    monkeypatch.setattr(P, "_ASSET_OPENER", FakeOpener(SlowResponse(clock, step=50)))
    with pytest.raises(P.ProvenanceError, match="try again later"):
        P.download_file("https://github.com/o/r/z.zip", str(dest), deadline=120)
    assert not dest.exists()


def test_download_removes_the_partial_file_on_any_error(monkeypatch, tmp_path):
    clock = _fake_clock(monkeypatch)
    dest = tmp_path / ZIP

    class Dies(SlowResponse):
        def read1(self, n=-1):
            if self.tell():
                raise ConnectionResetError("gone")
            return super().read(10)
    monkeypatch.setattr(P, "_ASSET_OPENER", FakeOpener(Dies(clock, 0)))
    with pytest.raises(ConnectionResetError):
        P.download_file("https://github.com/o/r/z.zip", str(dest))
    assert not dest.exists()


def test_download_writes_the_file_and_reports_progress(monkeypatch, tmp_path):
    clock = _fake_clock(monkeypatch)
    dest = tmp_path / ZIP
    seen = []
    resp = SlowResponse(clock, step=0, body=b"y" * 50, headers={"content-length": "50"})
    monkeypatch.setattr(P, "_ASSET_OPENER", FakeOpener(resp))
    P.download_file("https://github.com/o/r/z.zip", str(dest), seen.append)
    assert dest.read_bytes() == b"y" * 50 and seen[-1] == 100


def test_download_is_https_only(tmp_path):
    with pytest.raises(P.ProvenanceError):
        P.download_file("http://github.com/o/r/z.zip", str(tmp_path / "z"))


def test_updater_downloads_through_the_capped_function_and_cleans_its_work_dir():
    src = (_ROOT / "macos" / "winzapp_mac" / "updater_mac.py").read_text(encoding="utf-8")
    assert "provenance.download_file(" in src and "requests" not in src
    assert "shutil.rmtree(work, ignore_errors=True)" in src and "handed_over" in src


# -- the releases repository name -------------------------------------------------

@pytest.mark.parametrize("value", ["gabrielhhaber/WinZapp_Python", "rocco-labs/WinZapp_Python",
                                   "a/b", "owner/name.with.dots", "o/_x-1"])
def test_valid_repositories(value):
    assert P.valid_repo(value)


@pytest.mark.parametrize("value", [
    "owner/name\n", "owner/name ", "../name", "owner/..", "owner/.hidden", "owner/a..b",
    "ow..ner/name", ".owner/name", "-owner/name", "owner/name/extra", "owner", "", "/name",
    "owner/", "٣wner/name", "owner/näme", None, 5, "o" * 40 + "/name",
])
def test_invalid_repositories(value):
    assert not P.valid_repo(value)
