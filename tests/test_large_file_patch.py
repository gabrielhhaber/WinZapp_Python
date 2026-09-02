"""Regression checks for large-document delivery through WPPConnect."""

import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from core import wppconnect_sender_layer_patch as sender_patch


def test_sender_patch_migrates_vanilla_and_legacy_sources():
    for source in (
        sender_patch.ORIGINAL_SEND_FILE,
        sender_patch.LEGACY_PATCHED_SEND_FILE,
    ):
        patched = source
        for original, replacement in sender_patch.ALL_PATCHES:
            patched = patched.replace(original, replacement)

        assert patched == sender_patch.PATCHED_SEND_FILE

    loading = sender_patch.ORIGINAL_FILE_LOADING
    for original, replacement in sender_patch.ALL_PATCHES:
        loading = loading.replace(original, replacement)
    assert loading == sender_patch.PATCHED_FILE_LOADING


def test_sender_patch_migrates_intermediate_chunked_source():
    intermediate = sender_patch.PATCHED_SEND_FILE.replace(
        "        if (largeFilePath) {\n",
        "        if (base64.length > 8 * 1024 * 1024) {\n",
        1,
    )
    method = intermediate + "\n    }\n    /**"

    migrated = sender_patch.patch_sender_layer_source(method)

    assert "if (largeFilePath)" in migrated
    assert "if (base64.length >" not in migrated


def test_sender_patch_migrates_the_previous_chunked_patch_to_1gb():
    previous = sender_patch.PATCHED_SEND_FILE.replace(
        sender_patch._deepen(sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH),
        "",
    ).replace(
        sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH,
        "",
    )

    migrated = sender_patch.patch_sender_layer_source(previous)

    assert "WPP.whatsapp?.MediaGatingUtils" in migrated
    assert "1 * 1024 * 1024 * 1024" in migrated
    assert "__winzappUploadLimitPatched" in migrated


def test_sender_patch_migrates_the_leaking_unguarded_upload_limit_wrap():
    """LEGACY_PATCHED_SEND_FILE_V2 is the exact text every existing install
    already has on disk from before the __winzappUploadLimitPatched guard
    existed. Without this migration, re-running setup_api.py on an
    already-patched machine would leave the leak in place forever, since
    none of the other ALL_PATCHES pairs match already-patched text."""
    assert (
        sender_patch.LEGACY_PATCHED_SEND_FILE_V2,
        sender_patch.PATCHED_SEND_FILE,
    ) in sender_patch.ALL_PATCHES
    assert "__winzappUploadLimitPatched" not in sender_patch.LEGACY_PATCHED_SEND_FILE_V2

    migrated = sender_patch.LEGACY_PATCHED_SEND_FILE_V2
    for original, replacement in sender_patch.ALL_PATCHES:
        migrated = migrated.replace(original, replacement)

    assert migrated == sender_patch.PATCHED_SEND_FILE


def test_sender_patch_guards_upload_limit_wrap_against_repeated_wrapping():
    """The getUploadLimit() override must install at most once per page
    load — re-wrapping it on every document send chains one more closure
    onto a singleton (WPP.whatsapp.MediaGatingUtils) that lives for the
    whole session, leaking memory in the browser process forever."""
    patched = sender_patch.PATCHED_SEND_FILE

    assert patched.count("__winzappUploadLimitPatched = true") == 2
    assert patched.count("&& !mediaGating.__winzappUploadLimitPatched") == 2
    assert "&& !mediaGating.__winzappUploadLimitPatched" in sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH


def test_sender_patch_migrates_the_guarded_document_only_limit():
    """Existing installs already carry the guarded document-only wrapper.
    Updating must widen that exact source instead of treating it as current."""
    migrated = sender_patch.patch_sender_layer_source(
        sender_patch.LEGACY_PATCHED_SEND_FILE_V3
    )

    assert migrated == sender_patch.PATCHED_SEND_FILE
    assert sender_patch._BROWSER_DOCUMENT_LIMIT_PATCH not in migrated
    assert sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH in migrated


def test_browser_upload_limit_covers_every_supported_attachment_type():
    """The bounded transfer already accepts every media kind; its page-side
    size gate must not leave audio at WhatsApp Web's default 16 MB ceiling."""
    block = sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH

    for kind in ("document", "image", "video", "audio"):
        assert f"'{kind}'" in block
    assert "includes(options.type)" in block
    assert "includes(type)" in block
    # The GATE must cover every type. The old document-only gate is what this
    # guards against — not the words "type === 'document'", which now legitimately
    # appear inside the ceiling expression, where documents get 2 GB and the rest
    # 1 GB.
    assert "options.type === 'document' &&" not in sender_patch.PATCHED_SEND_FILE


def test_browser_media_prep_explicitly_marks_audio_mime_as_audio():
    """WA-JS exposes MediaPrep's isAudio option but sendFileMessage leaves it
    unset. Converted OGG/Opus must not depend on container auto-detection to
    reach the audio path."""
    block = sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH

    assert "WPP.whatsapp?.MediaPrep" in block
    assert "startsWith('audio/')" in block
    assert "{ ...prepOptions, isAudio: true }" in block
    assert "!mediaPrep.__winzappAudioTypePatched" in block
    assert "mediaPrep.__winzappAudioTypePatched = true" in block


def test_sender_patch_migrates_limit_only_block_to_explicit_audio_type():
    old = sender_patch.PATCHED_SEND_FILE.replace(
        sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH,
        sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH_V1,
    ).replace(
        sender_patch._deepen(sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH),
        sender_patch._deepen(sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH_V1),
    )

    assert sender_patch.patch_sender_layer_source(old) == sender_patch.PATCHED_SEND_FILE


def test_browser_patch_does_not_bypass_whatsapps_wav_validation():
    """A native WAV upload reaches HTTP 201 but WhatsApp rejects the message
    with ACK -1, so WAV compatibility belongs in the Python transcoder."""
    block = sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH

    assert "WAWebSendMessageToMediaWorker" not in block
    assert "__winzappWavPassthroughPatched" not in block


def test_sender_patch_removes_the_failed_native_wav_bypass():
    migrated = sender_patch.patch_sender_layer_source(
        sender_patch.LEGACY_PATCHED_SEND_FILE_V4
    )

    assert migrated == sender_patch.PATCHED_SEND_FILE
    assert "__winzappWavPassthroughPatched" not in migrated


def test_large_documents_use_bounded_browser_transfers_and_a_browser_limit():
    patched = sender_patch.PATCHED_SEND_FILE

    assert "createReadStream(largeFilePath" in patched
    assert "highWaterMark: 3 * 1024 * 1024" in patched
    assert "new File(chunks" in patched
    assert "__winzappFileTransfers.delete(id)" in patched
    assert "if (largeFilePath)" in patched
    assert "WPP.whatsapp?.MediaGatingUtils" in patched
    assert "1 * 1024 * 1024 * 1024" in patched


def test_document_limits_match_whatsapps_2gb_document_ceiling():
    """WhatsApp allows 2 GB for documents. WinZapp capped them at 1 GB for no
    reason other than sharing one constant with photos/videos/audio, which do
    stop at 1 GB."""
    conversations = (ROOT / "client" / "ui" / "conversations.py").read_text(
        encoding="utf-8"
    )
    main = (ROOT / "client" / "main.py").read_text(encoding="utf-8")

    assert "_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024 * 1024" in conversations
    assert "_MAX_DOCUMENT_MB    = 2048" in conversations
    assert "2 * 1024 * 1024 * 1024 if media_type == \"document\"" in main


def test_media_ceilings_stay_at_1gb():
    """Image/video/audio used to be capped at 70MB in the UI's own
    pre-check, well under what sender.layer.js's bounded transfer can now
    actually move (see wppconnect_sender_layer_patch.py) — that gap is what
    used to force a 500 for any media send past 70MB. 1 GB is WhatsApp's own
    limit for them, and raising documents to 2 GB must not drag these up too."""
    conversations = (ROOT / "client" / "ui" / "conversations.py").read_text(
        encoding="utf-8"
    )

    assert "_MAX_MEDIA_BYTES    = 1 * 1024 * 1024 * 1024" in conversations
    assert "_MAX_MEDIA_MB       = 1024" in conversations
    assert "70  * 1024 * 1024" not in conversations


def test_every_gate_a_large_document_passes_agrees_on_2gb():
    """A document between 1 and 2 GB crosses four independent ceilings. If any
    one of them is still 1 GB the send fails somewhere the user cannot see, so
    they are pinned together rather than one by one."""
    conversations = (ROOT / "client" / "ui" / "conversations.py").read_text(
        encoding="utf-8"
    )
    main = (ROOT / "client" / "main.py").read_text(encoding="utf-8")
    websocket_client = (ROOT / "client" / "core" / "websocket_client.py").read_text(
        encoding="utf-8"
    )

    # 1. the attachment panel's own pre-check
    assert "_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024 * 1024" in conversations
    # 2. send_media_attachment(), which the queue calls
    assert "2 * 1024 * 1024 * 1024 if media_type == \"document\"" in main
    # 3. the flat cap WPPConnect is told about (one value for every type, so it
    #    has to be the largest)
    assert '"type": "maxFileSize", "value": 2 * 1024 * 1024 * 1024' in websocket_client
    # 4. WhatsApp Web's own MediaGatingUtils ceiling, inside the page
    assert (
        "type === 'document' ? 2 * 1024 * 1024 * 1024 : 1 * 1024 * 1024 * 1024"
        in sender_patch.PATCHED_SEND_FILE
    )


def test_an_install_still_on_the_flat_1gb_ceiling_is_migrated():
    """npm install refetches pristine sources, so a reinstall takes the fresh
    path — but an install patched in place would otherwise sit at 1 GB forever.
    Both nesting depths the block appears at have to be migrated."""
    already_patched = sender_patch.PATCHED_SEND_FILE.replace(
        sender_patch._UPLOAD_LIMIT_CEILING_V3, sender_patch._UPLOAD_LIMIT_CEILING_V2
    )
    assert sender_patch._UPLOAD_LIMIT_CEILING_V2 in already_patched

    migrated = sender_patch.patch_sender_layer_source(already_patched)

    assert migrated == sender_patch.PATCHED_SEND_FILE
    assert sender_patch._UPLOAD_LIMIT_CEILING_V2 not in migrated
    assert migrated.count(sender_patch._UPLOAD_LIMIT_CEILING_V3) == 2


def test_migrating_the_ceiling_twice_changes_nothing():
    once = sender_patch.patch_sender_layer_source(sender_patch.PATCHED_SEND_FILE)
    assert once == sender_patch.PATCHED_SEND_FILE


def test_wpp_only_sets_the_effective_file_size_limit():
    websocket_client = (ROOT / "client" / "core" / "websocket_client.py").read_text(
        encoding="utf-8"
    )
    method = websocket_client.split("    def _set_wpp_limits(self):", 1)[1].split(
        "    def ", 1
    )[0]

    assert '"type": "maxFileSize"' in method
    assert '"type": "maxMediaSize"' not in method


def test_sender_patch_widens_bounded_transfer_to_every_attachment_type():
    """PATCHED_FILE_LOADING_V1 (document-only) must still migrate an
    already-patched sender.layer.js to the widened PATCHED_FILE_LOADING —
    otherwise a machine that already has the old patch applied never picks
    up the fix on an ordinary restart (see
    tests/test_reapply_node_modules_patches.py for why that reapply path
    matters)."""
    assert (sender_patch.PATCHED_FILE_LOADING_V1, sender_patch.PATCHED_FILE_LOADING) in sender_patch.ALL_PATCHES
    for kind in ("document", "image", "video", "audio"):
        assert f"'{kind}'" in sender_patch.PATCHED_FILE_LOADING
    assert "options.type === 'document'" not in sender_patch.PATCHED_FILE_LOADING


class TestTheUploadLimitOverrideInstallsItselfOnlyOnce:
    """The getUploadLimit override lives inside the per-send page callback, so
    it runs again on every document sent. Its first version guarded on
    `mediaGating?.getUploadLimit` — but a wrapper IS a getUploadLimit, so that
    guard is true forever after the first wrap. Each send then captured the
    previous wrapper and layered another one on top, with nothing ever
    resetting it: an unbounded closure chain for as long as the WhatsApp Web
    page lived, walked in full on every call.

    `__winzappUploadLimitPatched` on the object is what makes it once-per-page.
    Being source-text assertions, these can't run the JS — what they pin down
    is that the marker is both TESTED before wrapping and SET after, in every
    copy of the block. A version that only sets it, or only tests it, would
    read as fixed and behave exactly like the leak."""

    def _blocks(self, text):
        """Each copy of the override block, split off at its opening line."""
        parts = text.split("const mediaGating = WPP.whatsapp?.MediaGatingUtils;")
        return parts[1:]

    def test_every_copy_of_the_block_tests_and_sets_the_marker(self):
        blocks = self._blocks(sender_patch.PATCHED_SEND_FILE)
        assert len(blocks) == 2, (
            "expected the override in both the chunked and the base64 branch; "
            f"found {len(blocks)}"
        )
        for block in blocks:
            head = block.split("const result")[0]
            assert "!mediaGating.__winzappUploadLimitPatched" in head, (
                "the guard must consult the marker, not just getUploadLimit's "
                "existence — a wrapper satisfies that check forever"
            )
            assert "mediaGating.__winzappUploadLimitPatched = true;" in head, (
                "the marker must be set, or the guard can never become false"
            )

    def test_the_injected_block_carries_the_marker_too(self):
        """The block injected into a node_modules that predates the override
        entirely is a separate constant from the two inline copies."""
        head = sender_patch._BROWSER_ATTACHMENT_LIMIT_PATCH
        assert "!mediaGating.__winzappUploadLimitPatched" in head
        assert "mediaGating.__winzappUploadLimitPatched = true;" in head

    def test_the_unmarked_variant_is_migrated_not_left_alone(self):
        """A machine patched by the build that shipped the leaking version has
        the unmarked block in node_modules. It matches no ALL_PATCHES pair, so
        without an explicit migration every later setup run would leave it
        exactly as it is — the same trap the intermediate chunked variant fell
        into."""
        legacy = sender_patch._LEGACY_DOCUMENT_LIMIT_PATCH
        stale = (
            "        let sendResult;\n"
            + legacy
            + "                    const result = await WPP.chat.sendFileMessage(to, base64, {\n"
        )

        migrated = sender_patch.patch_sender_layer_source(stale)

        assert "__winzappUploadLimitPatched" in migrated
        assert legacy not in migrated

    def test_the_unmarked_variant_is_migrated_at_the_deeper_indentation_too(self):
        """The chunked branch carries the same block one nesting level in."""
        legacy_deep = sender_patch._deepen(sender_patch._LEGACY_DOCUMENT_LIMIT_PATCH)
        stale = (
            "        let sendResult;\n"
            + legacy_deep
            + "                        const result = await WPP.chat.sendFileMessage(to, file, {\n"
        )

        migrated = sender_patch.patch_sender_layer_source(stale)

        assert "__winzappUploadLimitPatched" in migrated
        assert legacy_deep not in migrated

    def test_migrating_an_already_marked_source_changes_nothing(self):
        current = sender_patch.PATCHED_SEND_FILE + "\n    }\n    /**"

        once = sender_patch.patch_sender_layer_source(current)
        twice = sender_patch.patch_sender_layer_source(once)

        assert once == twice
        assert once.count("__winzappUploadLimitPatched = true;") == 2, (
            "re-running the migration must not duplicate the marker assignment"
        )
