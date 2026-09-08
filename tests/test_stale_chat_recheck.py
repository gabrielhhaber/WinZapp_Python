"""The staleness net behind issue #181.

Reported by a contributor on 1.1.0.2576alpha: after a normal synchronization,
two chats stayed outdated and only F5 fixed them. The plan was

    full=196 incremental=11 unchanged=88

and neither affected chat was fetched. Both already held 200 local messages
and were classified `unchanged`; F5 replanned as `full=295 incremental=0
unchanged=0 reasons={'forced-full': 295}` and the chats gained 50 and 51
messages, 34 and 7 of them *newer* than anything stored, moving their ends
from 08:35 and 07:34 to 17:12 and 17:11.

Two things that report rules out, and they matter for where the fix goes:

  * it is not the history-repair queue's job — that asks the phone for
    messages *older* than what is held, and these were newer;
  * the messages were genuinely absent from the database, not just from the
    UI, so this is a planning fault rather than a rendering one.

Every signal classify_chat_sync() reads — `t`, `unreadCount`,
`lastReceivedKey`, `lastMessage` — is chat-list *metadata*. When it goes stale
for one chat, every signal honestly agrees nothing changed, and nothing in the
plan can break out of that. So staleness is bounded by time instead.
"""

from core.incremental_sync import select_stale_rechecks
from main import MainWindow

HOUR = 3600
NOW = 1_788_900_000


class TestChoosingWhatToReCheck:
    def test_a_chat_checked_recently_is_left_alone(self):
        assert select_stale_rechecks(
            ["a@g.us"], {"a@g.us": NOW - 60}, NOW, 5, HOUR) == []

    def test_a_chat_past_the_interval_is_due(self):
        assert select_stale_rechecks(
            ["a@g.us"], {"a@g.us": NOW - HOUR}, NOW, 5, HOUR) == ["a@g.us"]

    def test_a_chat_never_checked_sorts_first(self):
        """Nothing is known about it, which is the worst case, not the best."""
        out = select_stale_rechecks(
            ["seen@g.us", "never@g.us"],
            {"seen@g.us": NOW - 2 * HOUR}, NOW, 1, HOUR)
        assert out == ["never@g.us"]

    def test_the_oldest_go_first(self):
        out = select_stale_rechecks(
            ["new@g.us", "old@g.us", "mid@g.us"],
            {"new@g.us": NOW - HOUR, "mid@g.us": NOW - 5 * HOUR,
             "old@g.us": NOW - 9 * HOUR},
            NOW, 2, HOUR)
        assert out == ["old@g.us", "mid@g.us"]

    def test_the_cost_per_round_is_fixed_however_large_the_account(self):
        # The property a full sweep does not have, and the reason this can run
        # on every round of a 295-chat account.
        many = [f"{i}@g.us" for i in range(1000)]
        assert len(select_stale_rechecks(many, {}, NOW, 5, HOUR)) == 5

    def test_a_zero_budget_selects_nothing(self):
        assert select_stale_rechecks(["a@g.us"], {}, NOW, 0, HOUR) == []

    def test_junk_in_the_verified_map_is_survivable(self):
        # It is read back off persisted metadata, so it may be anything.
        assert select_stale_rechecks(
            ["a@g.us"], {"a@g.us": "not-a-number"}, NOW, 5, HOUR) == ["a@g.us"]
        assert select_stale_rechecks(["a@g.us"], None, NOW, 5, HOUR) == ["a@g.us"]

    def test_blank_jids_are_ignored(self):
        assert select_stale_rechecks(["", None], {}, NOW, 5, HOUR) == []


class TestTheShippedBudget:
    def test_it_covers_the_reported_account_within_the_interval(self):
        """295 chats, a 60 s poll: 5 per round is 300 an hour, so the whole
        account is re-verified inside the hour this bounds staleness to."""
        per_hour = MainWindow._STALE_RECHECK_PER_ROUND * 60
        assert per_hour >= 295

    def test_the_interval_is_long_enough_not_to_be_a_second_poll(self):
        assert MainWindow._STALE_RECHECK_AFTER >= 1800
