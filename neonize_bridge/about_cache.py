"""Each person's about ("Sobre") as the bridge last read it from WhatsApp.

`profile-status/:phone` costs one usync query to WhatsApp's servers. The
client asks for it for every person it is trying to name (main_window/
identity.py's mention scan: two thousand at a launch), and WhatsApp Web
answers most of those from what it already knows; asking the server each
time drew a refusal every few seconds and risks the account. So an about is
kept for ABOUT_TTL, a refusal for FAILURE_TTL, and when the server says the
account is asking too much (429) nobody's about is asked for RATE_PAUSE.

Pure, with the clock passed in (tested in test_about_cache.py).
"""
import re
import time

ABOUT_TTL = 6 * 3600
FAILURE_TTL = 10 * 60
RATE_PAUSE = 60
MAX_ENTRIES = 5000

# whatsmeow: "failed to send usync query: info query returned status 429: rate-overlimit"
_CODE = re.compile(r"returned (?:status )?(\d{3})")


def failure_code(error) -> str:
    """The server's status code in a get_user_info error, or ''."""
    match = _CODE.search(str(error or ""))
    return match.group(1) if match else ""


class AboutCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._entries = {}          # jid -> (about or None, error text, until)
        self._paused_until = 0.0

    def lookup(self, jid):
        """(about, error) still valid for this person, or None to ask WhatsApp."""
        entry = self._entries.get(jid)
        if entry is None or entry[2] <= self._clock():
            return None
        return entry[0], entry[1]

    def paused(self) -> bool:
        return self._paused_until > self._clock()

    def remember(self, jid, about):
        self._store(jid, (about, "", self._clock() + ABOUT_TTL))

    def failed(self, jid, error):
        now = self._clock()
        if failure_code(error) == "429":
            self._paused_until = now + RATE_PAUSE
        self._store(jid, (None, str(error), now + FAILURE_TTL))

    def _store(self, jid, entry):
        if len(self._entries) >= MAX_ENTRIES and jid not in self._entries:
            now = self._clock()
            self._entries = {k: v for k, v in self._entries.items() if v[2] > now}
            if len(self._entries) >= MAX_ENTRIES:
                self._entries.pop(next(iter(self._entries)))
        self._entries[jid] = entry


def cache_of(session) -> AboutCache:
    """The session's AboutCache, created on first use."""
    cache = getattr(session, "about_cache", None)
    if cache is None:
        cache = session.about_cache = AboutCache()
    return cache
