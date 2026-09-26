"""The Locked chats navigation item names its Alt+7 shortcut, like Alt+4/5/6."""
import json
from pathlib import Path

LANGUAGES = Path(__file__).resolve().parent.parent / "client" / "languages"


def test_every_locale_shows_alt7_on_the_locked_chats_nav_item():
    files = [p for p in LANGUAGES.glob("*.json") if p.name != "language_map.json"]
    assert files
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["locked_chats_nav"].endswith(" alt+7"), path.name
