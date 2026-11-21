"""core/sound_selection.py: "only the original WhatsApp sounds" (issue #769),
in the installer and in every update after it."""
import os
import re
from pathlib import Path

from core import sound_selection as sel

ROOT = Path(__file__).resolve().parents[1]


def test_whatsapp_keeps_its_own_sounds_the_alerts_and_every_non_sound():
    for name in ("call_incoming.ogg", "MESSAGE_CURRENT.OGG", "alerts/Alert-01.ogg",
                 r"alerts\Alert-10.ogg", "default.pack.json", "typing.SOURCE.md"):
        assert sel.is_whatsapp_original(name), name
    for name in ("message_sent.ogg", "startup.ogg", "typing.wav", "other/Alert-01.ogg"):
        assert not sel.is_whatsapp_original(name), name


def test_every_named_sound_is_in_the_pack():
    pack = ROOT / "client" / "sounds" / "default"
    for name in sel.WHATSAPP_SOUNDS:
        assert (pack / name).is_file(), name
    assert any((pack / "alerts").glob("*.ogg"))


def test_the_installer_keeps_the_same_sounds():
    src = (ROOT / "installer" / "installer.c").read_text(encoding="utf-8")
    body = re.search(r"WHATSAPP_SOUNDS\[\] = \{(.*?)\};", src, re.S).group(1)
    assert tuple(re.findall(r'L"([^"]+)"', body)) == sel.WHATSAPP_SOUNDS
    exts = re.search(r"SOUND_EXTENSIONS\[\] = \{(.*?)\};", src, re.S).group(1)
    assert set(re.findall(r'L"([^"]+)"', exts)) == set(sel._AUDIO)
    assert f'INSTALLED_SOUNDS_MARKER L"{sel.INSTALLED_SOUNDS_MARKER}"' in src
    assert '_wcsnicmp(relative, L"alerts", 6)' in src and sel.WHATSAPP_SOUND_FOLDERS == ("alerts",)
    assert 'WriteFile(hf, "whatsapp", 8' in src


def test_the_marker_says_which_sounds_were_installed(tmp_path):
    assert not sel.keeps_only_whatsapp(str(tmp_path))
    (tmp_path / sel.INSTALLED_SOUNDS_MARKER).write_text("whatsapp", encoding="utf-8")
    assert sel.keeps_only_whatsapp(str(tmp_path))
    (tmp_path / sel.INSTALLED_SOUNDS_MARKER).write_text("full", encoding="utf-8")
    assert not sel.keeps_only_whatsapp(str(tmp_path))


def test_an_update_leaves_out_the_sounds_the_install_left_out(tmp_path):
    default = tmp_path / "sounds" / "default"
    (default / "alerts").mkdir(parents=True)
    for name in ("call_ended.ogg", "startup.ogg", "default.pack.json", "alerts/Alert-02.ogg"):
        (default / name).write_bytes(b"x")
    assert sel.prune_to_whatsapp(str(tmp_path / "sounds")) == 1
    left = sorted(str(p.relative_to(default)).replace(os.sep, "/") for p in default.rglob("*") if p.is_file())
    assert left == ["alerts/Alert-02.ogg", "call_ended.ogg", "default.pack.json"]


def _a_new_pack(source_dir):
    default = source_dir / "sounds" / "default"
    default.mkdir(parents=True)
    for name in ("call_ended.ogg", "startup.ogg"):
        (default / name).write_bytes(b"x")
    return default


def test_the_updater_prunes_the_new_pack_for_a_whatsapp_only_install(tmp_path):
    import updater
    source, install = tmp_path / "new", tmp_path / "installed"
    default = _a_new_pack(source)
    (install / "sounds").mkdir(parents=True)
    (install / sel.INSTALLED_SOUNDS_MARKER).write_text("whatsapp", encoding="utf-8")
    kept_installed = install / "sounds" / "startup.ogg"
    kept_installed.write_bytes(b"x")
    assert updater._leave_out_unselected_sounds(str(source), str(install)) == 1
    assert sorted(p.name for p in default.iterdir()) == ["call_ended.ogg"]
    assert kept_installed.exists()


def test_the_updater_deletes_nothing_without_the_marker(tmp_path):
    import updater
    source, install = tmp_path / "new", tmp_path / "installed"
    default = _a_new_pack(source)
    install.mkdir()
    assert updater._leave_out_unselected_sounds(str(source), str(install)) == 0
    assert len(list(default.iterdir())) == 2


# ── Changing the selection from the update dialog (issue #842) ───────────────

def _an_install(tmp_path, marker=None):
    install = tmp_path / "installed"
    default = install / "sounds" / "default"
    (default / "alerts").mkdir(parents=True)
    for name in ("call_ended.ogg", "startup.ogg", "default.pack.json", "alerts/Alert-02.ogg"):
        (default / name).write_bytes(b"x")
    (install / "sounds" / "custom.ogg").write_bytes(b"x")
    if marker:
        (install / sel.INSTALLED_SOUNDS_MARKER).write_text(marker, encoding="utf-8")
    return install


def test_the_current_mode_comes_from_the_marker(tmp_path):
    assert sel.current_mode(str(tmp_path)) == sel.MODE_ALL
    (tmp_path / sel.INSTALLED_SOUNDS_MARKER).write_text("whatsapp", encoding="utf-8")
    assert sel.current_mode(str(tmp_path)) == sel.MODE_WHATSAPP


def test_choosing_whatsapp_prunes_the_release_marks_it_and_lists_the_installed_extras(tmp_path):
    install = _an_install(tmp_path)
    source = tmp_path / "new"
    default = _a_new_pack(source)
    extras = sel.stage_selection(str(source), str(install), sel.MODE_WHATSAPP)
    assert [os.path.basename(p) for p in extras] == ["startup.ogg"]
    assert sorted(p.name for p in default.iterdir()) == ["call_ended.ogg"]
    assert (source / sel.INSTALLED_SOUNDS_MARKER).read_text(encoding="utf-8") == "whatsapp"
    # Nothing installed is touched here; the update's script deletes after the copy.
    assert (install / "sounds" / "default" / "startup.ogg").exists()
    assert (install / "sounds" / "custom.ogg").exists()


def test_choosing_all_again_keeps_the_whole_release_and_replaces_the_marker(tmp_path):
    install = _an_install(tmp_path, marker="whatsapp")
    source = tmp_path / "new"
    default = _a_new_pack(source)
    assert sel.stage_selection(str(source), str(install), sel.MODE_ALL) == []
    assert len(list(default.iterdir())) == 2
    assert not sel.keeps_only_whatsapp(str(source))
    assert (source / sel.INSTALLED_SOUNDS_MARKER).read_text(encoding="utf-8") == "full"


def test_keeping_the_current_selection_changes_nothing_else(tmp_path):
    source = tmp_path / "new"
    default = _a_new_pack(source)
    full = _an_install(tmp_path / "a")
    assert sel.stage_selection(str(source), str(full), sel.MODE_ALL) == []
    assert not (source / sel.INSTALLED_SOUNDS_MARKER).exists() and len(list(default.iterdir())) == 2
    only = _an_install(tmp_path / "b", marker="whatsapp")
    assert sel.stage_selection(str(source), str(only), sel.MODE_WHATSAPP) == []
    assert sorted(p.name for p in default.iterdir()) == ["call_ended.ogg"]
    assert not (source / sel.INSTALLED_SOUNDS_MARKER).exists()


def test_the_pending_choice_wins_over_the_installed_one_and_can_be_cleared(tmp_path):
    install = _an_install(tmp_path, marker="whatsapp")
    source = tmp_path / "new"
    _a_new_pack(source)
    try:
        sel.set_pending_mode(sel.MODE_ALL)
        assert sel.pending_mode() == sel.MODE_ALL
        sel.stage_selection(str(source), str(install))
        assert (source / sel.INSTALLED_SOUNDS_MARKER).read_text(encoding="utf-8") == "full"
        sel.set_pending_mode("nonsense")
        assert sel.pending_mode() is None
    finally:
        sel.set_pending_mode(None)


def test_the_update_script_deletes_the_dropped_sounds_only_after_the_copy():
    import updater
    args = dict(source_dir="C:\\src", install_dir="C:\\app", exe_path="C:\\app\\WinZapp.exe",
                log_path="C:\\log.txt", marker_path="C:\\app\\m", pid=1, api_port=6300)
    script = updater._build_installer_script(**args, delete_files=["C:\\app\\sounds\\default\\startup.ogg"])
    line = 'del /F /Q "C:\\app\\sounds\\default\\startup.ogg"'
    assert line in script and script.index("xcopy /E") < script.index(line)
    assert "del /F /Q" not in updater._build_installer_script(**args)
