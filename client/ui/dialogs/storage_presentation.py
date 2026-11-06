"""Translated storage rows; raw message identifiers never become list names."""
from datetime import datetime


CATEGORY_KEYS = {
    "program": "storage_category_program", "media": "storage_category_media",
    "voice": "storage_category_voice", "models": "storage_category_models",
    "backups": "storage_category_backups", "sessions": "storage_category_sessions",
    "database": "storage_category_database", "logs": "storage_category_logs",
    "compile_cache": "storage_category_compile_cache", "quarantine": "storage_category_quarantine",
    "other": "storage_category_other",
}
REASON_KEYS = {
    "protected": "storage_reason_protected", "critical": "storage_reason_critical",
    "important": "storage_reason_important", "external": "storage_reason_external",
    "user_protected": "storage_reason_user_protected", "in_use": "storage_reason_in_use",
    "reproducible": "storage_reason_reproducible", "local_copy": "storage_reason_local_copy",
    "received": "storage_reason_local_copy",
    "unmatched": "storage_reason_unmatched",
}


def size_text(size, i18n):
    unit = "B"
    number = float(size)
    for next_unit in ("KiB", "MiB", "GiB", "TiB"):
        if number < 1024:
            break
        number /= 1024
        unit = next_unit
    number = f"{number:.1f}".replace(".", i18n.t("decimal_separator"))
    return f"{number} {unit}"


def category_text(category, i18n):
    return i18n.t(CATEGORY_KEYS.get(category, "storage_category_other"))


def reason_text(reason, i18n):
    return i18n.t(REASON_KEYS.get(reason, "storage_reason_protected"))


def entry_name(entry, ordinal, i18n):
    if not entry.display_name:
        return i18n.t("storage_private_item").format(number=ordinal) + "; " + category_text(entry.category, i18n)
    label = entry.display_name
    # Label comes only from an allowed visible chat, never the raw cache name.
    return i18n.t("storage_item_label").format(name=label, number=ordinal, category=category_text(entry.category, i18n))


def modified_text(entry):
    try:
        return datetime.fromtimestamp(entry.modified).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return ""


def summary(snapshot, preferences, i18n):
    scope = i18n.t("storage_scope_all" if preferences.include_other_accounts else "storage_scope_current")
    if snapshot is None:
        return scope + "\n" + i18n.t("storage_not_scanned")
    status = "storage_scan_complete" if snapshot.complete else "storage_scan_partial"
    if snapshot.cancelled:
        status = "storage_scan_cancelled"
    lines = [scope, i18n.t(status), i18n.t("storage_logical_total").format(
        size=size_text(snapshot.total_bytes, i18n), count=len(snapshot.entries)),
        i18n.t("storage_measured_at").format(time=datetime.fromtimestamp(snapshot.created).strftime("%Y-%m-%d %H:%M"))]
    for category, (count, size) in sorted(snapshot.totals().items(), key=lambda item: item[1][1], reverse=True):
        lines.append(i18n.t("storage_category_summary").format(
            category=category_text(category, i18n), count=count, size=size_text(size, i18n)))
    if snapshot.issues:
        lines.append(i18n.t("storage_coverage_skipped").format(count=sum(snapshot.issues.values())))
    lines.append(i18n.t("storage_accounting_note"))
    return "\n".join(lines)
