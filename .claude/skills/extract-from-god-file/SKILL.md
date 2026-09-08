---
name: extract-from-god-file
description: Pull a slice of logic out of one of WinZapp's god files without changing behaviour. Use when asked to extract, split, shrink or refactor `client/main.py`, `client/ui/conversations.py` or `client/status_panel.py`, when a method is too tangled to test, or when a bug fix keeps being blocked by not being able to reach the logic from a test.
---

# Extracting from a god file

## The two files, and the actual reason to do this

`client/main.py` is ~26,900 lines and `client/ui/conversations.py` ~16,000.
`client/status_panel.py` (~3,000) is a distant third.

The reason to extract is **not** that big files are ugly. It is that
`MainWindow` is a `wx.Frame` and `ConversationsPanel` a `wx.Panel`: neither can
be instantiated without a running `wx.App`, so logic sitting on them is
reachable from a test only through a hand-built stub that has to be kept in
sync with whatever attributes the method touches. The same logic at module
level is imported and called directly.

So the test for whether an extraction is worth doing is concrete: **does it
turn a stub test into a direct one, or make an untestable thing testable?** If
the answer is no, you are moving code around for aesthetics, and this repo has
better uses for a diff.

## Where extracted code goes

Two destinations, and the cheap one is usually right.

**Module level in the same file.** `main.py` already holds 21 module-level
functions — `is_countable_message()`, `own_message_marks_chat_read()`,
`unread_after_history_sync()`, `history_gap_detected()` and the rest of that
block. They were pulled off `MainWindow` for exactly this reason and are
tested directly. No new file, no import churn, no circular-import risk. Start
here.

**A module under `client/core/`.** Justified when the slice is cohesive and
carries its own state or lifecycle — `notification_manager.py`,
`message_queue.py`, `video_player.py`, `token_vault.py`,
`database_bridge.py`. Do not create a new package, a new layer, or a
`services/` directory. The repo is flat under `core/` and staying flat is
what makes it navigable.

## The shape to copy

Mirror the existing module-level functions exactly: type-annotated signature,
and a docstring that explains **why**, usually naming the incident that
motivated it. `is_countable_message()`'s docstring is the reference — it
explains why the check derives from an allowlist rather than keeping a second
blocklist, and names the bug that proved it.

Take arguments; do not reach back into `self`. A function that needs six
attributes of `MainWindow` is telling you the slice is drawn wrong — redraw it
before extracting.

## Procedure

1. **Establish the baseline before touching anything.**
   ```
   venv/Scripts/python.exe -m pytest -q
   ```
   Record the number. If something is already failing, you need to know that
   now — otherwise you cannot tell your breakage from what you inherited.

2. **Characterize first.** Write a test against the behaviour *as it is
   today*, through a stub if that is the only way in (see `write-test`). This
   test is the contract: it must pass before and after, unchanged. If you
   cannot write it before moving the code, say so and say why — that is a
   real finding about the slice, not a formality to skip.

3. **One responsibility per pass.** Not one method — one responsibility. Two
   unrelated slices in one diff cannot be reverted independently, and the
   review cannot tell which one broke something.

4. **Move the code, verbatim.** No renames, no reformatting, no "while I'm
   here" fixes, no exception-handling improvements outside what you are
   already touching. A behaviour-preserving extraction whose diff is pure
   movement can be reviewed in minutes; the same extraction with three
   drive-by improvements cannot be reviewed at all.

5. **Leave the god file thin.** The old method either disappears or becomes a
   one-line call to the new function. Never leave the logic duplicated in
   both places — that is strictly worse than not extracting, because the two
   copies drift and nothing tells you.

6. **Add the direct test** the extraction just made possible. This is the
   payoff; without it the extraction bought nothing.

7. **Verify.**
   ```
   venv/Scripts/python.exe -m pytest -q
   ```
   Same count as the baseline, plus your new tests. Bare `pytest` and
   `python -m pytest` do not resolve on a dev machine here.

## What must not change

An extraction is behaviour-preserving by definition. These are the areas where
"I just tidied it slightly" has shipped real bugs, and where the reviewer will
look first:

- **JID normalization** — the `@lid`/phone bridge, the Brazilian 8/9-digit
  handling, the fake-`@g.us` guard.
- **The live-events gate** — anything behind `_live_events_ready()` stays
  behind it.
- **Echo matching by message type** — reordering or "simplifying" that
  matching swaps real WhatsApp IDs between unrelated messages.
- **Speech and list mutation** — `speak_output`, `Freeze()`/`Thaw()`. See
  `accessible-ui`.
- **Anything that touches the five locales.** See `i18n-ui-string`.

If making the extraction clean seems to require changing one of these, stop.
The extraction is wrong, not the invariant.

## Tooling this repo does not have

There is no ruff, no mypy, no `pyproject.toml`, no pre-commit hook and no
coverage threshold. `pytest` is the whole gate. Do not invent a lint step, and
do not add one as part of an extraction — that is a separate decision for the
team, not a side effect of moving a function.

## Finishing

Report the before/after `wc -l` of the god file as raw numbers, the baseline
and final test counts, and what the extraction made testable that was not
before. Then hand the diff to `winzapp-reviewer`.
