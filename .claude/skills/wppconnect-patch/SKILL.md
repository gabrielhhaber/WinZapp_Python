---
name: wppconnect-patch
description: Change WinZapp's patches on top of WPPConnect Server or its wppconnect library. Use when a fix has to happen on the Node side — a controller, middleware, route, start.js, config.json, a dependency in package.json, or the compiled wppconnect code inside node_modules — or when client/api/ and client/api_patches/ have drifted. Explains which of the three patch mechanisms applies and every list that has to be updated with it.
---

# Patching WPPConnect

`client/api/` is a git-ignored clone of `wppconnect-team/wppconnect-server`.
**Never edit it**: the next `setup_api.py` run overwrites it. WinZapp's
changes are applied on top through three mechanisms — pick the right one
before editing.

## 1 — WPPConnect Server's own source

For `start.js`, `config.json`, `decrypt.js` and everything under `src/**`.
Edit `client/api_patches/<same relative path>`.

A **new** patched file must be added to three lists:

| where | list |
| --- | --- |
| `setup_api.py` | `CUSTOM_ROOT_FILES` / `CUSTOM_SRC_FILES` (source of truth) |
| `client/ui/dialogs/api_setup.py` | `_CUSTOM_ROOT_FILES` / `_CUSTOM_SRC_FILES` (end-user install) |
| `tests/test_api_patches_in_sync.py` | `MIRRORED_FILES` |

`ApiSetupDialog` also restores `dist/middleware/auth.js`, a compiled artifact
with no counterpart in `api_patches/`.

## 2 — `package.json`

Merged, not copied: `_merge_package_json_dependencies()` overrides only the
keys in `_PATCHED_DEPENDENCY_KEYS`. Both `setup_api.py` and `api_setup.py`
carry that list; a test holds them equal.

- **`@wppconnect-team/wppconnect` and `@wppconnect/wa-js` are one
  homologated pair, pinned exact.** Mechanism 3 rewrites their compiled code
  by literal search-and-replace, so a moved version silently disables a
  patch. To move the pair: review both library pins and run all six
  `node_modules` patches against the candidate. Change
  `client/wpp_minimum_version.txt` only when the homologated server version
  also changes; a library-only upgrade can retain the same server tag.
- When a new runtime restructures patched code, add a **second patch set
  selected by matching the file** (as `host.layer.js` has for ≤ 2.3.1 and
  ≥ 2.3.2). Never edit a shipped one: both call sites re-run on every launch
  against whatever `node_modules` holds.
- **`@wppconnect/wa-version` stays unpinned**: it is an expiring catalogue
  (`docs/traps/whatsapp-web-version-pin.md`).
- Upgrade procedure: `docs/traps/wppconnect-upgrade.md`.

## 3 — compiled code inside `node_modules`

For bugs in `@wppconnect-team/wppconnect`'s compiled output. Five modules in
`client/core/`:

```
wppconnect_host_layer_patch.py      host.layer.js    pairing-code lifecycle
wppconnect_sender_layer_patch.py    sender.layer.js  attachment sending
wppconnect_status_layer_patch.py    status.layer.js  status post success/failure
wppconnect_welcome_layer_patch.py   welcome.js
wppconnect_browser_layer_patch.py   browser.js       injectApi readiness timeout
```

The first four hold `ORIGINAL_*` / `PATCHED_*` constants and an `ALL_PATCHES` tuple,
applied by idempotent search-and-replace (re-running must be a no-op). A
shipped `_V<n>` constant is never edited — add a version and a migration
(`docs/traps/large-media.md`).

`wppconnect_browser_layer_patch.py` is shaped like the wa-js one below instead
(matched by structure, owns its file handling).

A sixth module patches the other half of the pair, `@wppconnect/wa-js`'s
bundle — the script injected into the WhatsApp Web page, so a bug in
`WPP.*` itself is fixed there:

```
wppconnect_wa_js_patch.py           wppconnect-wa.js  Meta AI send without a loaded bot profile
```

Its bundle is minified, and the minifier renames temporaries between builds,
so it matches the expression by structure (a regex with a back-reference for
the temporary) instead of by one literal per build, and owns the file
handling too: call sites pass the outer `client/api/` directory to
`patch_wa_js_bundle()`.

**The call sites must stay in sync**; a patch in only one ships broken:

1. `setup_api.py` — dev and CI.
2. `ApiSetupDialog._apply_node_modules_patches()` in
   `client/ui/dialogs/api_setup.py` — the end-user install, and every launch.
3. `build_api.py`'s `_apply_node_modules_patches()` — the patcher tuple.

## After any change

```
uv run setup-api
```

A file copy is not enough: `client/api/dist/server.js` is compiled, and only
the build regenerates it from the patched `.ts` sources.

## Verify

```
uv run pytest tests/test_api_patches_in_sync.py tests/test_reapply_node_modules_patches.py tests/test_pairing_code_patch.py tests/test_status_layer_patch.py tests/test_welcome_layer_patch.py tests/test_wa_js_patch.py tests/test_large_file_patch.py
```

`test_the_two_copies_of_each_patch_are_identical` fails locally after pulling
changes to `client/api_patches/`: your `client/api/` is behind. Re-run
`uv run setup-api`. CI skips those cases because `client/api/` does not exist
there.
