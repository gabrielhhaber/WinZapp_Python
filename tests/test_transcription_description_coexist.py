"""ACC-08: a video keeps an AI description and a local transcription side by side.

Both live in the one `_transcription` envelope (one decision time, so every rule
of core/transcription/stored.py is unchanged); the description is a result of
`kind == "description"`, kept under its own key beside the transcription's.
Deleting one category re-dates the other as the newest decision; deleting the
last one is a tombstone.
"""

import inspect
import itertools

import pytest

from core.ai_media.saved import saved_description
from core.ai_media.summary_context import visual_text
from core.transcription import auto_queue, stored
from core.transcription.stored import TRANSCRIPTION_KEY

_JID = "5511988887777@s.whatsapp.net"
_ID = "3EB0DESCRIBED0001"
_SEEN = "zzqq um gato na janela yyww"
_SAID = "zzqq vamos jantar as oito yyww"


def _video(**extra):
    msg = {
        "key": {"id": _ID, "remoteJid": _JID, "fromMe": False},
        "messageType": "videoMessage",
        "messageTimestamp": 1_700_000_000,
        "message": {"videoMessage": {"seconds": 7, "mimetype": "video/mp4"}},
    }
    msg.update(extra)
    return msg


def _record(description, text, at, inline=True, provider="openai-model", backend=None):
    if description:
        return {"text": text, "model_id": provider, "backend": stored.ONLINE_BACKEND,
                "at": at, "inline": inline, "kind": stored.DESCRIPTION_KIND}
    return {"text": text, "model_id": "small", "backend": backend or "faster_whisper",
            "language": "pt", "vad_used": True, "at": at, "inline": inline}


def _save(previous, description, text, now=100.0, **kw):
    at = stored.next_decision_time(now, previous)
    return stored.with_result(previous, _record(description, text, at, **kw))


def _delete(previous, description, now=100.0):
    value, dropped = stored.drop_kind(
        previous, description, stored.next_decision_time(now, previous))
    return value if dropped else previous


def _has(value, description):
    msg = _video(**{TRANSCRIPTION_KEY: value})
    found = (stored.saved_description_record(msg) if description
             else stored.saved_transcription(msg))
    return found is not None


class TestBothKept:
    def test_a_description_and_a_transcription_coexist_in_either_order(self):
        for first in (True, False):
            value = _save(None, first, "um" if first else "dois")
            value = _save(value, not first, "dois" if first else "um")
            assert _has(value, True) and _has(value, False)
            assert stored.decision_time(value) is not None

    def test_the_same_provider_keeps_its_transcription_and_its_description(self):
        value = _save(None, False, _SAID, provider="p", backend=stored.ONLINE_BACKEND)
        value = _save(value, True, _SEEN, provider="p")
        msg = _video(**{TRANSCRIPTION_KEY: value})
        assert stored.saved_transcription(msg)["text"] == _SAID
        assert stored.saved_description_record(msg)["text"] == _SEEN

    def test_saved_transcription_never_hands_back_a_description(self):
        msg = _video(**{TRANSCRIPTION_KEY: _save(None, True, _SEEN)})
        assert stored.saved_transcription(msg) is None
        assert stored.saved_transcription(msg, ("faster_whisper", "small")) is None
        assert stored.saved_description_record(msg)["text"] == _SEEN

    def test_a_model_selection_still_finds_its_transcription_under_a_newer_description(self):
        value = _save(None, False, _SAID)
        value = _save(value, True, _SEEN)
        msg = _video(**{TRANSCRIPTION_KEY: value})
        assert stored.saved_transcription(msg, ("faster_whisper", "small"))["text"] == _SAID
        assert stored.saved_transcription(msg, ("faster_whisper", "large")) is None

    def test_a_run_again_replaces_only_its_own_category(self):
        value = _save(_save(None, False, "velho"), True, _SEEN)
        value = _save(value, False, _SAID)
        msg = _video(**{TRANSCRIPTION_KEY: value})
        assert stored.saved_transcription(msg)["text"] == _SAID
        assert stored.saved_description_record(msg)["text"] == _SEEN

    def test_a_new_transcription_does_not_inherit_the_descriptions_inline_mark(self):
        value = _save(None, True, _SEEN, inline=True)
        assert stored.is_inline(value)
        assert not stored.is_inline_transcription(value)
        assert stored.is_inline_transcription(_save(value, False, _SAID, inline=True))


class TestTheRow:
    def test_both_are_announced_description_first(self):
        value = _save(_save(None, False, _SAID), True, _SEEN)
        msg = _video(**{TRANSCRIPTION_KEY: value})
        assert stored.inline_row_parts(msg) == [
            ("description_row_label", _SEEN), ("transcription_row_label", _SAID)]
        assert stored.inline_row_text(msg) == f"{_SEEN} {_SAID}"
        assert stored.inline_row_label_key(msg) == "description_row_label"

    def test_only_an_inline_result_shows(self):
        value = _save(_save(None, False, _SAID, inline=False), True, _SEEN)
        msg = _video(**{TRANSCRIPTION_KEY: value})
        assert stored.inline_row_parts(msg) == [("description_row_label", _SEEN)]

    def test_a_transcription_only_row_is_as_it_was(self):
        msg = _video(**{TRANSCRIPTION_KEY: _save(None, False, "olá\nmundo")})
        assert stored.inline_row_parts(msg) == [("transcription_row_label", "olá mundo")]
        assert stored.inline_row_label_key(msg) == "transcription_row_label"

    def test_the_rendered_row_says_both(self):
        from ui.conversation_panel.message_rendering import _with_inline_text

        class _I18n:
            def t(self, key):
                return {"description_row_label": "descrição",
                        "transcription_row_label": "transcrição"}[key]

        value = _save(_save(None, False, "oi"), True, "gato")
        msg = _video(**{TRANSCRIPTION_KEY: value})
        line = _with_inline_text(None, msg, "Vídeo, duração: 0:07", _I18n())
        assert line == "Vídeo, duração: 0:07, descrição: gato, transcrição: oi"


class TestMigration:
    """Records written before this change keep reading the same."""

    def test_an_old_description_stored_under_the_providers_plain_key(self):
        old = {"text": _SEEN, "model_id": "p", "backend": "online", "at": 5.0,
               "inline": True, "kind": "description"}
        msg = _video(**{TRANSCRIPTION_KEY: old})
        assert stored.saved_description_record(msg)["text"] == _SEEN
        assert stored.inline_row_parts(msg) == [("description_row_label", _SEEN)]
        assert saved_description(msg, "video") == _SEEN

    def test_an_old_envelope_holding_both_under_by_model_reads_and_then_saves(self):
        # The old shape: the description was just one more entry of by_model.
        old = {"text": _SAID, "model_id": "small", "backend": "faster_whisper",
               "at": 9.0, "inline": True,
               "by_model": {"online:p": {"text": _SEEN, "model_id": "p", "backend": "online",
                                         "at": 7.0, "inline": True, "kind": "description"}}}
        msg = _video(**{TRANSCRIPTION_KEY: old})
        assert stored.saved_transcription(msg)["text"] == _SAID
        assert stored.saved_description_record(msg)["text"] == _SEEN
        # A new description of the same provider replaces the old entry and
        # does not duplicate it, and the transcription survives.
        value = _save(old, True, "novo gato", provider="p")
        assert sorted(r["text"] for r in stored.results_of(value)) == ["novo gato", _SAID]

    def test_an_old_transcription_only_record_is_untouched(self):
        old = _record(False, _SAID, 3.0)
        msg = _video(**{TRANSCRIPTION_KEY: old})
        assert stored.saved_transcription(msg) is old
        assert stored.saved_description_record(msg) is None


class TestDeletingOne:
    def test_deleting_the_transcription_keeps_the_description(self):
        value = _save(_save(None, False, _SAID), True, _SEEN)
        after = _delete(value, False)
        assert _has(after, True) and not _has(after, False)
        assert stored.decision_time(after) > stored.decision_time(value)

    def test_deleting_the_last_one_is_a_tombstone(self):
        value = _save(None, True, _SEEN)
        after = _delete(value, True)
        assert after.get("deleted") is True
        assert stored.results_of(after) == []

    def test_deleting_a_category_that_is_not_there_changes_nothing(self):
        value = _save(None, True, _SEEN)
        assert _delete(value, False) is value

    def test_delete_target_goes_transcription_first(self):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        assert stored.delete_target(both) is False
        assert stored.delete_target(_delete(both, False)) is True
        assert stored.delete_target(stored.tombstone(5.0)) is None
        assert stored.delete_target(None) is None

    def test_a_stale_copy_with_both_loses_to_the_partial_delete(self):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        after = _delete(both, False)
        stale = _video(**{TRANSCRIPTION_KEY: both})
        merged = stored.with_known_transcription(stale, _video(**{TRANSCRIPTION_KEY: after}))
        assert not _has(merged[TRANSCRIPTION_KEY], False)
        assert _has(merged[TRANSCRIPTION_KEY], True)

    def test_the_window_deletes_one_category_in_memory_and_tells_the_database(
            self, monkeypatch):
        import main as main_module
        from tests.test_transcription_stored import _Window
        monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        both = _save(_save(None, False, _SAID), True, _SEEN)
        record = _video(**{TRANSCRIPTION_KEY: both})
        window = _Window([record])
        answers = []

        window.delete_message_transcription(_JID, _ID, answers.append)

        assert answers == [True]
        assert not _has(record[TRANSCRIPTION_KEY], False)
        assert _has(record[TRANSCRIPTION_KEY], True)
        assert window.db.calls == [("delete", (_JID,), _ID)]

    def test_the_second_deletion_takes_the_description_and_leaves_a_tombstone(
            self, monkeypatch):
        import main as main_module
        from tests.test_transcription_stored import _Window
        monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        record = _video(**{TRANSCRIPTION_KEY: _save(_save(None, False, _SAID), True, _SEEN)})
        window = _Window([record])

        window.delete_message_transcription(_JID, _ID, lambda ok: None)
        window.delete_message_transcription(_JID, _ID, lambda ok: None)

        assert record[TRANSCRIPTION_KEY].get("deleted") is True

    def test_a_decision_made_while_the_delete_waited_is_not_undone(self, monkeypatch):
        import main as main_module
        from tests.test_transcription_stored import _Window
        monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        both = _save(_save(None, False, _SAID), True, _SEEN)
        record = _video(**{TRANSCRIPTION_KEY: both})
        window = _Window([record])
        newer = _save(both, False, "outra", now=10**11)

        class _Later(type(window.db)):
            def delete_message_transcription(self, jids, msg_id, deleted_at, description=None):
                record[TRANSCRIPTION_KEY] = newer
                return super().delete_message_transcription(jids, msg_id, deleted_at, description)

        window.db.__class__ = _Later
        answers = []
        window.delete_message_transcription(_JID, _ID, answers.append)

        assert answers == []
        assert stored.saved_transcription(record)["text"] == "outra"


class TestWithdrawnAndMerged:
    def test_a_message_deleted_for_everyone_loses_both(self):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        revoked = {"key": {"id": _ID}, "messageType": "protocolMessage",
                   TRANSCRIPTION_KEY: both}
        cleaned = stored.with_known_transcription(revoked, _video(**{TRANSCRIPTION_KEY: both}))
        assert TRANSCRIPTION_KEY not in cleaned

    def test_a_merge_folds_both_into_the_survivor(self):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        survivor, dropped = _video(), _video(**{TRANSCRIPTION_KEY: both})
        assert stored.fold_transcription(survivor, dropped)
        assert _has(survivor[TRANSCRIPTION_KEY], True) and _has(survivor[TRANSCRIPTION_KEY], False)

    def test_a_resync_carries_both_over(self):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        fresh = [_video()]
        assert stored.carry_over_transcriptions(fresh, [_video(**{TRANSCRIPTION_KEY: both})]) == 1
        assert _has(fresh[0][TRANSCRIPTION_KEY], True) and _has(fresh[0][TRANSCRIPTION_KEY], False)

    def test_a_write_keeps_the_rows_other_category(self):
        row = _save(_save(None, False, _SAID), True, _SEEN)
        built_in_memory = _save(None, True, "novo gato", now=100.0)
        merged = stored.with_missing_results(built_in_memory, row)
        assert sorted(r["text"] for r in stored.results_of(merged)) == sorted(["novo gato", _SAID])


class TestAutomaticRunsAreByCategory:
    def test_a_transcribed_video_is_still_described_and_the_other_way_round(self):
        transcribed = _video(**{TRANSCRIPTION_KEY: _save(None, False, _SAID)})
        described = _video(**{TRANSCRIPTION_KEY: _save(None, True, _SEEN)})
        assert not stored.has_decision(transcribed, True)
        assert stored.has_decision(transcribed, False)
        assert stored.has_decision(described, True)
        assert not stored.has_decision(described, False)

    def test_a_tombstone_is_a_decision_for_both(self):
        msg = _video(**{TRANSCRIPTION_KEY: stored.tombstone(5.0)})
        assert stored.has_decision(msg, True) and stored.has_decision(msg, False)

    def test_nothing_stored_is_no_decision(self):
        assert not stored.has_decision(_video(), True)
        assert not stored.has_decision(None, False)

    def test_the_local_automatic_transcription_ignores_a_description(self):
        msg = _video(**{TRANSCRIPTION_KEY: _save(None, True, _SEEN)})
        assert not stored.has_decision(msg, False)
        # auto_queue.eligible() asks has_decision(msg, False): a description is
        # not a decision about the sound.
        assert "has_decision(msg, False)" in inspect.getsource(auto_queue.eligible)


class TestDeletingOneCategoryIsADecisionAboutIt:
    def _both(self):
        return _save(_save(None, False, _SAID), True, _SEEN)

    def test_deleting_only_the_description_is_not_described_again(self):
        value = _delete(self._both(), True)
        msg = _video(**{TRANSCRIPTION_KEY: value})
        assert not _has(value, True) and _has(value, False)
        assert stored.has_decision(msg, True)
        assert stored.has_decision(msg, False)

    def test_deleting_only_the_transcription_is_not_transcribed_again(self):
        value = _delete(self._both(), False)
        assert stored.has_decision(_video(**{TRANSCRIPTION_KEY: value}), False)

    def test_an_explicit_new_description_lifts_the_mark_and_keeps_the_other(self):
        value = _delete(self._both(), True)
        again = _save(value, True, "um cachorro", now=200.0)
        assert stored.decided_of(again) == set()
        assert _has(again, True) and _has(again, False)
        assert stored.has_decision(_video(**{TRANSCRIPTION_KEY: again}), True)
        # and deleting it again marks again
        gone = _delete(again, True, now=300.0)
        assert stored.decided_of(gone) == {"description"}

    def test_a_new_transcription_keeps_the_descriptions_mark(self):
        value = _delete(self._both(), True)
        retyped = _save(value, False, "outra fala", now=200.0)
        assert stored.decided_of(retyped) == {"description"}
        assert stored.has_decision(_video(**{TRANSCRIPTION_KEY: retyped}), True)
        # the mark is not nested inside a result
        assert all(stored.DECIDED_KEY not in r for r in stored.results_of(retyped))

    def test_clearing_a_model_cache_keeps_the_mark(self):
        value = _delete(self._both(), True)
        value, _ = stored.drop_model(value, "small", None, 400.0)
        assert stored.decided_of(value) == {"description"} or value.get("deleted")

    def test_records_from_before_have_no_mark_and_read_as_ever(self):
        old = _save(None, False, _SAID)
        assert stored.DECIDED_KEY not in old and stored.decided_of(old) == set()
        assert not stored.has_decision(_video(**{TRANSCRIPTION_KEY: old}), True)

    def test_the_mark_of_a_tombstone_or_junk_is_ignored(self):
        assert stored.decided_of(stored.tombstone(1.0)) == set()
        assert stored.decided_of({"at": 1.0, "text": "x", "decided": "description"}) == set()
        assert stored.decided_of({"at": 1.0, "text": "x", "decided": ["bogus"]}) == set()
        assert stored.decided_of(None) == set()


class TestTheSummaryReadsBoth:
    def test_a_video_line_has_what_is_seen_but_never_what_is_said(self):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        line = visual_text(_video(**{TRANSCRIPTION_KEY: both}), "[Video]")
        assert line == f"[Video: {_SEEN}]" and _SAID not in line

    def test_a_local_transcription_alone_stays_out_of_the_summary(self):
        line = visual_text(_video(**{TRANSCRIPTION_KEY: _save(None, False, _SAID)}), "[Video]")
        assert line == "[Video]"

    def test_a_video_with_nothing_is_unchanged(self):
        assert visual_text(_video(), "[Video]") == "[Video]"


# ── The property: any sequence of saves and deletes ─────────────────────────

_OPS = [("save", True), ("save", False), ("delete", True), ("delete", False)]


@pytest.mark.parametrize("sequence", [
    seq for n in (1, 2, 3, 4) for seq in itertools.product(_OPS, repeat=n)])
def test_any_sequence_of_saves_and_deletes_agrees_with_a_plain_model(sequence):
    value, present, history = None, {True: False, False: False}, []
    for step, (op, description) in enumerate(sequence):
        if op == "save":
            value = _save(value, description, f"texto {step}")
            present[description] = True
        else:
            value = _delete(value, description) if value is not None else None
            present[description] = False
        history.append(value)
        if value is not None:
            assert _has(value, True) == present[True]
            assert _has(value, False) == present[False]
        else:
            assert not any(present.values())
    # No older copy ever beats the newest decision, in any step.
    newest = history[-1]
    if newest is not None:
        for older in history[:-1]:
            if older is not None and older is not newest:
                kept = stored.newer_decision(older, newest)
                assert kept is newest or stored.decision_time(older) == stored.decision_time(newest)


# ── The database ────────────────────────────────────────────────────────────


async def _row(db):
    [msg] = [m for m in await db.get_messages(_JID) if m["key"]["id"] == _ID]
    return msg


class TestTheDatabase:
    async def test_deleting_one_category_keeps_the_other_on_disk(self, in_memory_db):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        await in_memory_db.insert_message(_JID, _video())
        await in_memory_db.set_message_transcription(_JID, _ID, both)

        assert await in_memory_db.delete_message_transcription(_JID, _ID, 500.0, False)

        row = await _row(in_memory_db)
        assert stored.saved_transcription(row) is None
        assert stored.saved_description_record(row)["text"] == _SEEN
        assert stored.decision_time(row[TRANSCRIPTION_KEY]) >= 500.0

    async def test_a_stale_sync_does_not_bring_the_deleted_one_back(self, in_memory_db):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        await in_memory_db.insert_message(_JID, _video())
        await in_memory_db.set_message_transcription(_JID, _ID, both)
        await in_memory_db.delete_message_transcription(_JID, _ID, 500.0, False)

        await in_memory_db.insert_message(_JID, _video(**{TRANSCRIPTION_KEY: both}))

        row = await _row(in_memory_db)
        assert stored.saved_transcription(row) is None
        assert stored.saved_description_record(row) is not None

    async def test_deleting_the_last_category_leaves_a_tombstone(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _video())
        await in_memory_db.set_message_transcription(_JID, _ID, _save(None, True, _SEEN))

        assert await in_memory_db.delete_message_transcription(_JID, _ID, 500.0, True)

        assert (await _row(in_memory_db))[TRANSCRIPTION_KEY].get("deleted") is True

    async def test_deleting_a_category_the_row_lacks_is_not_a_failure(self, in_memory_db):
        await in_memory_db.insert_message(_JID, _video())
        await in_memory_db.set_message_transcription(_JID, _ID, _save(None, True, _SEEN))

        assert await in_memory_db.delete_message_transcription(_JID, _ID, 500.0, False)

        assert stored.saved_description_record(await _row(in_memory_db)) is not None

    async def test_a_plain_delete_still_removes_everything(self, in_memory_db):
        both = _save(_save(None, False, _SAID), True, _SEEN)
        await in_memory_db.insert_message(_JID, _video())
        await in_memory_db.set_message_transcription(_JID, _ID, both)

        assert await in_memory_db.delete_message_transcription(_JID, _ID, 500.0)

        assert (await _row(in_memory_db))[TRANSCRIPTION_KEY].get("deleted") is True
