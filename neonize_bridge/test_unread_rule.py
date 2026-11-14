"""unread_rule.py: the bridge counts unread as WhatsApp does."""
import pytest

import unread_rule


@pytest.mark.parametrize(("w", "expected"), [
    ({"type": "chat", "fromMe": False}, True),
    ({"type": "ptt", "fromMe": False}, True),
    ({"type": "ciphertext", "fromMe": False}, True),
    ({"type": "revoked", "fromMe": False}, False),      # deleted for everyone
    ({"type": "gp2", "fromMe": False}, False),
    ({"type": "chat", "fromMe": True}, False),
    (None, False),
])
def test_what_counts(w, expected):
    assert unread_rule.counted(w) is expected
