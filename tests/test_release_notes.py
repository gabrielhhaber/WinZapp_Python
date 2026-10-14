"""The updater's "What's new?" for the release on offer (issue #488)."""

import updater
from core import release_notes

BODY = """## What's Changed
* Show a verified business's name instead of its phone number by @reffehcsoibaf in https://github.com/gabrielhhaber/WinZapp_Python/pull/484
* Fix updater codec failures by @m-enes-senovali in https://github.com/gabrielhhaber/WinZapp_Python/pull/485

## New Contributors
* @someone made their first contribution in https://github.com/gabrielhhaber/WinZapp_Python/pull/480

**Full Changelog**: https://github.com/gabrielhhaber/WinZapp_Python/compare/v2.1.0.4370alpha...v2.1.0.4391alpha"""

KEYS = {
    "whats_new_pull_request": "{title} (pull request {number})",
    "changelog_diff_version": "Version {version}",
    "changelog_diff_new_version": "New version {version}, in full:",
    "changelog_diff_added": "New in the section {section}:",
    "changelog_diff_changed": "Changed in the section {section}, as it now reads:",
    "changelog_diff_removed": "Removed from the section {section}:",
    "changelog_diff_summary": "Version summary",
    "whats_new_changelog_heading": "Release notes:",
    "whats_new_github_heading": "On GitHub:",
}


def t(key):
    return KEYS[key]


INSTALLED = "V2.1.0.0\n\nSummary.\n\nWHAT'S NEW\n\n- Calls.\n"
OFFERED = "V2.1.0.0\n\nSummary.\n\nWHAT'S NEW\n\n- Calls.\n\n- Communities are listed.\n"


class TestTheGithubBody:
    def test_pull_requests_read_as_titles_and_numbers(self):
        text = release_notes.readable_body(BODY, t)
        assert text.splitlines() == [
            "- Show a verified business's name instead of its phone number (pull request 484)",
            "- Fix updater codec failures (pull request 485)",
        ]

    def test_alphas_between_the_two_versions_newest_first(self):
        releases = [
            {"tag_name": "v2.1.0.4391alpha", "body": BODY},
            {"tag_name": "v2.1.0.4370alpha", "body": "* Older by @a in https://x/pull/1"},
            {"tag_name": "v2.1.0.4365alpha", "body": "* Installed by @a in https://x/pull/0"},
            {"tag_name": "v2.1.0.0", "body": "* Stable by @a in https://x/pull/2"},
        ]
        text = release_notes.github_notes(
            releases, updater.is_alpha_release,
            lambda v: updater.is_newer(v, "2.1.0.4365alpha"),
            lambda v: not updater.is_newer(v, "2.1.0.4391alpha"), t, updater._version_order)
        assert text.index("Version 2.1.0.4391alpha") < text.index("Version 2.1.0.4370alpha")
        assert "Older (pull request 1)" in text
        assert "Installed" not in text and "Stable" not in text


class TestFetchingTheOfferedChangelog:
    def test_the_users_language_then_english(self):
        asked = []

        def fetch(url):
            asked.append(url)
            if "pt-BR" in url:
                raise OSError("404")
            return "V1.0.0.0\n"

        assert release_notes.changelog_at(fetch, "o/r", "v2", "pt-BR") == ("en-US", "V1.0.0.0\n")
        assert asked == [
            "https://raw.githubusercontent.com/o/r/v2/client/changelog_pt-BR.txt",
            "https://raw.githubusercontent.com/o/r/v2/client/changelog_en-US.txt",
        ]

    def test_nothing_fetched_is_none(self):
        def fetch(url):
            raise OSError("offline")

        assert release_notes.changelog_at(fetch, "o/r", "v2", "pt-BR") is None


class TestTheUpdatersText:
    def test_an_alpha_shows_the_changelog_changes_and_the_github_notes(self, monkeypatch):
        monkeypatch.setattr(updater, "_read_changelog_file", lambda locale: INSTALLED)
        release = {"tag_name": "v2.1.0.4391alpha", "name": "WinZapp Alpha 2.1.0.4391alpha",
                   "body": BODY}
        text = updater.whats_new_text("2.1.0.4370alpha", release, [release], "pt-BR", t,
                                      lambda url: OFFERED)
        assert text.startswith("Release notes:\n\nVersion 2.1.0.0\n\nNew in the section WHAT'S NEW:"
                               "\n- Communities are listed.")
        assert "On GitHub:\n\nVersion 2.1.0.4391alpha\n- Show a verified business's name" in text

    def test_a_stable_release_has_no_github_part(self, monkeypatch):
        monkeypatch.setattr(updater, "_read_changelog_file", lambda locale: INSTALLED)
        release = {"tag_name": "v2.1.0.1", "body": BODY}
        text = updater.whats_new_text("2.1.0.0", release, [release], "pt-BR", t,
                                      lambda url: OFFERED)
        assert "On GitHub" not in text and "Communities are listed." in text

    def test_offline_it_falls_back_to_what_it_showed_before(self, monkeypatch):
        monkeypatch.setattr(updater, "_read_changelog_file", lambda locale: "")
        monkeypatch.setattr(updater, "load_changelog_text", lambda locale: "")

        def fetch(url):
            raise OSError("offline")

        release = {"tag_name": "v2.1.0.1", "body": "Plain notes."}
        assert updater.whats_new_text("2.1.0.0", release, [release], "pt-BR", t, fetch) == "Plain notes."
