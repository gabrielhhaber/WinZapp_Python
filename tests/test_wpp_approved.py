"""Which approved WPPConnect Server release a build is offered (core/wpp_approved.py).

The list on main says, per release, the oldest pin whose patches run it
(``min_pin``). A build pinned at P is offered the newest release V with
``min_pin <= P < V``: older builds, ported before the last release that
changed a file WinZapp overrides, never see a release their patches do not
fit, and the list itself is validated here so a malformed edit fails CI
before every install reads it from main.
"""

import json
import os

import pytest

from core import wpp_approved
from core.wpp_approved import best_approved, parse_manifest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COMMITTED = os.path.join(_ROOT, "client", "wpp_approved_versions.json")


def _manifest(*entries, schema=1):
    return {"schema": schema, "approved": list(entries)}


def _best(manifest, pin, satisfies=lambda req: True):
    return best_approved(parse_manifest(manifest), pin, satisfies)


# 2.10.50 was ported in an old WinZapp; 2.10.58 changed an overridden file and
# was ported later; 2.10.51 and 2.10.59 changed dependencies only.
_LIST = _manifest(
    {"version": "2.10.51", "min_pin": "2.10.50"},
    {"version": "2.10.59", "min_pin": "2.10.58"},
)


@pytest.mark.parametrize("pin, expected", [
    ("2.10.49", ""),           # pinned before 2.10.50: its patches fit neither
    ("v2.10.50", "v2.10.51"),  # old build: the dependency release of its own line
    ("2.10.51", ""),           # already pinned at it
    ("2.10.55", ""),           # pinned after 2.10.51, before 2.10.58: nothing newer fits
    ("2.10.58", "v2.10.59"),   # new build: only the release after the breaking one
    ("2.10.60", ""),           # pinned past everything approved
])
def test_a_build_is_offered_only_what_its_pin_supports(pin, expected):
    assert _best(_LIST, pin) == expected


def test_the_newest_eligible_release_wins():
    manifest = _manifest(
        {"version": "2.10.59", "min_pin": "2.10.58"},
        {"version": "2.10.61", "min_pin": "2.10.58"},
        {"version": "2.10.60", "min_pin": "2.10.58"},
    )
    assert _best(manifest, "2.10.58") == "v2.10.61"


def test_min_winzapp_is_asked_only_when_present():
    manifest = _manifest(
        {"version": "2.10.59", "min_pin": "2.10.58"},
        {"version": "2.10.60", "min_pin": "2.10.58", "min_winzapp": "2.2.0.0"},
    )
    asked = []
    assert _best(manifest, "2.10.58", lambda req: asked.append(req) or False) == "v2.10.59"
    assert asked == ["2.2.0.0"]
    assert _best(manifest, "2.10.58", lambda req: True) == "v2.10.60"


@pytest.mark.parametrize("pin", ["", "garbage", None, "2.10"])
def test_no_usable_pin_offers_nothing(pin):
    assert _best(_LIST, pin) == ""


@pytest.mark.parametrize("data", [
    None, [], "text", {}, {"approved": []},
    _manifest({"version": "2.10.59", "min_pin": "2.10.58"}, schema=2),
    {"schema": 1, "approved": "2.10.59"},
])
def test_an_unknown_or_broken_manifest_approves_nothing(data):
    assert parse_manifest(data) == []


@pytest.mark.parametrize("entry", [
    "2.10.59",
    {"version": "2.10.59"},                                    # no min_pin
    {"version": "2.10.59-beta.1", "min_pin": "2.10.58"},       # pre-release
    {"version": "main", "min_pin": "2.10.58"},                 # a branch
    {"version": "../2.10.59", "min_pin": "2.10.58"},           # a path
    {"version": "2.10.57", "min_pin": "2.10.58"},              # min_pin after version
    {"version": "2.10.59", "min_pin": "2.10.58", "min_winzapp": ""},
    {"version": "2.10.59", "min_pin": "2.10.58", "min_winzapp": 3},
])
def test_a_malformed_entry_is_skipped_without_voiding_the_rest(entry):
    good = {"version": "2.10.60", "min_pin": "2.10.58"}
    parsed = parse_manifest(_manifest(entry, good))
    assert [str(v) for v, _p, _w in parsed] == ["2.10.60"]


def test_fetch_reads_main_and_treats_404_as_nothing_approved(monkeypatch):
    from core import tls_trust
    seen = []

    class _Resp:
        def __init__(self, status, body=None):
            self.status_code, self._body = status, body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

        def json(self):
            return self._body

    monkeypatch.setattr(tls_trust, "get",
                        lambda url, **kw: seen.append(url) or _Resp(404))
    assert wpp_approved.fetch_manifest("owner/repo", "WinZapp/test") == {}
    assert seen == ["https://raw.githubusercontent.com/owner/repo/main/"
                    "client/wpp_approved_versions.json"]

    monkeypatch.setattr(tls_trust, "get", lambda url, **kw: _Resp(500))
    with pytest.raises(RuntimeError):
        wpp_approved.fetch_manifest("owner/repo", "WinZapp/test")

    monkeypatch.setattr(tls_trust, "get", lambda url, **kw: _Resp(200, _LIST))
    assert wpp_approved.fetch_manifest("owner/repo", "WinZapp/test") == _LIST


@pytest.fixture(scope="module")
def data():
    with open(_COMMITTED, encoding="utf-8") as fh:
        return json.load(fh)


class TestTheCommittedList:
    """Every install reads this file from main: a broken edit must fail here."""

    def test_it_is_the_schema_installs_understand(self, data):
        assert data["schema"] == wpp_approved.SCHEMA

    def test_every_entry_is_well_formed(self, data):
        assert len(parse_manifest(data)) == len(data["approved"])

    def test_no_version_is_listed_twice(self, data):
        versions = [e["version"] for e in data["approved"]]
        assert len(versions) == len(set(versions))

    def test_the_path_matches_what_installs_fetch(self):
        assert _COMMITTED.replace(os.sep, "/").endswith(wpp_approved.MANIFEST_PATH)
