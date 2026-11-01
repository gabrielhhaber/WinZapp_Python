"""Commit provenance of a macOS release: does it declare an official tag and
commit that WinZapp's own repository confirms?

The Mac releases are published from another repository (Info.plist
WinZappMacReleasesRepo), so the Apple signature only says "the Mac
maintainer's Apple team signed this", never "this code went through
gabrielhhaber/WinZapp_Python". Each Mac release therefore DECLARES, in a
provenance file, an official tag and commit, and the updater checks that
declaration against the OFFICIAL repository before installing. It does not
prove the zip was built from that commit. Pure standard library on purpose: no PyObjC, no wx, so the
normal Windows `pytest` exercises it and macos/build_app.py (which runs
before any dependency is installed) can use it too.

Threat model
  Attacker: whoever controls the releases repository (a fork, its CI secrets,
  its release assets) and can publish any zip and any provenance there.
  Cannot: push a tag to the official repository, or make api.github.com
  answer for it. The official repository's write access is the trust root the
  Windows releases already rely on.
  Goal: a Mac release that cannot name one of our tags and its commit must
  not install. A stronger guarantee (the zip IS that commit's build) needs an
  attestation from a workflow of the official repository, or a maintainer
  signature over the zip hash like Windows.

What is verified (in this order, every step fails closed)
  1. The provenance file is well formed: exact keys, schema 1, source_repo is
     the pinned official repository, source_commit 40 lowercase hex, version a
     release tag, artifacts a small name -> sha256 map.
  2. Its version is the tag of the release being installed (no replaying the
     provenance of another release) and is newer than the running version,
     which is the official tag the app was built from (Info.plist
     WinZappReleaseTag), never client/version.py.
  3. Over HTTPS, no redirects, against api.github.com/repos/<OFFICIAL_REPO>
     only: the tag exists in the official repository and, peeled through
     annotated tag objects, points at exactly source_commit.
  4. The downloaded zip's sha256 equals artifacts[<zip name>].

Why the tag, and not "the commit exists in our repo"
  GitHub serves a fork's commits through the parent repository
  (/repos/<parent>/commits/<sha> answers 200 for them), so existence proves
  nothing. A tag can only be created in the official repository by someone
  with write access to it, which a fork's owner does not have. A compare
  against the default branch would additionally prove "reachable from main",
  but it costs another API call (the unauthenticated limit is 60 an hour) and
  would refuse a legitimate hotfix tag on another branch; the tag is already
  the maintainer's explicit act, so it is the check used.

What this does NOT prove
  That the zip was built from that commit. The releaser can build from a
  modified tree and name an honest commit. build_app.py refuses a dirty tree
  and a HEAD that is not the tag, but that runs on the releaser's machine.
  Only a reproducible build or a CI attestation checked by the updater
  (docs/reference/macos.md, "Commit provenance") would prove it. Tags can
  also be created or moved by anyone with write access: the tag rules of the
  official repository are the real limit. Known gap: the ruleset "Allow tag
  creation only for admins" excludes refs/tags/v*alpha, so a collaborator with
  write access can create or move an alpha tag. Recommended to the maintainer:
  cover alpha tags too (or restrict them to the alpha workflow).
"""

import hashlib
import http.client
import json
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

# Pinned here: not read from the plist, not from the provenance file.
OFFICIAL_REPO = "gabrielhhaber/WinZapp_Python"
API_HOST = "api.github.com"
SCHEMA = 1
SOURCE_COMMIT_KEY = "WinZappSourceCommit"      # Info.plist, written by build_app.py
RELEASE_TAG_KEY = "WinZappReleaseTag"          # the official tag it was built from

MAX_PROVENANCE_BYTES = 16 * 1024
MAX_API_BYTES = 256 * 1024
TIMEOUT = 15                                   # per socket read
DEADLINE = 30                                  # whole request, however slowly it trickles
MAX_ARTIFACTS = 8
MAX_ZIP_BYTES = 2 << 30                        # the app is well under 1 GB; more is not our build
DOWNLOAD_DEADLINE = 30 * 60
CHUNK = 16 * 1024                              # small, so the deadline is looked at often
MAX_TAG_DEPTH = 4                              # annotated tag -> tag -> ... -> commit

_KEYS = {"schema", "version", "source_repo", "source_commit", "artifacts"}
# fullmatch + ASCII: "$" would let a trailing newline through, and \d would
# take digits of other scripts.
_SHA1 = re.compile(r"[0-9a-f]{40}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_TAG = re.compile(r"v(\d{1,9})\.(\d{1,9})\.(\d{1,9})\.(\d{1,9})(alpha|beta)?", re.ASCII)
# GitHub's charset: owner letters/digits/hyphens, no leading hyphen; name
# letters/digits/._- with no leading dot and no "..".
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.)(?!.*\.\.)[A-Za-z0-9._-]{1,100}", re.ASCII)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", re.ASCII)
_PRE = {"alpha": 1, "beta": 2, "": 3}


class ProvenanceError(Exception):
    """Anything that stops a release from being verified."""

    def __init__(self, message, *, code="", retry_after=0):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after

    def detail(self, stage):
        detail = VerificationDetail(f"could not verify commit provenance: {self}")
        detail.code, detail.stage, detail.retry_after = self.code, stage, self.retry_after
        return detail


class VerificationDetail(str):
    """Keep safe network metadata until the UI translates it; still a str for logs."""


def provenance_name(arch):
    """One file per architecture, so the two build jobs never have to merge
    or overwrite a shared one."""
    return f"WinZapp-macOS-provenance-{arch}.json"


def valid_repo(value):
    """True for an "owner/name" GitHub repository (the plist's
    WinZappMacReleasesRepo), nothing looser."""
    return isinstance(value, str) and bool(_REPO.fullmatch(value))


def is_commit(value):
    return isinstance(value, str) and bool(_SHA1.fullmatch(value))


def is_release_tag(value):
    return isinstance(value, str) and bool(_TAG.fullmatch(value))


def _tag_key(tag):
    m = _TAG.fullmatch(tag)
    return tuple(int(m.group(i)) for i in range(1, 5)), _PRE[m.group(5) or ""]


def tag_is_newer(tag, running_tag):
    """Is release *tag* strictly newer than *running_tag*, both release tags
    ("v2.0.0.5", "v2.0.0.5alpha")? The numbers compare as integers and an
    alpha or beta is older than the stable of the same number. An invalid
    tag on either side, or an equal one, is never newer."""
    if not is_release_tag(tag) or not is_release_tag(running_tag):
        return False
    return _tag_key(tag) > _tag_key(running_tag)


def bundle_version(tag):
    """CFBundleVersion of release *tag*, or "" for anything that is not one.

    Sparkle decides "newer" (and its installer refuses a downgrade) by
    comparing CFBundleVersion numerically, part by part, so this must order
    exactly as tag_is_newer() does: v2.1.0.4258alpha < v2.1.0.4258beta <
    v2.1.0.4258 < v2.1.0.4259alpha. The channel goes into the last digit of
    the fourth number (alpha 1, beta 2, stable 3): "2.1.0.42581". Four
    integers, the shape CFBundleShortVersionString already ships notarized
    in, rather than a fifth part Apple's format does not describe."""
    if not is_release_tag(tag):
        return ""
    nums, pre = _tag_key(tag)
    return f"{nums[0]}.{nums[1]}.{nums[2]}.{nums[3] * 10 + pre}"


def running_release_tag(info):
    """The official tag this app was built from (Info.plist WinZappReleaseTag,
    written by build_app.py), or "" for a development build. The running
    version is this and nothing else: client/version.py is the unstamped
    placeholder in every tagged commit (CI stamps it only at build), so it
    cannot tell an old release from a new one."""
    tag = info.get(RELEASE_TAG_KEY)
    return tag if is_release_tag(tag) else ""


def tag_version(tag):
    """The version a release tag names, as client/version.py spells it
    ("v2.1.0.4050alpha" -> "2.1.0.4050alpha"), or "" for anything that is
    not a release tag."""
    return tag[1:] if is_release_tag(tag) else ""


# -- the file -------------------------------------------------------------------

def parse_provenance(raw):
    """The validated provenance dict, or ProvenanceError."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_PROVENANCE_BYTES:
        raise ProvenanceError("provenance file missing or too large")
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ProvenanceError("provenance file is not valid JSON")
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise ProvenanceError("provenance file has unexpected fields")
    if data["schema"] != SCHEMA or isinstance(data["schema"], bool):
        raise ProvenanceError("provenance schema is not supported")
    if data["source_repo"] != OFFICIAL_REPO:
        raise ProvenanceError("provenance names a different source repository")
    if not is_commit(data["source_commit"]):
        raise ProvenanceError("provenance source_commit is not a full commit id")
    if not is_release_tag(data["version"]):
        raise ProvenanceError("provenance version is not a release tag")
    arts = data["artifacts"]
    if not isinstance(arts, dict) or not 1 <= len(arts) <= MAX_ARTIFACTS:
        raise ProvenanceError("provenance artifacts are malformed")
    for name, digest in arts.items():
        if not (isinstance(name, str) and _NAME.fullmatch(name)
                and isinstance(digest, str) and _SHA256.fullmatch(digest)):
            raise ProvenanceError("provenance artifacts are malformed")
    return data


def build_provenance(tag, commit, artifacts):
    """The JSON bytes build_app.py writes next to the zip."""
    data = {"schema": SCHEMA, "version": tag, "source_repo": OFFICIAL_REPO,
            "source_commit": commit, "artifacts": dict(artifacts)}
    raw = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    parse_provenance(raw)                       # never emit what the updater would refuse
    return raw


# -- the official repository ----------------------------------------------------

def _api(path):
    return f"https://{API_HOST}/repos/{OFFICIAL_REPO}/{path}"


def _json(fetch, url):
    try:
        data = json.loads(fetch(url).decode("utf-8"))
    except ProvenanceError:
        raise
    except Exception as exc:
        raise ProvenanceError(f"unreadable answer from {API_HOST}: {type(exc).__name__}")
    if not isinstance(data, dict):
        raise ProvenanceError(f"unexpected answer from {API_HOST}")
    return data


def _object(data):
    obj = data.get("object")
    if (not isinstance(obj, dict) or obj.get("type") not in ("commit", "tag")
            or not is_commit(obj.get("sha"))):
        raise ProvenanceError("unexpected object in the tag answer")
    return obj["type"], obj["sha"]


def resolve_official_tag(tag, fetch):
    """The commit our repository's tag *tag* points at (annotated tags
    peeled), or ProvenanceError when the tag is missing or odd. *fetch(url)*
    returns the body bytes or raises."""
    if not is_release_tag(tag):
        raise ProvenanceError("not a release tag")
    ref = _json(fetch, _api(f"git/ref/tags/{tag}"))
    if ref.get("ref") != f"refs/tags/{tag}":
        raise ProvenanceError("the official repository has no such tag")
    kind, sha = _object(ref)
    for _ in range(MAX_TAG_DEPTH):
        if kind == "commit":
            return sha
        tag_obj = _json(fetch, _api(f"git/tags/{sha}"))
        if tag_obj.get("sha") != sha:
            raise ProvenanceError("tag object answer does not match the request")
        kind, sha = _object(tag_obj)
    raise ProvenanceError("tag chain is too deep")


def resolve_build_source(git, fetch, tag_hint=""):
    """Build time (macos/build_app.py): (tag, commit) when HEAD is exactly the
    commit our repository's release tag points at, None for an untagged
    development build (no provenance is made, and no updater accepts such a
    build). Raises ProvenanceError for a tag that does not hold: a different
    HEAD, uncommitted changes to tracked files, a tag we do not have.

    *git(*args)* returns git's stdout stripped, raising on failure; *tag_hint*
    is the tag a CI checkout names (it may not have fetched tags). Untracked
    files are not looked at: this is a guard for an honest releaser, not a
    proof (see the module docstring)."""
    tag = tag_hint
    if not tag:
        try:
            tag = git("describe", "--exact-match", "--tags", "HEAD")
        except Exception:
            return None
    if not is_release_tag(tag):
        raise ProvenanceError(f"{tag!r} is not a release tag")
    head = git("rev-parse", "HEAD")
    if not is_commit(head):
        raise ProvenanceError("HEAD is not a full commit id")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ProvenanceError("tracked files differ from HEAD; build from a clean checkout")
    official = resolve_official_tag(tag, fetch)
    if official != head:
        raise ProvenanceError(
            f"HEAD is {head[:12]} but {OFFICIAL_REPO}'s {tag} is {official[:12]}")
    return tag, head


def verify_release(raw, tag, running_tag, fetch, running_commit=""):
    """(ok, detail) before anything is downloaded: the provenance in *raw* is
    valid, belongs to release *tag*, is newer than *running_tag*, and the
    official repository's tag points at its source_commit. Nothing raises."""
    try:
        data = parse_provenance(raw)
        if data["version"] != tag:
            raise ProvenanceError("provenance belongs to another release")
        if not tag_is_newer(tag, running_tag):
            raise ProvenanceError("release is not newer than the running version")
        if running_commit and data["source_commit"] == running_commit:
            raise ProvenanceError("release is the commit already running")
        got = resolve_official_tag(tag, fetch)
        if got != data["source_commit"]:
            raise ProvenanceError(
                "the release tag in the official repository points at another commit")
    except ProvenanceError as exc:
        return False, exc.detail("official_tag")
    return True, "ok"


def check_artifact(raw, name, path):
    """(ok, detail): the file at *path* has the sha256 *raw*'s provenance lists
    for *name*."""
    try:
        want = parse_provenance(raw)["artifacts"].get(name)
        if not want:
            raise ProvenanceError(f"provenance does not list {name}")
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            raise ProvenanceError(f"{name} does not match its provenance hash")
    except (ProvenanceError, OSError) as exc:
        return False, f"could not verify commit provenance: {exc}"
    return True, "ok"


# -- HTTP -----------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                             # surfaces as an HTTPError


class _HttpsRedirectOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _tls_context():
    """A verifying context that holds certificates on every Mac.

    The bundled OpenSSL looks for its CA file under
    /Library/Frameworks/Python.framework, a path compiled into the python.org
    build. It exists only on a Mac with that Python installed, which every
    build machine has and most users' do not: there, every check here failed
    as a TLS error. certifi travels inside the app.
    """
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return context


_TLS = urllib.request.HTTPSHandler(context=_tls_context())
_API_OPENER = urllib.request.build_opener(_TLS, _NoRedirect)
_ASSET_OPENER = urllib.request.build_opener(_TLS, _HttpsRedirectOnly)


UNREACHABLE = "GitHub unreachable or rate-limited, try again later"
_RATE_LIMITS = {}                 # host -> monotonic deadline; never a cached positive verdict
_RATE_LOCK = threading.Lock()


def _rate_wait(headers):
    """Read only bounded numeric/date hints, never an error body or URL."""
    waits = []
    for field, value in headers.items():
        value = str(value).strip()
        if len(value) > 64:
            continue
        if field.lower() == "retry-after":
            try:
                wait = int(value) if re.fullmatch(r"[0-9]{1,10}", value) else (
                    parsedate_to_datetime(value).timestamp() - time.time())
                waits.append(max(1, math.ceil(wait)))
            except (ValueError, TypeError, OverflowError, OSError):
                pass
        elif field.lower() == "x-ratelimit-reset" and headers.get("x-ratelimit-remaining") == "0":
            if re.fullmatch(r"[0-9]{1,10}", value):
                waits.append(max(1, math.ceil(int(value) - time.time())))
    return max(waits, default=60)


def _http_failure(status, headers):
    headers = {key.lower(): str(value) for key, value in (headers or {}).items()}
    if status == 429 or (status == 403 and (
            headers.get("x-ratelimit-remaining") == "0" or headers.get("retry-after"))):
        return ProvenanceError(UNREACHABLE + " (rate_limited)", code="rate_limited",
                               retry_after=_rate_wait(headers))
    code = ("access_denied" if status == 403 else "not_found" if status == 404
            else "server" if status >= 500 else "http_error")
    wait = _rate_wait(headers) if code == "server" and headers.get("retry-after") else 0
    return ProvenanceError(f"HTTP {status}", code=code, retry_after=wait)


def _network_failure(exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    code = ("tls" if isinstance(reason, ssl.SSLError) else
            "dns" if isinstance(reason, socket.gaierror) else
            "timeout" if isinstance(reason, TimeoutError) else
            "connection" if isinstance(reason, (OSError, http.client.HTTPException))
            or isinstance(exc, urllib.error.URLError) else "unexpected")
    return ProvenanceError(f"{UNREACHABLE} ({code})", code=code)


def _limit_wait(resp, remaining):
    """Shrink the socket timeout of *resp* to what is left of the budget, so
    one stalled read cannot outlive it. Best effort: a response without a
    reachable socket (a test fake) keeps the timeout it was opened with."""
    try:
        resp.fp.raw._sock.settimeout(max(0.05, min(TIMEOUT, remaining)))
    except Exception:
        pass


def _chunks(resp, deadline_at, size=CHUNK):
    """The body of *resp* in small chunks, within the deadline: each read
    gets at most the time that is left (a server sending a byte every few
    seconds cannot hold one read open past it), and read1 returns whatever has
    arrived instead of waiting to fill the chunk."""
    read = getattr(resp, "read1", resp.read)
    while True:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise ProvenanceError(UNREACHABLE, code="timeout")
        _limit_wait(resp, remaining)
        data = read(size)
        if not data:
            return
        yield data


def _read(opener, url, limit, headers):
    """At most two small GETs sharing one deadline. No retry for a trust refusal.

    Quota responses delay subsequent checks on the same host in this process;
    no sleep through a rate-limit window, and no cached verification success.
    """
    req = urllib.request.Request(url, headers=dict(headers, **{"User-Agent": "WinZapp-macOS"}))
    host = urllib.parse.urlsplit(url).netloc
    with _RATE_LOCK:
        wait = math.ceil(_RATE_LIMITS.get(host, 0) - time.monotonic())
    if wait > 0:
        raise ProvenanceError(UNREACHABLE + " (rate_limited)", code="rate_limited", retry_after=wait)
    deadline_at = time.monotonic() + DEADLINE
    for attempt in range(2):
        try:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise ProvenanceError(UNREACHABLE, code="timeout")
            with opener.open(req, timeout=min(TIMEOUT, remaining)) as resp:
                if resp.status != 200:
                    raise _http_failure(resp.status, getattr(resp, "headers", {}))
                final = resp.geturl()
                if not final.lower().startswith("https://"):
                    raise ProvenanceError("answer did not come over HTTPS")
                chunks, size = [], 0
                for chunk in _chunks(resp, deadline_at):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise ProvenanceError("answer is too large")
                return b"".join(chunks), final
        except ProvenanceError as exc:
            failure = exc
        except urllib.error.HTTPError as exc:
            failure = _http_failure(exc.code, exc.headers)
            exc.close()
        except Exception as exc:
            failure = _network_failure(exc)
        if failure.code == "rate_limited":
            with _RATE_LOCK:
                _RATE_LIMITS[host] = max(_RATE_LIMITS.get(host, 0),
                                         time.monotonic() + failure.retry_after)
        if attempt or failure.retry_after or failure.code not in {"server", "connection", "dns", "timeout"} or (
                deadline_at - time.monotonic() <= 1):
            raise failure from None
        time.sleep(1)              # only the verification worker, never the UI thread


def download_file(url, dest, progress=None, opener=None, limit=MAX_ZIP_BYTES,
                  deadline=DOWNLOAD_DEADLINE):
    """Download *url* (https, redirects to https only) to *dest* within *limit*
    bytes and *deadline* seconds in total. On any failure the partial file is
    deleted before the error goes on. *progress* gets 0-100 when the size is
    known."""
    if not url.startswith("https://"):
        raise ProvenanceError("downloads are https only")
    deadline_at = time.monotonic() + deadline
    req = urllib.request.Request(url, headers={"User-Agent": "WinZapp-macOS"})
    try:
        with (opener or _ASSET_OPENER).open(req, timeout=TIMEOUT) as resp:
            if resp.status != 200 or not resp.geturl().lower().startswith("https://"):
                raise ProvenanceError(f"download refused (HTTP {resp.status})")
            try:
                total = int(resp.headers.get("content-length") or 0)
            except ValueError:
                total = 0
            if total > limit:
                raise ProvenanceError("the download is larger than any WinZapp build")
            done = 0
            with open(dest, "wb") as fh:
                for chunk in _chunks(resp, deadline_at, 1 << 16):
                    done += len(chunk)
                    if done > limit:
                        raise ProvenanceError("the download is larger than any WinZapp build")
                    fh.write(chunk)
                    if total and progress:
                        progress(min(100, int(done * 100 / total)))
    except BaseException:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise


def fetch_official(url):
    """GET from the official API: https, api.github.com only, no redirect,
    size-capped, unauthenticated (no token is ever sent or logged)."""
    if not url.startswith(f"https://{API_HOST}/repos/{OFFICIAL_REPO}/"):
        raise ProvenanceError("refusing to ask anything but the official repository")
    body, final = _read(_API_OPENER, url, MAX_API_BYTES,
                        {"Accept": "application/vnd.github+json"})
    if final != url:
        raise ProvenanceError("answer came from another address")
    return body


def fetch_release_asset(url):
    """GET a release asset from the releases repository. Downloads redirect
    to GitHub's storage hosts, so https redirects are followed; the content
    is untrusted until verify_release() and check_artifact() pass."""
    if not url.startswith("https://github.com/"):
        raise ProvenanceError("release assets come from github.com")
    return _read(_ASSET_OPENER, url, MAX_PROVENANCE_BYTES, {})[0]
