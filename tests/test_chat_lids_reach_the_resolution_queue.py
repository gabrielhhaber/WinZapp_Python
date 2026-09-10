"""A chat whose own JID is an @lid has to reach the LID resolution queue.

Reported as duplicated contacts in the "new conversation" picker — the same
person listed twice, once under @lid and once under their phone number.

The picker already deduplicates, through core.utils.contact_dedup_key(), which
collapses the two forms of one person **as long as _lid_to_phone holds the
pair**. It usually did not. Measured live against a real install:

    /list-chats     159 chats,   132 of them @lid
    /all-contacts   487 contacts, 244 of them @lid, 208 names appearing twice
    LID cache       42 lids bridged

Neither payload carries the link — an @lid contact arrives with no phoneNumber
field, and an @lid chat's `remoteJid` is null while `accountLid` merely repeats
the @lid. The bridge is built from messages instead, and the only producers for
the resolution queue are per-message: a group message's sender, and @lid
mentions. So a chat whose own JID is an @lid was bridged only if that person
happened to turn up as a sender or a mention somewhere else.

_run_sync() computed exactly the right list, logged "Deferring N unresolved
@lid chat(s) to background name backfill", and then dropped it on the floor.
Nothing handed it to the queue.

The resolution itself was never the missing part: resolve_lid_jids_via_api()
exists, batches, caches what cannot be resolved, and persists — and
WPPConnect's /contact/pn-lid/ answers every one of those lids with its phone
number (verified live against three of them, including the exact pair behind a
duplicate in the picker).
"""

import ast
from pathlib import Path


MAIN = Path(__file__).resolve().parents[1] / "client" / "main.py"


def _run_sync_source():
    source = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_sync":
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError("_run_sync() not found in main.py")


def _uncommented(text):
    return "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#"))


def test_the_unresolved_chat_lids_are_queued_not_just_counted():
    """The list was computed and logged, and that was all — which is why an
    install could sit on 132 unbridged @lid chats indefinitely."""
    body = _uncommented(_run_sync_source())
    assert "unresolved_lids" in body
    assert "_queue_lid_resolutions(unresolved_lids)" in body, (
        "_run_sync() still only logs the unresolved @lid chats; nothing hands "
        "them to the queue, so contact_dedup_key() never learns the pair and "
        "the picker keeps listing both forms of the same person"
    )


def test_it_is_queued_rather_than_resolved_inline():
    """Deferring is the point: resolving even one chunk here is serial and
    keeps the app in "synchronizing" long after every conversation is already
    usable. _queue_lid_resolutions()' drain loop sleeps until _sync_completed,
    so the hand-off costs the sync nothing."""
    body = _uncommented(_run_sync_source())
    assert "resolve_lid_jids_via_api" not in body


def test_the_queue_still_waits_for_the_sync_to_finish():
    """The other half of the same promise, pinned here because this change is
    what starts relying on it: if the drain loop stopped deferring, the sync
    would now be the thing paying for these lookups."""
    source = MAIN.read_text(encoding="utf-8")
    start = source.index("def _queue_lid_resolutions")
    body = source[start:start + 3000]
    assert "_initial_sync_running" in body
    assert "_sync_completed" in body


def test_the_queue_is_a_set_so_requeuing_is_free():
    """_run_sync() runs again on every full round and will re-offer whatever
    is still unbridged, so the producer side has to tolerate repeats."""
    source = MAIN.read_text(encoding="utf-8")
    start = source.index("def _queue_lid_resolutions")
    body = source[start:start + 3000]
    assert "= set()" in body
    assert "pending.update(unique)" in body
