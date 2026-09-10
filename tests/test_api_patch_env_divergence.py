"""WinZapp's config.ts must not start loading a .env, however upstream drifts.

wppconnect-server 2.10.19 (commit b66881b, "load and document optional
environment configuration") added `dotenv.config({ override: false })` to
src/config.ts and deleted the dead `//require('dotenv').config();` from
src/index.ts. `setup_api.py` restores WinZapp's own copies of both files over
whatever tag is checked out, so both changes are already overridden — this
pins that as a decision rather than an accident, because the natural instinct
on the next rebase is to merge upstream's version in.

Two reasons it must stay overridden.

*Inert*: this file is not a variant of upstream's. Upstream resolves every
setting from process.env (`env.SECRET_KEY || ...`, `env.PORT || '21465'`,
`envNumber('MAX_LISTENERS', 15)`); WinZapp hardcodes them and reads exactly one
environment variable. There is no `env` object here for a .env to feed.

*Unsafe*: that one variable is WINZAPP_USER_DATA_DIR — the path to the Chrome
profile carrying the WhatsApp login, computed per install and per account by
main.py. It is the single value that must never come from a file next to the
exe; pointing Chrome at the wrong userDataDir is the session-loss failure mode
this codebase has spent the most time on. dotenv's `override: false` protects a
variable we do inject, and says nothing about one we do not.

Consequence worth knowing: upstream's src/config.test.ts (added in the same
commit) fails all three of its cases against WinZapp's config.ts, by design.
Nothing here runs jest — setup_api.py and build.py only ever run
`npm run build` — so it costs nothing; the last test below pins that too.
"""

import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCHES = os.path.join(REPO_ROOT, "client", "api_patches")


def _read(*parts) -> str:
    with open(os.path.join(PATCHES, *parts), encoding="utf-8") as f:
        return f.read()


def _code_only(source: str) -> str:
    """Strip // comments and /* */ blocks, so a mention inside the explanatory
    note at the top of the file is not mistaken for the call itself."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("//")
    )


class TestConfigTsNeverLoadsAnEnvFile:
    def test_it_does_not_call_dotenv(self):
        code = _code_only(_read("src", "config.ts"))
        assert "dotenv" not in code, (
            "src/config.ts is loading a .env again. Upstream's version is "
            "harmless because every setting comes from process.env there; "
            "WinZapp's hardcodes them and reads only WINZAPP_USER_DATA_DIR, "
            "which must never come from a file on disk."
        )

    def test_it_still_reads_only_the_one_variable_that_justifies_the_rewrite(self):
        code = _code_only(_read("src", "config.ts"))
        used = set(re.findall(r"process\.env\.([A-Z_][A-Z0-9_]*)", code))
        used |= set(re.findall(r"\benv\.([A-Z_][A-Z0-9_]*)", code))
        assert used == {"WINZAPP_USER_DATA_DIR"}, (
            "config.ts reads environment variables other than "
            f"WINZAPP_USER_DATA_DIR ({sorted(used)}). Each one is a value a "
            "stray .env could reach the day dotenv is merged back in — decide "
            "deliberately, then update this test."
        )

    def test_the_port_and_secret_are_hardcoded_not_environment_driven(self):
        """The concrete difference from upstream, and why its config.test.ts
        cannot pass here."""
        code = _code_only(_read("src", "config.ts"))
        assert "port: '6300'" in code
        assert "secretKey: 'THISISMYSECURETOKEN'" in code

    def test_the_reason_is_written_down_in_the_file_itself(self):
        """A future rebase reads config.ts, not this test."""
        source = _read("src", "config.ts")
        assert "dotenv" in source, (
            "the note explaining why upstream's dotenv change is overridden "
            "is gone — without it the next rebase merges it back in"
        )


class TestNothingRunsJestAgainstTheVendoredServer:
    """The divergence above is free only as long as upstream's own tests never
    run. `npm run build` is `tsc` plus babel — neither executes an assertion."""

    @pytest.mark.parametrize("script", ["setup_api.py", "build.py"])
    def test_the_installers_only_ever_build(self, script):
        with open(os.path.join(REPO_ROOT, script), encoding="utf-8") as f:
            source = f.read()
        for forbidden in ('"test"]', "'test']", "npm test", "npx jest"):
            assert forbidden not in source, (
                f"{script} runs the vendored server's test suite; upstream's "
                "src/config.test.ts asserts .env behaviour WinZapp's config.ts "
                "deliberately does not have, so it would fail the build"
            )
