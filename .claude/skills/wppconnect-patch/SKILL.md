---
name: wppconnect-patch
description: Change WinZapp's patches on top of WPPConnect Server or its wppconnect library. Use when a fix has to happen on the Node side — a controller, middleware, route, start.js, config.json, a dependency in package.json, or the compiled wppconnect code inside node_modules — or when client/api/ and client/api_patches/ have drifted. Explains which of the three patch mechanisms applies and every list that has to be updated with it.
---

# Patching WPPConnect

`client/api/` is a clone of upstream `wppconnect-team/wppconnect-server`, is
git-ignored, and is **not** where you edit anything. WinZapp keeps its changes
as patches applied on top, through three separate mechanisms. Picking the wrong
one is silent: the change appears to work until the next `setup_api.py` run
throws it away.

Decide which mechanism applies **before** editing anything.

## Mechanism 1 — WPPConnect Server's own source

Applies to `start.js`, `config.json`, `decrypt.js` and everything under
`src/**` (controllers, middleware, routes, util, dto, services).

**Edit the tracked copy: `client/api_patches/<same relative path>`.** Never
`client/api/`. Editing the live copy looks like it works and is reverted by the
next setup run — that is exactly what happened to `start.js` in `daf2d352`,
where the npx-cli.js resolution fallback (needed on machines with no
system-wide Node) was added to the live copy only and thrown away.

Adding a **new** file to the patch set means updating **three** lists, not one:

| where | list |
| --- | --- |
| `setup_api.py` | `CUSTOM_ROOT_FILES` / `CUSTOM_SRC_FILES` — the source of truth (28 files today: 6 root + 22 src) |
| `client/ui/dialogs/api_setup.py` | `_CUSTOM_ROOT_FILES` / `_CUSTOM_SRC_FILES` — the end-user install flow, which has its own copy |
| `tests/test_api_patches_in_sync.py` | `MIRRORED_FILES` — or the file is restored forever while nothing ever compares the two copies |

`src/middleware/auth.ts` and `src/controller/statusController.ts` both sat in
that blind spot. The test asserts *set equality* against `setup_api.py`
precisely because a subset check misses the direction that bites.

`ApiSetupDialog` legitimately restores one extra entry, `dist/middleware/auth.js`
— a compiled artifact with no counterpart in `api_patches/` — which is why its
check is containment rather than equality.

## Mechanism 2 — `package.json`

Not a full-file restore. `_merge_package_json_dependencies()` overrides only the
entries named in `_PATCHED_DEPENDENCY_KEYS` (today: `prom-client`, `zod`,
`@ffmpeg-installer/ffmpeg`, `qrcode`, `@wppconnect-team/wppconnect`,
`@wppconnect/wa-js`) and re-serializes, so WPPConnect Server's own `version`
field keeps reflecting the tag actually cloned. Both `setup_api.py` and
`api_setup.py` carry that list and a test holds them equal.

**`@wppconnect-team/wppconnect` + `@wppconnect/wa-js` are one homologated pair,
pinned exact.** They are the compiled code mechanism 3 rewrites by literal
search-and-replace and that `deviceController.ts` drives through private WA-JS
loader APIs, so a caret range lets a plain reinstall move the browser-side send
and status APIs underneath an unchanged WinZapp build, with nothing failing
until a user tries to send something. Measured: `2.3.2` (resolved from
upstream's own `^2.2.7` while nothing pinned the key) rewrote `host.layer.js`'s
`checkQrCode()`/`loginByCode()`, which turned the v8 pairing-code rotation
cooldown into a silent no-op — two warnings among forty lines of `setup_api.py`
output, and nothing else.

Moving the pair is a deliberate act: bump both keys **and**
`client/wpp_minimum_version.txt` in the same commit, after running all four
`node_modules` patches against the candidate and confirming each still matches.

The pin is also why the server tag and the pin cannot be edited independently —
the original reason the pin was once removed was a stale `2.2.4` sitting under
a server release that had already moved to `^2.2.6`, i.e. a pin that no longer
satisfied its own server's range. That is now a test
(`tests/test_wpp_homologated_runtime_pin.py`), not a prohibition.

**`@wppconnect/wa-version` stays unpinned**, deliberately: it is the expiring
catalogue of WhatsApp Web builds, not an API surface, and freezing it strands
an install on entries Meta has stopped serving (see CLAUDE.md's version-pin
section).

## Mechanism 3 — compiled code inside `node_modules`

For bugs in `@wppconnect-team/wppconnect`'s own *compiled* output — not
WPPConnect Server's source, so mechanism 1 cannot reach it. There are four such
patch modules in `client/core/`:

```
wppconnect_host_layer_patch.py      host.layer.js    — pairing-code rotation cooldown
wppconnect_sender_layer_patch.py    sender.layer.js  — attachment sending
wppconnect_status_layer_patch.py    status.layer.js  — status post success/failure
wppconnect_welcome_layer_patch.py   welcome.js
```

Each holds the patch text as `ORIGINAL_*` / `PATCHED_*` constants plus an
`ALL_PATCHES` tuple, applied by **idempotent search-and-replace**. Follow that
shape for a new one: idempotent means re-running must be a no-op, because both
call sites run repeatedly.

**Two call sites must stay in sync**, and this is the part people miss:

1. `setup_api.py` — dev and CI.
2. `ApiSetupDialog._apply_node_modules_patches()` in
   `client/ui/dialogs/api_setup.py` — the real end-user install, which needs
   its own copy because `npm install` on a user's machine re-fetches pristine
   `node_modules`.

A patch added to only one of them works for you and ships broken.

## After any change: re-run setup

```
python setup_api.py
```

A file copy alone is not enough: `client/api/dist/server.js` is precompiled JS
and only `npm run build` regenerates it from the patched `.ts` sources. That
gap is what made stale patches shippable — `build.py`'s `check_tools()` now
diffs every patched file and re-runs `setup_api.py` itself on drift.

## Verify

```
pytest tests/test_api_patches_in_sync.py tests/test_reapply_node_modules_patches.py tests/test_pairing_code_patch.py tests/test_status_layer_patch.py tests/test_welcome_layer_patch.py tests/test_large_file_patch.py
```

**Expect `test_the_two_copies_of_each_patch_are_identical` to fail locally
after pulling changes that touched `client/api_patches/`** — your `client/api/`
was built from the older patch set and is now genuinely behind. That is the
test doing its job, not a repo problem; the fix is re-running `setup_api.py`.
CI's fast test job skips those cases entirely because `client/api/` does not
exist there, which is also why local drift is the only place they can catch
anything.
