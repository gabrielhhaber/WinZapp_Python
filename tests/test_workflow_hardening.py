"""The release pipeline's security properties, pinned so a later edit cannot
quietly undo one.

Each of these is invisible when it breaks: a workflow with an unpinned action
still runs, a stable pipeline that publishes directly still ships, an alpha
that forgets to upload its signature still publishes — and the first sign of
trouble is on users' machines. See CLAUDE.md, "Release integrity".

Read as text rather than parsed as YAML: PyYAML is not a dependency here, and
the properties below are all visible on single lines.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _jobs(text):
    """{job name: job text}, split on two-space-indented job keys."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\s*$", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def test_every_third_party_action_is_pinned_to_a_commit_sha():
    offenders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"^\s*(?:-\s*)?uses:\s*(\S+)", line)
            if not match or match.group(1).startswith("./"):
                continue
            if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", match.group(1)):
                offenders.append(f"{path.name}:{number}: {match.group(1)}")
    assert not offenders, "Pin these to a full commit SHA:\n" + "\n".join(offenders)


def test_stable_pipeline_starts_from_a_tag_and_only_ever_leaves_a_draft():
    text = _read("release.yml")
    triggers = text.split("\njobs:\n", 1)[0]
    assert not re.search(r"^  release:", triggers, re.MULTILINE), "a draft never fires `release: created`"
    assert re.search(r"^  push:\s*\n    tags:", text, re.MULTILINE)
    assert '"!v*alpha*"' in text
    assert "draft: true" in text
    assert "draft: false" not in text and "draft=false" not in text


def test_stable_pipeline_never_touches_the_bump_token():
    assert "RELEASE_BUMP_TOKEN" not in _read("release.yml")


def test_bump_token_is_only_read_from_the_release_environment():
    for path in WORKFLOWS.glob("*.yml"):
        for job_name, job in _jobs(path.read_text(encoding="utf-8")).items():
            if "RELEASE_BUMP_TOKEN" in job:
                assert re.search(r"^    environment: release\s*$", job, re.MULTILINE), (
                    f"{path.name}:{job_name} reads RELEASE_BUMP_TOKEN outside the release environment"
                )


def test_alpha_signing_key_is_only_read_from_its_environment():
    for path in WORKFLOWS.glob("*.yml"):
        for job_name, job in _jobs(path.read_text(encoding="utf-8")).items():
            if "ALPHA_SIGNING_KEY" in job:
                assert re.search(r"^    environment: alpha-release\s*$", job, re.MULTILINE), (
                    f"{path.name}:{job_name} reads ALPHA_SIGNING_KEY outside the alpha-release environment"
                )


def test_no_publish_job_checks_out_after_downloading_its_assets():
    """actions/checkout empties the workspace. Placed after the download it
    deleted dist/, and an alpha was published with no assets."""
    for name in ("alpha-release.yml", "release.yml"):
        publish = _jobs(_read(name))["publish"]
        download = publish.index("actions/download-artifact@")
        checkout = publish.find("actions/checkout@")
        assert checkout == -1 or checkout < download, f"{name}: checkout after download wipes dist/"


def test_alpha_refuses_to_publish_without_its_assets():
    publish = _jobs(_read("alpha-release.yml"))["publish"]
    verify = publish.index("Verify the required assets are present")
    assert verify < publish.index("softprops/action-gh-release@")
    for asset in ("dist/WinZappInstaller.exe", "dist/WinZapp.zip", "dist/SHA256SUMS.txt"):
        assert asset in publish[verify:publish.index("softprops/action-gh-release@")]


def test_alpha_is_signed_before_its_draft_is_created_and_uploads_the_signature():
    publish = _jobs(_read("alpha-release.yml"))["publish"]
    sign = publish.index("release_signing.py ci-sign dist/SHA256SUMS.txt")
    draft = publish.index("softprops/action-gh-release@")
    publish_step = publish.index("-F draft=false")
    assert sign < draft < publish_step
    assert "dist/SHA256SUMS.txt.sig" in publish


def test_alphas_still_publish_on_every_push_to_main():
    """The automation the signing work had to keep: no reviewer gate can be
    expressed in the workflow itself, but the trigger and the unattended
    publish can."""
    text = _read("alpha-release.yml")
    assert re.search(r"^  push:\s*\n    branches: \[main\]", text, re.MULTILINE)
    assert "-F draft=false" in _jobs(text)["publish"]


def test_manifest_declares_its_version_for_versioned_builds():
    text = _read("build-windows.yml")
    assert "# winzapp-version: $($ver.ToLower())" in text


def test_version_bump_runs_only_after_a_stable_release_is_published():
    text = _read("release-published.yml")
    assert re.search(r"^  release:\s*\n    types: \[published\]", text, re.MULTILINE)
    assert "!contains(github.event.release.tag_name, 'alpha')" in text
