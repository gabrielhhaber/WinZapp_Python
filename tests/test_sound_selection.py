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
