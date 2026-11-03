"""Changing the language and pressing Apply in Settings must re-translate every
control the dialog labelled when it was built.

SettingsDialog does not rebuild itself on Apply: _refresh_dialog_labels() calls
SetLabel()/SetItemLabel()/... on an explicit list of controls. A control left
out of that list keeps the previous language until the dialog is closed and
opened again — and a control created inline, without a `self.` attribute, can
never be put on that list at all. Reported live: after switching from English
to Portuguese, "When opening a chat, show up to this many messages" and
"Messages to skip when pressing Page Up / Page Down" stayed in English while
the group right below them changed. An audit then found nine such controls,
plus two list column headers.

Compared per control, not per key: two controls may share one string (both
"custom path" labels do), and a key re-applied to one of them must not count
for the other.

This reads the dialog's source, so it runs everywhere without opening a
window.
"""

import ast
import inspect
from pathlib import Path

from core.sound_system import AlertPreviewController
from tests.god_modules import main_window_source

SETTINGS_DIALOG = (
    Path(__file__).resolve().parent.parent / "client" / "ui" / "dialogs" / "settings_dialog.py"
)

TRANSCRIPTION_TAB = SETTINGS_DIALOG.with_name("transcription_tab.py")
TRANSCRIPTION_EXTERNAL = SETTINGS_DIALOG.with_name("transcription_external.py")
TRANSCRIPTION_WHISPER_CPP = SETTINGS_DIALOG.with_name("transcription_whisper_cpp.py")
TRANSCRIPTION_PRECISION = SETTINGS_DIALOG.with_name("transcription_precision.py")
TRANSCRIPTION_CACHE = SETTINGS_DIALOG.with_name("transcription_cache.py")
TRANSCRIPTION_OPTIONS = SETTINGS_DIALOG.with_name("transcription_options.py")
TRANSCRIPTION_REUSE = SETTINGS_DIALOG.with_name("transcription_reuse.py")
ADVANCED_TAB = SETTINGS_DIALOG.with_name("advanced_tab.py")
VOICE_RECORDING_OPTIONS = SETTINGS_DIALOG.with_name("voice_recording_options.py")

LABELLED_CONTROLS = {"StaticText", "CheckBox", "RadioButton", "StaticBox", "Button", "RadioBox"}


def _self_attr(node):
    """'_foo' for `self._foo`, else None."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _i18n_keys(node):
    """Every literal key of an i18n.t("...") call below node."""
    return [
        sub.args[0].value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "t"
        and sub.args
        and isinstance(sub.args[0], ast.Constant)
        and isinstance(sub.args[0].value, str)
    ]


def _dialog_class():
    """SettingsDialog with the methods of the mixins that hold a tab merged in:
    the Local Transcription tab lives in transcription_tab.py (and its models-in-other-folders
    section in transcription_external.py, its whisper.cpp section in
    transcription_whisper_cpp.py, its precision picker in
    transcription_precision.py) but is built, relabelled and applied as part
    of the dialog."""
    tree = ast.parse(SETTINGS_DIALOG.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SettingsDialog")
    tab = ast.parse(TRANSCRIPTION_TAB.read_text(encoding="utf-8"))
    mixin = next(
        n for n in tab.body if isinstance(n, ast.ClassDef) and n.name == "TranscriptionTabMixin"
    )
    external = ast.parse(TRANSCRIPTION_EXTERNAL.read_text(encoding="utf-8"))
    external_mixin = next(
        n for n in external.body
        if isinstance(n, ast.ClassDef) and n.name == "ExternalModelsMixin"
    )
    whisper_cpp = ast.parse(TRANSCRIPTION_WHISPER_CPP.read_text(encoding="utf-8"))
    whisper_cpp_mixin = next(
        n for n in whisper_cpp.body
        if isinstance(n, ast.ClassDef) and n.name == "WhisperCppMixin"
    )
    precision = ast.parse(TRANSCRIPTION_PRECISION.read_text(encoding="utf-8"))
    precision_mixin = next(
        n for n in precision.body
        if isinstance(n, ast.ClassDef) and n.name == "TranscriptionPrecisionMixin"
    )
    cache = ast.parse(TRANSCRIPTION_CACHE.read_text(encoding="utf-8"))
    cache_mixin = next(
        n for n in cache.body
        if isinstance(n, ast.ClassDef) and n.name == "TranscriptionCacheMixin"
    )
    options = ast.parse(TRANSCRIPTION_OPTIONS.read_text(encoding="utf-8"))
    options_mixin = next(
        n for n in options.body
        if isinstance(n, ast.ClassDef) and n.name == "TranscriptionOptionsMixin"
    )
    reuse = ast.parse(TRANSCRIPTION_REUSE.read_text(encoding="utf-8"))
    reuse_mixin = next(
        n for n in reuse.body
        if isinstance(n, ast.ClassDef) and n.name == "TranscriptionReuseMixin"
    )
    advanced_mixin = next(
        n for n in ast.parse(ADVANCED_TAB.read_text(encoding="utf-8")).body
        if isinstance(n, ast.ClassDef) and n.name == "AdvancedTabMixin"
    )
    voice_options = next(
        n for n in ast.parse(VOICE_RECORDING_OPTIONS.read_text(encoding="utf-8")).body
        if isinstance(n, ast.ClassDef) and n.name == "VoiceRecordingOptionsMixin"
    )
    cls.body = (cls.body + mixin.body + external_mixin.body + whisper_cpp_mixin.body
                + precision_mixin.body + cache_mixin.body + options_mixin.body
                + reuse_mixin.body + advanced_mixin.body + voice_options.body)
    return cls


def _built_and_refreshed():
    """built: [(line, what, target attr or None, key)] for every label the
    dialog puts on a control when building it.
    refreshed: {(target attr, key)} re-applied in _refresh_dialog_labels()."""
    cls = _dialog_class()
    refresh = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_refresh_dialog_labels"
    )

    # The attribute each constructor call is assigned to, keyed by the Call
    # node itself — not by line, where an inline control nested in another
    # call on an assignment's line would inherit that assignment's target.
    targets = {}
    for func in cls.body:
        if not isinstance(func, ast.FunctionDef) or func is refresh:
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and _self_attr(node.targets[0])
                and isinstance(node.value, ast.Call)
            ):
                targets[id(node.value)] = _self_attr(node.targets[0])

    built = []
    preview_buttons = {}  # controller attr -> button attr
    for func in cls.body:
        if not isinstance(func, ast.FunctionDef) or func is refresh:
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "AlertPreviewController"
                and len(node.value.args) >= 2
            ):
                preview_buttons[_self_attr(node.targets[0])] = _self_attr(node.value.args[1])

            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr in LABELLED_CONTROLS:
                label = next((k.value for k in node.keywords if k.arg == "label"), None)
                if label is None:
                    continue
                target = targets.get(id(node))
                built.extend(
                    (node.lineno, f"wx.{node.func.attr}", target, key) for key in _i18n_keys(label)
                )
            elif node.func.attr == "InsertColumn" and len(node.args) >= 2:
                target = _self_attr(node.func.value)
                built.extend(
                    (node.lineno, "InsertColumn", target, key) for key in _i18n_keys(node.args[1])
                )

    refreshed = set()
    play_stop = {
        p.default
        for name, p in inspect.signature(AlertPreviewController.__init__).parameters.items()
        if name in ("play_label_key", "stop_label_key")
    }
    # _refresh_dialog_labels() may hand a whole tab to a `self._refresh_*()`
    # helper of its own (the Local Transcription tab's _refresh_transcription_labels()
    # rebuilds comboboxes as well as relabelling); what such a helper re-applies
    # is re-applied on Apply just the same, so it is read as part of it.
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    # Followed to any depth: _refresh_transcription_labels() hands the section
    # for models in other folders on to _refresh_external_labels().
    scopes = [refresh]
    for scope in scopes:
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Call)
                and _self_attr(node.func)
                and node.func.attr.startswith(("_refresh_", "_relabel_"))
                and node.func.attr in methods
                and methods[node.func.attr] not in scopes
            ):
                scopes.append(methods[node.func.attr])
    for node in (n for scope in scopes for n in ast.walk(scope)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        # self._x.SetLabel(i18n.t(k)) / self._x.SetItemLabel(i, i18n.t(k)) / ...
        target = _self_attr(node.func.value)
        # self._helper(self._x, i18n.t(k))
        if target is None and node.args:
            target = _self_attr(node.args[0])
        if node.func.attr == "refresh_label" and target in preview_buttons:
            refreshed |= {(preview_buttons[target], key) for key in play_stop}
            continue
        if target is None:
            continue
        refreshed |= {(target, key) for key in _i18n_keys(node)}
    return built, refreshed


def test_the_parse_finds_the_dialog_labels():
    """Guards the guard: a rewrite of how labels are built must fail here,
    not make the real test pass on an empty list."""
    built, refreshed = _built_and_refreshed()
    assert len(built) >= 80
    assert len(refreshed) >= 80
    assert any(what == "InsertColumn" for _, what, _, _ in built)


def test_every_built_label_is_reapplied_to_its_own_control():
    built, refreshed = _built_and_refreshed()
    missing = sorted(
        f"line {line}: {what} on self.{target or '<inline, no attribute>'} "
        f"with i18n.t({key!r})"
        for line, what, target, key in built
        if (target, key) not in refreshed
    )
    assert missing == [], (
        "these keep the previous language after Apply — keep each control on a "
        "self._ attribute and re-apply its label to that same control in "
        "_refresh_dialog_labels():\n  " + "\n  ".join(missing)
    )


def test_main_language_change_repaints_dynamic_content_and_calls():
    main_source = main_window_source()
    start = main_source.index("    def apply_language_changes(self):")
    end = main_source.index("\n    def on_alt_1", start)
    body = main_source[start:end]

    assert "self._refresh_call_language_surfaces()" in body
    assert "cp.populate_messages(preserve_focus=True)" in body
    assert "self._chats_ui_fp = None" in body
    assert "self.add_chats_to_ui()" in body
    assert "self._refresh_status_language()" in body
    assert "self._build_menubar()" in body


def test_incoming_call_popup_has_live_language_refresh_hook():
    source = (
        Path(__file__).resolve().parent.parent
        / "client" / "ui" / "dialogs" / "incoming_call.py"
    ).read_text(encoding="utf-8")
    assert "def refresh_labels(self, message: str | None = None):" in source
    assert "self._apply_labels()" in source
    assert (
        "(self._answer_button, answer_key, self._on_answer)" in source
        and '"incoming_call_close_button", self._on_close' in source
    )
