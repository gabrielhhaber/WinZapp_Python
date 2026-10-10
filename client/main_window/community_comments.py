"""Explicit comment reads/sends: no normal-message queue and no automatic retry."""

from urllib.parse import quote

from core.api_client import api_get, api_post
from core.community_comments import CommunityCommentsError, comment_send_confirmed, parse_comments


class CommunityCommentsMixin:
    def _announcement_comment_id(self, jid, message):
        if not isinstance(jid, str) or not jid.endswith("@g.us"):
            raise CommunityCommentsError("not_announcement")
        key = message.get("key") or {}
        parent = self._serialize_msg_id(jid, key, message)
        parts = parent.split("_", 2) if isinstance(parent, str) else []
        if len(parts) != 3 or parts[1] != jid:
            raise CommunityCommentsError("unavailable")
        return parent

    def _community_comments_url(self, parent):
        return (f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
                f"/message-comments/{quote(parent, safe='')}")

    def get_announcement_comments(self, jid, message):
        parent = self._announcement_comment_id(jid, message)
        try:
            response = api_get(self._community_comments_url(parent),
                               headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
            payload = response.json()
            if response.status_code == 400 and payload.get("code") == "not_community_announcement":
                raise CommunityCommentsError("not_announcement")
            response.raise_for_status()
            return parse_comments(payload, parent)
        except CommunityCommentsError:
            raise
        except Exception:
            raise CommunityCommentsError("unavailable") from None

    def send_announcement_comment(self, jid, message, text):
        parent = self._announcement_comment_id(jid, message)
        if not isinstance(text, str) or not text.strip():
            raise CommunityCommentsError("invalid_text")
        try:
            response = api_post(self._community_comments_url(parent), json={"text": text},
                                headers={"Authorization": f"Bearer {self.token}"}, timeout=45)
            if comment_send_confirmed(response.status_code, response.json()):
                return True
        except Exception:
            pass
        # A timeout or a failed verdict may follow an actual delivery.
        raise CommunityCommentsError("unconfirmed")
