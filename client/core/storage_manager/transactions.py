"""Fixed explicit plans, durable media blocks and recoverable same-volume moves."""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import ExitStack
import json
import os
import time
import uuid

from . import fileops
from .policy import eligibility
from .types import Entry, Root, canonical, inside


class PlanTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    snapshot_id: str
    entries: tuple[Entry, ...]
    method: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.time)
    automatic: bool = False


@dataclass
class Result:
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_removed: int = 0
    bytes_held: int = 0
    cancelled: bool = False
    reasons: dict = field(default_factory=dict)

    def note(self, reason):
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def make_plan(snapshot, selected, method, preferences, *, busy=lambda e: False):
    if method not in ("remove", "quarantine"):
        raise ValueError("unsupported storage action")
    selected = set(selected)
    entries = tuple(e for e in snapshot.entries if e.key in selected
                    and eligibility(e, preferences, busy=busy(e)) in ("local_copy", "reproducible"))
    # Rewriting a crash-safe journal is intentionally bounded. Refuse a
    # larger selection instead of silently applying only its first portion.
    if len(entries) > 500 or sum(len(json.dumps(e.__dict__, default=str)) for e in entries) > 500000:
        raise PlanTooLarge("select at most 500 files and a smaller journal payload")
    return Plan(snapshot.id, entries, method)


def _write(path, value):
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def execute(plan, snapshot, preferences, store, media_index, *, busy=lambda e: False,
            cancel=lambda: False, provider=fileops.pinned, progress=None):
    """No rescan or new candidates. Lock serializes a consumed plan marker."""
    if plan.snapshot_id != snapshot.id:
        raise ValueError("stale storage plan")
    if not 0 <= time.time() - plan.created <= 86400:
        raise ValueError("expired storage plan")
    if len(plan.id) != 32 or any(c not in "0123456789abcdef" for c in plan.id):
        raise ValueError("invalid plan identity")
    result = Result()
    # The durable plan marker also prevents double application after restart.
    with store.lock(), ExitStack() as stack:
        stack.enter_context(fileops.pinned_directory(store.directory))
        journal_dir = os.path.join(store.directory, "storage_quarantine")
        os.makedirs(journal_dir, exist_ok=True)
        stack.enter_context(fileops.pinned_directory(journal_dir))
        if not fileops.inside(journal_dir, store.directory):
            raise ValueError("invalid journal root")
        journal_path = os.path.join(journal_dir, plan.id + ".json")
        if os.path.lexists(journal_path):
            raise ValueError("storage plan already consumed")
        journal = dict(version=1, id=plan.id, created=time.time(), method=plan.method, items=[])
        _write(journal_path, journal)
        known = {e.key: e for e in snapshot.entries}
        for entry in plan.entries:
            if cancel():
                result.cancelled = True
                break
            if known.get(entry.key) != entry or eligibility(entry, preferences, busy=busy(entry)) not in ("local_copy", "reproducible"):
                result.skipped += 1
                result.note("protected")
                continue
            item = dict(root=entry.root.key, root_path=canonical(entry.root.path),
                        relative=entry.relative, identity=entry.identity, category=entry.category,
                        media_id=entry.media_id, chat_key=entry.chat_key, state="pending", size=entry.size,
                        held=uuid.uuid4().hex + ".held")
            journal["items"].append(item)
            added = set()
            try:
                # Journal and suppression precede the mutation. A crash can
                # leave a conservative block, never an unrecorded refetch.
                _write(journal_path, journal)
                if entry.media_id:
                    added = media_index.update_durable([entry.media_id])
                item["new_block"] = entry.media_id in added
                _write(journal_path, journal)
                with provider(entry) as pinned:
                    if busy(entry) or cancel():
                        raise fileops.ChangedFile("became busy or cancelled")
                    if plan.method == "quarantine":
                        pinned.move(os.path.join(journal_dir, item["held"]))
                        result.bytes_held += entry.size
                    else:
                        pinned.remove()
                        result.bytes_removed += entry.size
                result.completed += 1
                item["state"] = "held" if plan.method == "quarantine" else "removed"
            except (OSError, ValueError, NotImplementedError):
                result.failed += 1
                result.note("changed_or_unavailable")
                item["state"] = "failed"
                if added:
                    try:
                        media_index.update_durable(added, remove=True)
                    except (OSError, ValueError):
                        result.note("index_recovery")
            # If this fails after mutation, preserve the pending journal and
            # stop; never continue claiming that the full plan was completed.
            _write(journal_path, journal)
            if progress is not None:
                progress(result.completed + result.failed + result.skipped, len(plan.entries))
        journal["finished"] = not result.cancelled
        if plan.method == "remove" or all(i["state"] in ("failed", "removed", "restored") for i in journal["items"]):
            # Keep the replay token, not a permanent list of private paths.
            journal["items"] = []
        _write(journal_path, journal)
    return result
