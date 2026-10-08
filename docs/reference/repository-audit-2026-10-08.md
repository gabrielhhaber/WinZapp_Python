# Repository audit — 2026-10-08

## Scope

The audit used the tracked checkout at `9ba61104` on
`codex/customizable-shortcuts`. All 1,283 tracked text files (494,686 lines)
were read by the inventory/static scan, including 71 Markdown files and
1,122 Python files. Every Python file parsed successfully. This is a static
inventory, not a claim that every line received a manual semantic review.
Detailed review covered the contribution rules, subsystem indexes, test
configuration, shortcut/settings changes and their relevant bug histories.

The main composition files delegate to responsibility-specific mixins:
`client/main_window/`, `client/ui/conversation_panel/` and
`client/status_tab/`. At the audited revision their entry files contain
1,989, 1,290 and 517 lines respectively; `settings_dialog.py` contains 4,047.
New behavior should follow the package ownership map rather than enlarge
these entry files. See [architecture and test ownership](architecture-and-tests.md).

## Five upstream CI failures

[CI run 37706070667](https://github.com/gabrielhhaber/WinZapp_Python/actions/runs/37706070667)
reported five failures, 17,451 passes and 301 skips on the shortcut change:

- Two incoming-call dialog tests supplied a parent with `i18n` but no
  `settings`. Shortcut label binding now reads the parent's preferences.
  The fixtures now supply empty settings, exercising the default gestures.
- Three settings-notebook tests assumed Transcription was the last page.
  Shortcuts now follows it. Assertions now check both page identities and
  translated titles: 18 pages with Locked chats visible, 17 when hidden.
  Existing fixed page indices remain covered.

The structural Transcription test now states its narrower contract: it
examines inline `AddPage()` calls before the Shortcuts mixin appends its page.
The real-notebook tests cover the complete order. Locale assertion failure
messages also use the actual locale variable instead of an undefined name.

## Documentation and test-harness findings

- Serial test guidance replaces stale `-n auto` examples that contradicted
  the desktop safety rules. Native dialog tests remain CI-only.
- Status documentation now describes the existing REST read, cache merge
  and readiness requirement before deleting absent own-status entries.
- Node upgrade guidance names all six current library patch paths and
  distinguishes library pair upgrades from server-tag upgrades.
- Settings migration examples are described as examples rather than the
  complete current migration list. Shortcut descriptions identify defaults.
- Accessibility guidance preserves message row identity instead of showing
  a clear-and-rebuild example.
- Generated API drift still fails its tests. The failure now reports file
  paths without asking pytest to diff enormous byte strings: a diagnostic
  stack showed `difflib` spending minutes formatting one mismatch.

The local `.agents/skills/` copies were synchronized with the authoritative
`.claude/skills/` versions. They were already untracked local configuration.
The locale registry contains seven locales; translation validation passed
with zero pending entries.

## Validation

The focused shortcut, incoming-call, settings and desktop-safety selection
passed **1,809 tests**, with **24 expected native-dialog skips**. Local runs
cannot verify the five native-dialog cases directly; their fixture and order
corrections require CI's wxGUI run.

The completed full serial run reported **17,121 passed, 21 failed, 616
skipped**, plus 154 passed subtests, in 419.59 seconds. It is not a green run.
The failures split into:

- **15 installed-runtime mismatches:** nine generated API source files,
  three additional installed-source/bridge checks, and three dependency-pin
  checks. Installed wppconnect is 2.3.3 instead of 2.3.4; installed wa-js is
  4.6.0 instead of 4.6.1. Regeneration is needed through the setup/build path,
  when replacing the local runtime is appropriate. No generated API files
  or running client/server processes were changed by this audit.
- **One browser VM fixture mismatch:** startup timing needs `process.hrtime`
  and emits diagnostics before the JSON result. The fixture now supplies
  the clock and parses the final result line, preserving its exact required
  browser assertion. This test passed in the follow-up selection.
- **Five filesystem-dependent failures:** one interrupted snapshot deletion
  and four staged WA catalogue operations. Follow-up runs failed on different
  catalogue cases, even with a shorter temporary path. Instrumenting the
  filesystem calls observed `PermissionError`, errno 13, **WinError 5** on
  directory rename. This is an unresolved intermittent local filesystem
  restriction; its cause was not established. No retry or skip was added to
  hide it. The snapshot test passed on follow-up.

The follow-up browser/cache, profile and WA-refresh selection reported
**138 passed, 2 failed**; both failures were catalogue filesystem cases.
Earlier full attempts were interrupted after diagnosing restricted temporary
directory cleanup and oversized assertion-diff formatting. Native GUI tests
remained disabled throughout. Translation checks passed for all seven locales;
`git diff --check` passed.

The final browser-cache and structural Transcription selection passed
**318 tests** after the VM fixture correction.

Live WhatsApp delivery, NVDA speech, cross-device synchronization and native
macOS behavior are outside this local audit's verification boundary.
