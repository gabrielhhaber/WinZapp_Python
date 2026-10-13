"""A transcription kept with its message, and the rules that keep it there.

A transcription takes minutes; doing it twice for the same voice note is the
waste this module exists to prevent. It lives inside the message's own record,
under `TRANSCRIPTION_KEY`, which puts it inside `message_json` — the column the
database encrypts with the per-install Fernet key. That is the whole privacy
argument: a column of its own would have been the text of a private
conversation in the clear, readable by anyone holding `messages.db`.

The key sits at the **top level of the record**, beside `_local_pending` and
the other local-only fields, never inside `message`: the `message` payload is
what a reply copies into its `quotedMessage` and what prune/slim helpers
rewrite, and a transcription carried along with it would end up stored under
somebody else's message.

What makes it hard is that nothing on the server knows about it, and every
resync writes the server's copy over the record — in the database
(`INSERT OR REPLACE` of the whole row) and in memory (the conversation's
records replaced by the API's). `MEASURED_SECONDS_KEY` hit exactly this and
settled the shape: one rule, applied on both sides, at the single point every
write passes through. The rule here is one step stricter, because unlike a
measured duration a transcription can be *deleted*:

* **Every value carries the time it was decided (`at`), and the later decision
  wins.** A copy arriving without the key inherits what is stored; a copy
  arriving with it wins only if it is at least as recent. Deleting writes a
  **tombstone** (`{"deleted": True, "at": ...}`) instead of removing the key,
  so a stale copy still holding the text — the conversation panel's, a star
  toggled on the old dict, a sync that read the records before the delete —
  loses to it instead of bringing the transcription back. Removing the key
  would have turned every one of those copies into "a copy with the key wins".

* **Only a message that can hold speech carries one.** `audioMessage`,
  `videoMessage` and `documentMessage` (`CARRYING_TYPES`) — by type, not by
  mimetype, because a message's type never
  changes while one copy of a document may lack the mimetype another had, and
  dropping a transcription because of that would be a silent loss.

* **Only a withdrawn message loses it.** A message deleted for everyone comes
  back under its own id as a `protocolMessage` (`websocket_client`'s
  `"revoked"`, `MainWindow._apply_remote_revoke()`), and the text of what its
  sender withdrew is not something to keep — nor is one that finishes after
  the withdrawal: `MainWindow.store_message_transcription()` refuses it, and
  `DatabaseManager._rewrite_transcription()` will not write a text over a
  row that cannot hold one. Any other copy that is not audio
  or a document says nothing about the transcription either way. A known kind
  (`OTHER_KINDS`) cannot be the same message, so it is left as it came, with no
  read; a type WPPConnect passes through unmapped (a `ciphertext` placeholder
  of a note not yet decrypted, say) may well be the voice note itself, so a
  stored value is kept exactly as it is — the copy's own key neither replaces
  it nor drops it — and with nothing stored the copy is written as it came,
  its key included. Dropping on "not audio" alone would have erased a
  transcription for good the first time such a placeholder was written.

* **Merging two copies keeps the later decision.** The same message can sit
  under two chats (an `@lid` and its phone number) until they are merged, and
  a merge deletes one of the two. `fold_transcription()` is what the survivor
  takes from the one being dropped — in the database
  (`merge_or_rename_chat()`) and in memory (`_merge_lid_into_phone()`,
  `deduplicate_chats()`).

* **The time of a decision only moves forward.** `at` comes from the wall
  clock, and a clock corrected backwards between two decisions would make the
  newer one lose — a delete undone by the next sync. `next_decision_time()`
  dates every new decision after the one it replaces.

What is deliberately *not* kept: a full resync (F5) or a logout clears the
database with `import_from_dict(clear_first=True)`, and every transcription
goes with it, exactly as the downloaded voice notes in `voice_messages/` do —
they were made from those files, and a wipe that kept them would keep the
text of conversations the user asked to forget. And a message that enters
memory through `on_historical_message()` or `fetch_older_messages()` arrives
from the server without the key: the row on disk keeps it (the write goes
through the rule), but the menu offers `transcription_transcribe_again` rather than
`transcription_view` until the conversation is reopened and read back from the database. Reading every
such message back on the UI thread to avoid that would cost more than the
second run it could save.

Nothing here imports anything heavier than the backend's result type, and
`core.database` imports this module — which is why it is not in
`core.utils`, whose own imports start with `requests`.
"""

from __future__ import annotations

from core.transcription.backend import TranscriptionResult

#: Where the transcription lives in a message record. Private (leading
#: underscore) like `_measured_seconds`: WhatsApp never sends such a field, so
#: an incoming copy can never collide with it.
TRANSCRIPTION_KEY = "_transcription"

#: The types a transcription can be attached to — see the module docstring for
#: why by type and not by message_audio.is_transcribable()'s mimetype check.
CARRYING_TYPES = ("audioMessage", "videoMessage", "documentMessage")

#: What a message deleted for everyone becomes — the only copy that drops the
#: key.
WITHDRAWN_TYPE = "protocolMessage"

#: The kinds websocket_client maps a message to that can never be a voice note
#: or a document seen differently: a message's type never changes, so a copy
#: of one of these is another message, and writing it needs no read of the
#: stored row. Anything outside this set and CARRYING_TYPES — a type passed
#: through unmapped — may be.
OTHER_KINDS = frozenset((
    "conversation", "extendedTextMessage", "imageMessage",
    "stickerMessage", "contactMessage", "locationMessage", "liveLocationMessage",
    "pollCreationMessage", "buttonsMessage", "listMessage", "templateMessage",
    "groupNotification", "reactionMessage", "orderMessage", "orderUpdateMessage", "productMessage",
))


# ── What is stored ───────────────────────────────────────────────────────────


def record_from_result(result, at, inline=False) -> dict:
    """The stored form of a finished `TranscriptionResult`.

    Everything the window needs to say the truth when it is opened again: the
    text, the detected language and how sure the model was (the language and
    confidence notes), the model and backend (said with the date), and
    `vad_used` — a transcription made without the voice filter has to keep
    warning about it on every reopening, since it is the one degradation a
    listener cannot hear. The segments are not kept: nothing reads them yet,
    and they would roughly double the size of every stored transcription.

    `inline` is True for a transcription the user asked to have in the
    message's own line of the list (`inline_row_text()`); it is the only thing
    that makes the row carry the text. Left out of the record when False, so a
    record made by the window is exactly what it always was.
    """
    probability = getattr(result, "language_probability", None)
    record = {
        "text": getattr(result, "text", "") or "",
        "language": getattr(result, "language", None),
        "language_probability": float(probability) if isinstance(probability, (int, float))
        and not isinstance(probability, bool) else None,
        "model_id": getattr(result, "model_id", "") or "",
        "backend": getattr(result, "backend", "") or "",
        "vad_used": getattr(result, "vad_used", True) is not False,
        "at": float(at),
    }
    if inline:
        record["inline"] = True
    return record


def tombstone(at) -> dict:
    """What deleting leaves behind: no text, only the time of the decision."""
    return {"deleted": True, "at": float(at)}


def decision_time(value):
    """When `value` was decided, or None when it is not a stored decision."""
    if not isinstance(value, dict):
        return None
    at = value.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return float(at)


def next_decision_time(now, *previous):
    """When a new decision is dated: `now`, or just after the latest of
    `previous` if the clock says otherwise.

    The later decision wins everywhere (`newer_decision()`), so a clock set
    back between two decisions — corrected after running fast, or simply
    wrong — would make the newer one lose to the older, and a sync still
    holding the older copy would bring a deleted text back. Per message, the
    order of decisions is what matters, not the hour.
    """
    at = float(now)
    for value in previous:
        before = decision_time(value)
        if before is not None and before >= at:
            at = before + 1e-3
    return at


# ── One transcription per model ──────────────────────────────────────────────
#
# The text a model produced is that model's, not the message's: after a change
# of model the stored one answers a question nobody is asking any more. So a
# record belongs to the model that made it (`model_id` + `backend`), and what a
# message offers is the record of the model the user has selected *now*.
#
# Results of the models that are not current are kept inside the same record,
# under BY_MODEL_KEY, one per model — so that going back to a model brings its
# text back without another run, and so that the whole thing is still ONE value
# under TRANSCRIPTION_KEY with ONE decision time: every rule above (which copy
# wins, tombstones, merges, the database write) keeps working unchanged. The
# outer fields are always the newest result.
#
# "auto" (the default) does not name a model, so under it nothing changes: the
# newest result is the one offered, exactly as before.

#: The results of the models that are not the outer record's, by model key.
BY_MODEL_KEY = "by_model"
#: When the text was made, once `at` has become the time of a later decision
#: (clearing another model's cache promotes a record that is older than that).
MADE_AT_KEY = "made_at"
_AUTO = "auto"


def selection_from_settings(settings):
    """(backend, model) the user has selected, "auto" where they chose none.

    From `settings["transcription"]`, tolerant of anything: a settings file
    from before the feature, or a section that is not a dict, reads as
    ("auto", "auto") — the selection under which every stored text is offered.
    """
    section = settings.get("transcription") if isinstance(settings, dict) else None
    if not isinstance(section, dict):
        return (_AUTO, _AUTO)

    def pick(name):
        value = section.get(name, _AUTO)
        return value if isinstance(value, str) and value else _AUTO

    return (pick("backend"), pick("model"))


def model_key(backend, model_id) -> str:
    """The key a result is kept under: "backend:model", or "" when unknown."""
    if not model_id:
        return ""
    return f"{backend or ''}:{model_id}"


def record_model_key(record) -> str:
    if not isinstance(record, dict):
        return ""
    return model_key(record.get("backend"), record.get("model_id"))


def _has_text(record) -> bool:
    text = record.get("text") if isinstance(record, dict) else None
    return isinstance(text, str) and bool(text.strip())


def _core(value) -> dict:
    """The outer record without the other models' results."""
    return {k: v for k, v in value.items() if k != BY_MODEL_KEY}


def results_of(value) -> list:
    """Every result held under a stored transcription, the outer one first.

    Each is a plain record (no nesting) with its own `at`. Blank texts and
    junk are left out; a tombstone or a non-record has none.
    """
    if decision_time(value) is None or value.get("deleted"):
        return []
    found = []
    if _has_text(value):
        found.append(_core(value))
    others = value.get(BY_MODEL_KEY)
    if isinstance(others, dict):
        for record in others.values():
            if isinstance(record, dict) and _has_text(record):
                found.append(record)
    return found


def matches_selection(record, selection) -> bool:
    """Whether `record` was made by the selected backend and model.

    "auto" on either side matches anything: it names no model to differ from.
    So does a record that names no model (stored before results were kept per
    model): there is no model to tell it apart by, and dropping it would throw
    away a text the user already waited for.
    """
    if not record.get("model_id"):
        return True
    backend, model = selection if selection else (_AUTO, _AUTO)
    if model != _AUTO and record.get("model_id") != model:
        return False
    if backend != _AUTO and record.get("backend") != backend:
        return False
    return True


def saved_transcription(msg, selection=None):
    """The transcription stored on `msg` that the selection allows, or None.

    Without a selection (or under "auto" for both) that is the newest result —
    the outer record — as it always was. With a model selected it is that
    model's result, wherever it is held, and None when only other models have
    one: the message then has nothing stored as far as this model is concerned.
    A tombstone is None too.
    """
    if not isinstance(msg, dict):
        return None
    value = msg.get(TRANSCRIPTION_KEY)
    if decision_time(value) is None or value.get("deleted"):
        return None
    if selection is None or tuple(selection) == (_AUTO, _AUTO):
        return value if _has_text(value) else None
    # The newest first, and as the outer record itself (not a copy) so that a
    # caller that keeps the value it was handed keeps the other models' results.
    if _has_text(value) and matches_selection(value, selection):
        return value
    others = value.get(BY_MODEL_KEY)
    if isinstance(others, dict):
        for record in others.values():
            if _has_text(record) and matches_selection(record, selection):
                return record
    return None


def with_result(previous, record) -> dict:
    """`record` as the new outer result, keeping `previous` models' results.

    What a new transcription is stored as: it becomes the newest, and the
    result that was the newest — together with the ones it held — stays under
    BY_MODEL_KEY unless `record` is from the same model, which replaces it.
    """
    record = _core(record)
    mine = record_model_key(record)
    kept = {}
    for old in results_of(previous):
        key = record_model_key(old)
        if key and key != mine:
            kept[key] = old
    if kept:
        record[BY_MODEL_KEY] = kept
    return record


def with_missing_results(value, current):
    """`value`, also holding the other models' results `current` has and it lacks.

    For the database's write of a new text: the value was built from the
    in-memory copy (with_result(previous, …)), and a message that reached
    memory without the key (on_historical_message) built it without the other
    models' results the row holds. Writing it whole would lose them. Only
    results of models `value` names nowhere are added; `value`'s own stay as
    they are. A tombstone, or a `current` with nothing, leaves `value` as is.
    """
    if decision_time(value) is None or value.get("deleted"):
        return value
    present = {record_model_key(record) for record in results_of(value)}
    missing = {}
    for record in results_of(current):
        key = record_model_key(record)
        if key and key not in present:
            missing[key] = record
    if not missing:
        return value
    own = value.get(BY_MODEL_KEY)
    merged = dict(value)
    merged[BY_MODEL_KEY] = {**missing, **(own if isinstance(own, dict) else {})}
    return merged


def drop_model(value, model_id, backend_id, decided_at):
    """`value` without the results of one model: (new value, how many dropped).

    `backend_id` None drops that model id under every backend. The newest of
    what is left becomes the outer record, dated `decided_at` — a decision later
    than the one it replaces, which is what lets it beat a stale copy still
    holding the dropped text — with `made_at` keeping the date its text was
    made. When nothing is left the value is a tombstone at `decided_at`. A
    value with nothing of that model comes back unchanged (count 0).
    """
    results = results_of(value)

    def is_dropped(record):
        return (record.get("model_id") == model_id
                and (backend_id is None or record.get("backend") == backend_id))

    dropped = sum(1 for record in results if is_dropped(record))
    if not dropped:
        return value, 0
    remaining = [record for record in results if not is_dropped(record)]
    if not remaining:
        return tombstone(decided_at), dropped
    newest = max(remaining, key=lambda record: decision_time(record) or 0.0)
    kept = {record_model_key(record): record for record in remaining
            if record is not newest and record_model_key(record)}
    promoted = dict(newest, at=float(decided_at))
    promoted.setdefault(MADE_AT_KEY, newest.get("at"))
    if kept:
        promoted[BY_MODEL_KEY] = kept
    return promoted, dropped


def drop_all(value, decided_at):
    """`value` without any result: (a tombstone at `decided_at`, how many went).

    A value with no result (a tombstone already, junk) comes back unchanged
    with count 0.
    """
    dropped = len(results_of(value))
    if not dropped:
        return value, 0
    return tombstone(decided_at), dropped


def drop_all_on_copies(copies, decided_at, present=None):
    """`drop_all()` on every record of `copies`; the ids of the messages that
    changed, in first-seen order (see drop_model_on_copies())."""
    return _drop_on_copies(
        copies, decided_at, present,
        lambda value: drop_all(value, next_decision_time(decided_at, value)))


def drop_model_on_copies(copies, model_id, backend_id, decided_at, present=None):
    """`drop_model()` on every record of `copies` holding a transcription.

    Returns the ids of the messages that changed, in first-seen order. Each
    distinct dict is visited once; copies of one message end up agreeing
    because they start from the same stored value and share `decided_at`.

    `present` is `values_on(copies)` taken when the clear was asked for. A
    value that is not among them and is dated after `decided_at` was stored
    while the clear waited in the queue — and reached the disk after it — so
    it is left alone: clearing it here would date a tombstone after it, and the
    next sync would carry that tombstone to the disk too. A value that was
    there is cleared whatever its date (a clock that ran behind), as the
    database clears it.
    """
    # Dated like the database dates it (clear_model_transcriptions()): after
    # this copy's own decision, whatever the clock says.
    return _drop_on_copies(
        copies,
        decided_at,
        present,
        lambda value: drop_model(
            value, model_id, backend_id, next_decision_time(decided_at, value)))


def values_on(copies) -> list:
    """The stored values `copies` hold now, for drop_*_on_copies(`present`).

    A list of the values themselves, not their ids: holding them keeps an id
    from being reused by a value stored later.
    """
    return [record.get(TRANSCRIPTION_KEY) for record in copies or ()
            if isinstance(record, dict) and isinstance(record.get(TRANSCRIPTION_KEY), dict)]


def _drop_on_copies(copies, decided_at, present, transform):
    known = None if present is None else {id(value) for value in present}
    changed, seen_dicts = [], set()
    for record in copies or ():
        if not isinstance(record, dict) or id(record) in seen_dicts:
            continue
        seen_dicts.add(id(record))
        value = record.get(TRANSCRIPTION_KEY)
        if not isinstance(value, dict):
            continue
        if known is not None and id(value) not in known:
            at = decision_time(value)
            if at is not None and at > float(decided_at):
                # Stored after the clear was asked for (drop_model_on_copies()).
                continue
        new_value, dropped = transform(value)
        if not dropped:
            continue
        record[TRANSCRIPTION_KEY] = new_value
        msg_id = (record.get("key") or {}).get("id", "")
        if msg_id and msg_id not in changed:
            changed.append(msg_id)
    return changed


# ── Whether a result is kept at all (the "cache" settings) ───────────────────

#: Keys inside the settings.json "transcription" section.
CACHE_ENABLED_KEY = "cache_enabled"
CACHE_BY_MODEL_KEY = "cache_by_model"


def cache_enabled_for(settings, model_id) -> bool:
    """Whether a result of this model is kept with the message.

    The model's own choice (`cache_by_model`, by model id) when the user made
    one, else the general switch (`cache_enabled`, on by default). Keyed by the
    model id alone: the two backends' catalogues never share an id, and the tab
    that sets it knows the model but not always the backend. Tolerant of
    anything: only a real False turns the general switch off and only a real
    bool counts as a model's choice.
    """
    section = settings.get("transcription") if isinstance(settings, dict) else None
    if not isinstance(section, dict):
        return True
    own = section.get(CACHE_BY_MODEL_KEY)
    if model_id and isinstance(own, dict) and isinstance(own.get(model_id), bool):
        return own[model_id]
    return section.get(CACHE_ENABLED_KEY) is not False


def may_cache(settings, value) -> bool:
    """`cache_enabled_for()` for the newest result held by `value`; a value that
    names no model (a tombstone) is not a result and is always allowed."""
    if not isinstance(value, dict) or not value.get("model_id"):
        return True
    return cache_enabled_for(settings, value.get("model_id"))


def clear_cache_message_key(count) -> str:
    """The i18n key saying how clearing a model's saved transcriptions ended:
    `count` is how many results went, None when the database failed."""
    if count is None:
        return "transcription_clear_cache_failed"
    return "transcription_clear_cache_done" if count else "transcription_clear_cache_none"


def made_time(value):
    """When the text of `value` was made — not when it was last decided about."""
    if not isinstance(value, dict):
        return None
    made = value.get(MADE_AT_KEY)
    if isinstance(made, (int, float)) and not isinstance(made, bool):
        return float(made)
    return decision_time(value)


def marked_inline(value, previous, now):
    """`previous` with `value` (one of its results) marked to show in the line.

    A new decision — dated after `previous`, whatever the clock says — about an
    old text: the text keeps the date it was made (MADE_AT_KEY), or "transcribed
    on" would be announced as today. The chosen model's result becomes the
    newest; the others stay.
    """
    chosen = dict(value, inline=True, at=next_decision_time(now, previous))
    made = made_time(value)
    if made is not None:
        chosen.setdefault(MADE_AT_KEY, made)
    return with_result(previous, chosen)


def is_inline(value) -> bool:
    """Whether a stored transcription was asked to show in the message's line."""
    return isinstance(value, dict) and value.get("inline") is True


def inline_row_text(msg, selection=None) -> str:
    """The text the message's row carries after its duration, or "".

    One line: a row of the list has no line breaks, and a transcription has
    them wherever the speaker paused. Only a transcription stored as inline
    shows here — one opened in the result window leaves the row as short as it
    was, which is the long-standing choice for rows that are read on every pass.
    """
    value = saved_transcription(msg, selection)
    if value is None or not is_inline(value):
        return ""
    return " ".join(value["text"].split())


def as_result(value) -> TranscriptionResult:
    """A stored transcription as the result type narration and the window read.

    `vad_used` is believed only when it says True: a stored value that lost the
    field is treated like one made without the filter, because the warning is
    cheap and its absence is the one mistake the listener cannot notice.
    """
    probability = value.get("language_probability")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        probability = None
    return TranscriptionResult(
        text=value.get("text") or "",
        language=value.get("language") or None,
        language_probability=probability,
        duration_seconds=None,
        backend=value.get("backend") or "",
        model_id=value.get("model_id") or "",
        vad_used=value.get("vad_used") is True,
    )


# ── Which copy wins ──────────────────────────────────────────────────────────


def _message_type(msg):
    """`msg`'s type — `messageType`, or the payload's own key without it."""
    msg_type = msg.get("messageType")
    if msg_type:
        return msg_type
    payload = msg.get("message")
    if isinstance(payload, dict):
        for known in CARRYING_TYPES + (WITHDRAWN_TYPE,):
            if known in payload:
                return known
    return ""


def can_carry_transcription(msg) -> bool:
    """Whether `msg`'s type is one a transcription may be attached to."""
    if not isinstance(msg, dict):
        return False
    return _message_type(msg) in CARRYING_TYPES


def is_withdrawn(msg) -> bool:
    """Whether `msg` is a message its sender deleted for everyone."""
    return isinstance(msg, dict) and _message_type(msg) == WITHDRAWN_TYPE


def may_hold_transcription(msg) -> bool:
    """Whether a copy like `msg` may be the message a transcription belongs to.

    Audio and documents, and any type passed through unmapped; never a
    withdrawn message, nor a known other kind. What decides whether writing
    `msg` needs the stored row read first.
    """
    if not isinstance(msg, dict):
        return False
    msg_type = _message_type(msg)
    if msg_type in CARRYING_TYPES:
        return True
    return msg_type != WITHDRAWN_TYPE and msg_type not in OTHER_KINDS


def newer_decision(incoming, stored):
    """Which of two values of the key to keep: the later decision.

    A tie goes to `incoming` — the same decision written twice, where either
    answer is the same one. Anything that is not a stored decision counts as
    absent, so junk under the key can never outrank a real value.
    """
    incoming_at = decision_time(incoming)
    stored_at = decision_time(stored)
    if incoming_at is None:
        return stored if stored_at is not None else None
    if stored_at is None or incoming_at >= stored_at:
        return incoming
    return stored


def with_known_transcription(msg, stored_msg):
    """`msg` as it should be written over `stored_msg` — the database's rule.

    Returns `msg` itself when nothing changes, and a shallow copy otherwise, so
    the caller's dict (often a record the UI is holding) is never mutated by a
    write.
    """
    if not isinstance(msg, dict):
        return msg
    if is_withdrawn(msg):
        if TRANSCRIPTION_KEY not in msg:
            return msg
        cleaned = dict(msg)
        del cleaned[TRANSCRIPTION_KEY]
        return cleaned
    if not may_hold_transcription(msg):
        return msg
    incoming = msg.get(TRANSCRIPTION_KEY)
    stored = stored_msg.get(TRANSCRIPTION_KEY) if isinstance(stored_msg, dict) else None
    if not can_carry_transcription(msg):
        # A type passed through unmapped: the stored value stays exactly as
        # it is, whatever this copy holds — see the module docstring.
        if decision_time(stored) is None:
            return msg
        kept = stored
    else:
        kept = newer_decision(incoming, stored)
        if (kept is incoming and kept is not None
                and decision_time(incoming) == decision_time(stored)):
            # The same decision on both sides, one of them knowing more models'
            # results: a copy built in memory without the row's (a message
            # loaded through on_historical_message) must not drop them.
            kept = with_missing_results(incoming, stored)
    if kept is incoming and (kept is not None or TRANSCRIPTION_KEY not in msg):
        return msg
    merged = dict(msg)
    if kept is None:
        merged.pop(TRANSCRIPTION_KEY, None)
    else:
        merged[TRANSCRIPTION_KEY] = kept
    return merged


def carry_over_transcriptions(new_msgs, old_msgs) -> int:
    """The same rule for the records held in memory. Returns how many
    transcriptions it carried — texts only, a tombstone carried is not one.

    A resync replaces a conversation's records with the server's copies, which
    never hold the key; without this the transcription would survive on disk
    and vanish from the screen at the first sync — `transcription_view` gone from
    the menu until the next restart. Mutates `new_msgs` in place, the way
    `carry_over_video_durations()` does.
    """
    known = {}
    for m in old_msgs or ():
        if not isinstance(m, dict):
            continue
        mid = (m.get("key") or {}).get("id") if isinstance(m.get("key"), dict) else None
        value = m.get(TRANSCRIPTION_KEY)
        if not mid or decision_time(value) is None:
            continue
        known[mid] = newer_decision(value, known.get(mid))
    if not known:
        return 0
    changed = 0
    for m in new_msgs or ():
        if not may_hold_transcription(m):
            continue
        mid = (m.get("key") or {}).get("id") if isinstance(m.get("key"), dict) else None
        if mid not in known:
            continue
        current = m.get(TRANSCRIPTION_KEY)
        kept = newer_decision(current, known[mid])
        if kept is not current:
            m[TRANSCRIPTION_KEY] = kept
            if not kept.get("deleted"):
                changed += 1
    return changed


def fold_transcription(dst_record, src_record) -> bool:
    """Give `dst_record` what `src_record` knows, before `src_record` is dropped.

    For merges: the same message filed under two chats, one copy about to be
    deleted. The survivor keeps the later decision of the two — a text only
    the dropped copy had, or a tombstone that must keep beating it — by the
    same `newer_decision()` every write goes through. Mutates `dst_record` in
    place, because in memory it is the dict the conversation panel may be
    holding, and returns whether anything changed, which is what tells the
    database side the surviving row needs writing.
    """
    if not isinstance(dst_record, dict) or not isinstance(src_record, dict):
        return False
    if not may_hold_transcription(dst_record):
        return False
    current = dst_record.get(TRANSCRIPTION_KEY)
    kept = newer_decision(current, src_record.get(TRANSCRIPTION_KEY))
    if kept is None or kept is current:
        return False
    dst_record[TRANSCRIPTION_KEY] = kept
    return True


# ── Finding the message again ────────────────────────────────────────────────


def find_record(records, msg_id):
    """The record in `records` that is message `msg_id`, or None.

    By `key.id` first; failing that, by `_local_id`, which is what an own
    message was known by while it was still being sent. A transcription takes
    minutes, and a message sent meanwhile has changed its id under the flow
    that captured the old one.
    """
    if not msg_id:
        return None
    fallback = None
    for record in records or ():
        if not isinstance(record, dict):
            continue
        key = record.get("key") if isinstance(record.get("key"), dict) else {}
        if key.get("id") == msg_id:
            return record
        if fallback is None and record.get("_local_id") == msg_id:
            fallback = record
    return fallback


def is_unsent(record) -> bool:
    """Whether `record` is an own message that has no WhatsApp id yet.

    Such a message is stored under a local UUID that is replaced when the send
    is confirmed — and its echo can arrive as a separate record that replaces
    it outright — so a transcription written under the UUID could end up on a
    row nothing reads again. It is refused instead, and the window says so.
    """
    if not isinstance(record, dict):
        return True
    if record.get("_local_pending"):
        return True
    local_id = record.get("_local_id")
    key = record.get("key") if isinstance(record.get("key"), dict) else {}
    return bool(local_id) and key.get("id") == local_id


def set_on_copies(copies, msg_id, value) -> int:
    """Put `value` under the key on every record in `copies` that is `msg_id`.

    The same message can be held by several dicts at once — the chat's
    records, its `lastMessage`, the conversation panel's own lists after a
    resync swapped the chat under it — and every one of them may be written
    back to the database by some later path. Returns how many distinct dicts
    were changed.
    """
    seen = set()
    for record in copies or ():
        if not isinstance(record, dict) or id(record) in seen:
            continue
        key = record.get("key") if isinstance(record.get("key"), dict) else {}
        if msg_id and key.get("id") == msg_id:
            record[TRANSCRIPTION_KEY] = value
            seen.add(id(record))
    return len(seen)


def set_where_newer(copies, msg_id, value):
    """Put `value` only on the copies of `msg_id` it is not older than.

    For a decision that reaches memory after the database answered — the
    delete's tombstone: the queue it waited in may have held it behind a
    newer transcription the user asked for meanwhile, which is already on
    the copies and already on disk after the tombstone. Writing it blindly
    would replace that newer text in memory with the older deletion.

    Returns `(accepted, found)`: how many distinct dicts took `value`, and
    how many are `msg_id` at all. None found is not the same as none
    accepted — a message no longer in memory has nothing to disagree with.
    """
    seen = set()
    accepted = 0
    for record in copies or ():
        if not isinstance(record, dict) or id(record) in seen:
            continue
        key = record.get("key") if isinstance(record.get("key"), dict) else {}
        if not msg_id or key.get("id") != msg_id:
            continue
        seen.add(id(record))
        if newer_decision(value, record.get(TRANSCRIPTION_KEY)) is value:
            record[TRANSCRIPTION_KEY] = value
            accepted += 1
    return accepted, len(seen)


# ── What storing answered ────────────────────────────────────────────────────

#: MainWindow.store_message_transcription()'s answers. Only SAVE_STORED means
#: the transcription will be there next time; SAVE_UNSENT and SAVE_MISSING are
#: said in the result window, so that nobody expects to find it again and does
#: not. SAVE_WITHDRAWN opens no window at all: the message was deleted for
#: everyone while it was being transcribed (see ui.transcription_flow).
SAVE_STORED = "stored"
SAVE_UNSENT = "unsent"
SAVE_MISSING = "missing"
SAVE_WITHDRAWN = "withdrawn"
#: The cache is off for the model that made it (Settings > Local transcription).
SAVE_DISABLED = "disabled"
