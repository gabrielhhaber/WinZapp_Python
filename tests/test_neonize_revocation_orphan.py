"""A delete notice with no stored original must not create a chat (issue #762)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "neonize_bridge"))

import store  # noqa: E402


def _notice(target="X1", mid="N1"):
    return {"id": {"id": mid}, "t": 100, "type": "revoked", "fromMe": False,
            "protocolMessageKey": {"id": target}}


def _msg(mid):
    return {"id": {"id": mid}, "t": 50, "type": "chat", "body": "hi", "fromMe": False}


def test_orphan_notice_for_unknown_chat_stores_nothing(tmp_path):
    db = store.Store(str(tmp_path / "b.db"))
    assert db.revocation("123@g.us", _notice()) is False
    assert not db.has_chat("123@g.us")


def test_notice_marks_the_stored_original(tmp_path):
    db = store.Store(str(tmp_path / "b.db"))
    db.chat("123@g.us")
    db.message("123@g.us", _msg("X1"))
    assert db.revocation("123@g.us", _notice()) is True


def test_orphan_notice_in_known_chat_is_kept(tmp_path):
    db = store.Store(str(tmp_path / "b.db"))
    db.chat("123@g.us")
    assert db.revocation("123@g.us", _notice()) is True
