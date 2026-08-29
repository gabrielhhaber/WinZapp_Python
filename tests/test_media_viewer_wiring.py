"""Source-level regression tests for MediaViewer integration.

These tests intentionally avoid importing wxPython so they also run in the
Linux packaging/review environment where wx is not installed.
"""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _method_calls(rel, class_name, method_name):
    tree = ast.parse(_source(rel))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    return {
                        n.func.attr
                        for n in ast.walk(child)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    }
    raise AssertionError(f"{class_name}.{method_name} not found")


def test_conversation_activation_routes_images_and_videos_to_media_viewer():
    src = _source("client/ui/conversations.py")
    assert "from ui.media_viewer import MediaViewerDialog" in src
    calls = _method_calls("client/ui/conversations.py", "ConversationsPanel", "activate_message")
    assert "open_media_viewer_for_message" in calls


def test_status_plain_selection_never_opens_the_dialog_or_marks_viewed_directly():
    """Plain (arrow-key) selection must never open the dialog viewer or mark
    a status viewed itself, in either mode — Settings > Interface do
    usuário > "Mostrar os status em player separado" toggles between
    _open_status_media_viewer() (dialog, default) and _show_current_status()
    (classic inline) for *explicit* activation only; this handler only ever
    calls _use_status_media_viewer_dialog() to decide which of the two
    other handlers should react, plus (in classic mode) _show_current_
    status() itself — which does its own marking internally, not something
    this handler does directly. See test_only_viewer_open_callback_marks_
    status_viewed() below for that."""
    calls = _method_calls("client/status_panel.py", "StatusPanel", "_on_status_contact_selected")
    assert "_open_status_media_viewer" not in calls
    assert "_mark_status_viewed" not in calls
    assert "_use_status_media_viewer_dialog" in calls


def test_status_activation_opens_media_viewer():
    calls = _method_calls("client/status_panel.py", "StatusPanel", "_on_status_contact_activated")
    assert "_open_status_media_viewer" in calls
    assert "_use_status_media_viewer_dialog" in calls


def test_only_viewer_open_callback_marks_status_viewed_in_dialog_mode():
    """In dialog mode (the default), a status is marked viewed only by
    MediaViewer's on_item_opened callback (_on_viewer_status_opened), after
    the user explicitly activates it.

    _show_current_status() (the classic/inline viewer) legitimately marks
    a status viewed too — but it is only ever reachable when Settings >
    Interface do usuário > "Mostrar os status em player separado" is
    unchecked, restoring the pre-dialog behaviour where arrowing to a
    contact immediately showed (and viewed) their status. Both are
    correct for their respective mode; this test only pins the
    dialog-mode invariant.
    """
    calls = _method_calls("client/status_panel.py", "StatusPanel", "_on_viewer_status_opened")
    assert "_mark_status_viewed" in calls
    legacy = _method_calls("client/status_panel.py", "StatusPanel", "_show_current_status")
    assert "_mark_status_viewed" in legacy


def test_video_player_has_real_seek_volume_and_speeded_frame_clock():
    src = _source("client/core/video_player.py")
    assert "def set_volume(" in src
    assert "def bytes_to_seconds(" in src
    assert "self._restart_video_pipe(seconds)" in src
    assert '"-ss", f"{start_seconds:.3f}"' in src
    assert '"-re"' not in src
    assert "_FRAME_FPS * max(0.25, self._speed)" in src



def test_viewer_is_maximized_and_text_mode_is_read_only():
    src = _source("client/ui/media_viewer.py")
    assert "self.Maximize(True)" in src
    assert "wx.TE_MULTILINE | wx.TE_READONLY" in src
    assert "self._close_btn" in src
    assert "self._volume_slider" in src
    assert "self._position_slider" in src


def test_prev_next_save_have_real_shortcuts_and_are_announced():
    """Reported live: prev/next/save were reachable by mouse or Tab only —
    no keyboard shortcut worked, and NVDA announced no shortcut for any of
    the three buttons, unlike the classic StatusPanel viewer they mirror
    (AccessibleStatusPrev/Next/SaveAs, Ctrl+Left/Right, Ctrl+Shift+S)."""
    src = _source("client/ui/media_viewer.py")
    assert "from ui.accessible import AccessibleStatusPrev, AccessibleStatusNext, AccessibleSaveAs" in src

    calls = _method_calls("client/ui/media_viewer.py", "MediaViewerDialog", "_build_ui")
    assert "SetAccessible" in calls

    accel_calls = _method_calls("client/ui/media_viewer.py", "MediaViewerDialog", "_create_accelerators")
    assert "SetAcceleratorTable" in accel_calls
    assert "Bind" in accel_calls

    accel_src_start = src.index("def _create_accelerators")
    accel_src = src[accel_src_start:src.index("\n    def ", accel_src_start + 1)]
    assert "wx.WXK_LEFT" in accel_src and "self._on_prev" in accel_src
    assert "wx.WXK_RIGHT" in accel_src and "self._on_next" in accel_src
    assert 'ord("S")' in accel_src and "self._on_save" in accel_src
    assert "wx.ACCEL_CTRL | wx.ACCEL_SHIFT" in accel_src


def test_seek_back_forward_buttons_flank_pause_with_alt_v_and_alt_a():
    """Bug report: the new (dialog-based) status/video player has no way to
    skip forward/back — only Space (via _on_char_hook) to toggle play/pause.
    Voltar (Alt+V) must sit right before Pausar and Avançar (Alt+A) right
    after it, both in the same actions row/tab order, with the same fixed
    (locale-independent) shortcut mechanism the rest of the app's
    keyboard-shortcut buttons use — a custom Accessible reporting the literal
    combo, not a translated mnemonic."""
    src = _source("client/ui/media_viewer.py")
    assert "AccessibleMediaViewerSeekBack" in src
    assert "AccessibleMediaViewerSeekForward" in src

    build_src_start = src.index("def _build_ui")
    build_src = src[build_src_start:src.index("\n    def ", build_src_start + 1)]
    back_pos = build_src.index("self._seek_back_btn = wx.Button")
    play_pos = build_src.index("self._play_btn = wx.Button")
    fwd_pos = build_src.index("self._seek_forward_btn = wx.Button")
    assert back_pos < play_pos < fwd_pos

    accel_src_start = src.index("def _create_accelerators")
    accel_src = src[accel_src_start:src.index("\n    def ", accel_src_start + 1)]
    assert "wx.ACCEL_ALT" in accel_src
    assert 'ord("V")' in accel_src and "self._on_seek_back" in accel_src
    assert 'ord("A")' in accel_src and "self._on_seek_forward" in accel_src

    calls = _method_calls("client/ui/media_viewer.py", "MediaViewerDialog", "_seek_relative")
    assert "get_length" in calls
    assert "get_position" in calls
    assert "set_position" in calls


def test_reply_field_has_a_visible_label_not_just_setname():
    """Reported live: the status reply field announced nothing on focus.
    SetName() alone is not reliably read by NVDA/JAWS for an editable
    (non-read-only) wx.TextCtrl on Windows — the same reason StatusPanel's
    own classic reply field pairs a StaticText with the TextCtrl instead of
    relying on SetName() alone."""
    src = _source("client/ui/media_viewer.py")
    assert 'self._reply_label = wx.StaticText(' in src
    assert 'self._reply_field.SetName(self.i18n.t("status_reply_label"))' in src
    # The label must actually be shown/hidden alongside the field, not left
    # permanently visible while the field it names is hidden (or vice versa).
    calls = _method_calls("client/ui/media_viewer.py", "MediaViewerDialog", "_configure_status_actions")
    assert "Show" in calls


def test_i18n_audit_covers_save_filters():
    conversations = _source("client/ui/conversations.py")
    for key in (
        "file_filter_audio",
        "file_filter_images",
        "file_filter_videos",
        "file_filter_documents",
    ):
        assert key in conversations
def test_media_viewer_translations_exist_in_every_locale():
    required = {
        "media_viewer_title",
        "media_viewer_loading",
        "media_viewer_play",
        "media_viewer_pause",
        "media_viewer_seek_back",
        "media_viewer_seek_forward",
        "media_viewer_position",
        "media_viewer_volume",
        "media_viewer_speed",
        "media_viewer_caption",
        "media_viewer_error",
        "media_viewer_text_status",
        "language_select_title",
        "language_select_prompt",
        "file_filter_audio",
        "file_filter_images",
        "file_filter_videos",
        "file_filter_documents",
        "media_audio_convert_failed",
        "media_video_convert_failed",
        "startup_critical_title",
        "startup_critical_message",
    }
    for locale in ("en-US", "pt-BR", "es-ES", "pt-PT", "pl"):
        data = json.loads(_source(f"client/languages/{locale}.json"))
        missing = required.difference(data)
        assert not missing, f"{locale}: missing {sorted(missing)}"
        assert all(str(data[key]).strip() for key in required)


def test_media_viewer_has_accessible_media_bitmap_panel():
    mv_src = _source("client/ui/media_viewer.py")
    assert "AccessibleMediaBitmapPanel" in mv_src
    assert "_get_current_media_label" in mv_src
    assert "_update_media_labels" in mv_src
    acc_src = _source("client/ui/accessible.py")
    assert "class AccessibleMediaBitmapPanel" in acc_src
