"""Where a message's media is cached, answered in one place.

Audio does not live where the other media do: `handle_audio_message()` writes
`voice_messages/<id>.msv`, `handle_media_message()` writes
`media/<id>.wzmedia`. Three call sites resolved that themselves and one of them
— Ctrl+C — hardcoded the `media/` path for every type.

The failure that produced is the nastiest shape available, because the file is
right there on disk: the caller concludes it is missing, downloads it (into the
`.msv` path it is not looking at), re-checks the `.wzmedia` path, still finds
nothing, and tells the user

    "Não foi possível baixar este arquivo de mídia. O link pode ter expirado
     ou o WPPConnect está indisponível."

on a voice note that played perfectly a second earlier. Reported live after
v1.1.

CLAUDE.md already carries the rule this enforces: "is this media downloaded?"
spans two directories, and a second copy of that answer is how one part of the
app starts disagreeing with whatever wrote the file.
"""

import inspect
import re

import pytest

import app_paths
from ui.conversations import (
    ConversationsPanel,
    cached_media_path,
    local_media_cache_paths,
    media_cache_id,
)


@pytest.fixture(autouse=True)
def _account_scope():
    """cached_media_path() goes through data_path(), which refuses to answer
    without an active account — the same guard that keeps one account's media
    out of another's folder."""
    app_paths.set_active_account("acct-under-test")
    yield
    app_paths.set_active_account(None)


class TestTheIdTheFileIsNamedAfter:
    def test_a_plain_id_is_used_as_is(self):
        assert media_cache_id("3EB0CB1BB426875ACCFB49") == "3EB0CB1BB426875ACCFB49"

    def test_a_composite_id_reduces_to_its_last_component(self):
        """WhatsApp's `false_<jid>_<id>` form: the file on disk is named after
        the id alone."""
        assert media_cache_id(
            "false_120363151058129530@g.us_3EB0CB1BB426875ACCFB49"
        ) == "3EB0CB1BB426875ACCFB49"

    def test_a_two_part_id_falls_back_to_the_last_part(self):
        assert media_cache_id("false_3EB0ABC") == "3EB0ABC"

    def test_an_empty_id_is_left_alone(self):
        assert media_cache_id("") == ""


class TestAudioIsCachedSomewhereElse:
    """The whole bug in one assertion."""

    def test_audio_resolves_into_voice_messages(self):
        path = cached_media_path("audioMessage", "ABC123")
        assert path.endswith("ABC123.msv")
        assert "voice_messages" in path

    @pytest.mark.parametrize("msg_type", [
        "imageMessage", "videoMessage", "documentMessage", "stickerMessage",
    ])
    def test_everything_else_resolves_into_media(self, msg_type):
        path = cached_media_path(msg_type, "ABC123")
        assert path.endswith("ABC123.wzmedia")
        assert "voice_messages" not in path

    def test_the_composite_id_is_reduced_for_both(self):
        composite = "false_120363151058129530@g.us_ABC123"
        assert cached_media_path("audioMessage", composite).endswith("ABC123.msv")
        assert cached_media_path("imageMessage", composite).endswith("ABC123.wzmedia")

    def test_it_agrees_with_the_media_tabs_own_scan(self):
        """local_media_cache_paths() answers the same question for the
        downloaded/not-downloaded scan. The two must never drift: one says a
        file is there and the other goes looking somewhere else."""
        both = local_media_cache_paths("voice", "media", "ABC123")
        assert cached_media_path("audioMessage", "ABC123").endswith(
            both[0].split("voice")[-1].lstrip("\\/"))
        assert cached_media_path("imageMessage", "ABC123").endswith(
            both[1].split("media")[-1].lstrip("\\/"))


class TestEveryReaderGoesThroughIt:
    """A reader that resolves the path itself is the bug coming back. These are
    the three that read a cached file and download it when it is missing."""

    @pytest.mark.parametrize("method_name", [
        "_on_menu_copy_file",     # Ctrl+C — the one that was broken
        "_on_action_download",    # the Download button, same shape
        "_save_message_media",    # Save As, the one that was already right
    ])
    def test_the_reader_uses_the_shared_resolver(self, method_name):
        source = inspect.getsource(getattr(ConversationsPanel, method_name))
        assert "cached_media_path(" in source, (
            f"{method_name} must resolve its path through cached_media_path()"
        )

    @pytest.mark.parametrize("method_name", [
        "_on_menu_copy_file", "_on_action_download", "_save_message_media",
    ])
    def test_the_reader_never_builds_a_cache_path_itself(self, method_name):
        source = inspect.getsource(getattr(ConversationsPanel, method_name))
        inline = re.search(
            r'data_path\(\s*"(?:media|voice_messages)"\s*,\s*f"\{[^}]+\}\.'
            r'(?:wzmedia|msv)"', source)
        assert inline is None, (
            f"{method_name} builds its own cache path ({inline.group(0)!r}); "
            "that is exactly how Ctrl+C ended up looking in media/ for a file "
            "written to voice_messages/"
        )

    def test_copy_no_longer_hardcodes_the_media_directory(self):
        """The literal regression: `data_path("media", f"{msg_id}.wzmedia")`
        with the raw id, for every message type."""
        source = inspect.getsource(ConversationsPanel._on_menu_copy_file)
        assert 'data_path("media"' not in source
