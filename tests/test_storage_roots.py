"""Application paths partition storage without reading account secrets."""
from core.storage_manager.roots import build_roots, classify
from core.storage_manager.scan import scan
from core.storage_manager.types import canonical


def layout(tmp_path, include_other=False):
    program = tmp_path / "app"
    data = program / "data"
    account = data / "accounts" / "current"
    account.mkdir(parents=True)
    return build_roots(program=str(program), resources=str(program), data=str(data),
                       account=str(account), include_other=include_other), account


def put(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_current_scope_excludes_sibling_accounts_but_counts_shared_models(tmp_path):
    roots, account = layout(tmp_path)
    put(account / "media" / "image.wzmedia", 10)
    put(account.parent / "sibling" / "private", 100)
    put(account.parents[1] / "global" / "whisper_models" / "weights", 20)
    result = scan(roots, classify=classify)
    assert result.complete and result.total_bytes == 30
    assert result.totals() == {"media": (1, 10), "models": (1, 20)}


def test_opted_in_other_accounts_are_counted_but_not_managed(tmp_path):
    roots, account = layout(tmp_path, include_other=True)
    put(account.parent / "sibling" / "message.db", 100)
    result = scan(roots, classify=classify)
    value, = result.entries
    assert value.size == 100 and not value.root.managed


def test_default_compile_cache_and_profile_are_not_the_same_kind(tmp_path):
    roots, account = layout(tmp_path)
    shared = account.parents[1] / "global"
    put(shared / "node-compile-cache" / "bytecode", 5)
    put(shared / "api" / "userDataDir" / "session" / "keys", 7)
    put(shared / "api" / "userDataDirSnapshots" / "session.prev" / "keys", 9)
    result = scan(roots, classify=classify)
    assert result.total_bytes == 21
    assert result.totals() == {"backups": (1, 9), "sessions": (1, 7), "compile_cache": (1, 5)}
    assert next(e for e in result.entries if e.category == "compile_cache").root.managed
    assert all(e.reason == "critical" for e in result.entries if e.category in ("backups", "sessions"))


def test_external_models_are_information_only(tmp_path):
    roots, account = layout(tmp_path)
    external = tmp_path / "other_application_models"
    put(external / "weights", 11)
    roots = build_roots(program=str(tmp_path / "app"), resources=str(tmp_path / "app"),
                        data=str(account.parents[1]), account=str(account),
                        external_models=[str(external / "weights")])
    result = scan(roots, classify=classify)
    value, = result.entries
    assert value.category == "models" and value.root.scope == "external"
    assert not value.root.managed


def test_legacy_flat_account_does_not_drop_its_media_or_double_count_global(tmp_path):
    data = tmp_path / "app" / "data"
    put(data / "media" / "x", 7)
    put(data / "global" / "whisper_models" / "w", 13)
    roots = build_roots(program=str(data.parent), resources=str(data.parent),
                        data=str(data), account=str(data))
    result = scan(roots, classify=classify)
    assert result.total_bytes == 20 and result.complete


def test_frozen_other_account_scope_excludes_later_folders_and_marks_missing_ones(tmp_path):
    _, account = layout(tmp_path)
    allowed, later = account.parent / "allowed", account.parent / "later"
    put(allowed / "private", 5)
    put(later / "private", 100)
    roots = build_roots(program=str(account.parents[2]), resources=str(account.parents[2]),
                       data=str(account.parents[1]), account=str(account), include_other=True,
                       other_accounts=[str(allowed), str(account.parent / "gone")])
    result = scan(roots)
    assert result.total_bytes == 5 and result.issues == {"missing": 1}


def test_external_file_root_never_sweeps_a_folder_if_the_path_changes_kind(tmp_path):
    roots, account = layout(tmp_path)
    external = tmp_path / "external"
    put(external / "unrelated", 100)
    roots = build_roots(program=str(account.parents[2]), resources=str(account.parents[2]),
        data=str(account.parents[1]), account=str(account), external_models=[str(external)])
    result = scan(roots)
    assert result.total_bytes == 0 and result.issues == {"unsupported": 1}
