"""Counters of how each provider fared, kept in the install-wide ``ai_media`` block.

Only the provider id, an outcome category and counts are stored: never a
prompt, an answer, a file name, a chat or a key. The categories are the
``ai_error_<category>`` keys a failure already carries.
"""
import logging

from .config import PROVIDERS

OK = "ok"
#: The outcomes of one provider attempt that are counted. A cancel, bad media
#: or a local output limit says nothing about the provider, so it is not here.
CATEGORIES = ("authentication", "quota", "credits", "server", "request", "refusal",
              "response", "network", "timeout", "credentials")
#: A counter stops here instead of growing without limit.
CEILING = 10_000_000


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, CEILING)


def validated(raw):
    """``{provider: {"ok": n, "failed": {category: n}, "last": category}}`` with
    unknown providers, categories and malformed numbers dropped."""
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for provider in PROVIDERS:
        entry = raw.get(provider)
        if not isinstance(entry, dict):
            continue
        failed = entry.get("failed")
        failed = failed if isinstance(failed, dict) else {}
        counts = {c: _number(failed.get(c)) for c in CATEGORIES}
        counts = {c: n for c, n in counts.items() if n}
        last = entry.get("last")
        result[provider] = {"ok": _number(entry.get("ok")), "failed": counts,
                            "last": last if last in counts else ""}
    return result


def counted(block, provider, category):
    """The stored ``ai_media`` dict (``block``, possibly damaged) with one more
    attempt of ``provider`` counted as ``category`` (``OK`` or a failure)."""
    value = dict(block) if isinstance(block, dict) else {}
    if provider not in PROVIDERS or (category != OK and category not in CATEGORIES):
        return value
    usage = validated(value.get("usage"))
    entry = usage.setdefault(provider, {"ok": 0, "failed": {}, "last": ""})
    if category == OK:
        entry["ok"] = min(entry["ok"] + 1, CEILING)
    else:
        entry["failed"][category] = min(entry["failed"].get(category, 0) + 1, CEILING)
        entry["last"] = category
    value["usage"] = usage
    return value


def cleared(block):
    """The stored ``ai_media`` dict without any usage counter."""
    value = dict(block) if isinstance(block, dict) else {}
    value.pop("usage", None)
    return value


def persist(provider, category):
    """Write one outcome to the install-wide settings (the sink the callers of
    ``run_chain`` pass). Several account processes share the file, hence update()."""
    from app_paths import global_dir
    from app_settings import AppSettings
    AppSettings(global_dir()).update("ai_media", lambda old: counted(old, provider, category))


def note(sink, provider, category):
    """Hand an outcome to ``sink`` when there is one. Counting must never fail
    a request, and the exception text is not logged (it can quote a path)."""
    if sink is None:
        return
    try:
        sink(provider, category)
    except Exception as exc:
        logging.info("[ai-media] usage not counted: %s", type(exc).__name__)


def summary_lines(usage, label, category_label, none_text):
    """One readable line per provider that has counters, for the settings page.

    ``label(ok, failed, last)`` and ``category_label(category)`` are the
    caller's translated formatters; ``last`` is already a label or ``""``."""
    lines = []
    for provider, entry in usage.items():
        failed = sum(entry["failed"].values())
        if not entry["ok"] and not failed:
            continue
        last = category_label(entry["last"]) if entry["last"] else ""
        lines.append(label(PROVIDERS[provider].name, entry["ok"], failed, last))
    return "\n".join(lines) if lines else none_text
