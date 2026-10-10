# Auto-updater: two channels, the alpha version scheme, one prompt per machine

## Browser cache during WPPConnect updates

The in-place API clean step preserves `.cache`; a background build copies
complete Windows/Linux browser builds from the live cache into its own cache
(`core/browser_cache.py`). Never move or hard-link the running browser into
staging: a cancelled build or a repair must not alter the live API. The
Puppeteer CLI still selects its required version and downloads it if absent.
Incomplete version directories (missing/empty executable or `icudtl.dat`)
are removed only from the destination, so the CLI can repair them.

Preserving the cache also preserves older versions. `start.js` prefers
Puppeteer's requested executable before its existing fallback search; otherwise
the directory walk could launch the old Chrome after a successful update.
Keep `useChrome: false` and the platform preference: system-Chrome discovery
overwrites the selected executable, and the headless-shell's Windows child
processes previously opened visible consoles (`tests/test_headless_shell.py`).

## Validate a running API before completing an update

Menu/periodic WPPConnect updates build into `api_staging` in both foreground
and background modes. Keep `api_old` after the swap: `wpp_update_validation`
probes `/healthz` and `/winzapp/identity` off the UI thread, checking the
expected Node instance/PID when available. A listening TCP port alone never
completes the update. On failure, stop the new Node, wait for the profile and
port to be released, restore the retained tree and carry its token/log/env
state back. Only a healthy new API permits deleting `api_old`. A restored
previous API reports the attempted update as failed, not successful.

Without space for both builds, refuse before stopping the installed API;
the old destructive foreground fallback is gone. During an update, startup
timeouts return failure instead of exiting WinZapp before rollback can run.

An open-window restart queues the Node spawn and returns immediately. Wait
for it on the validation worker with the normal 300-second startup budget,
then capture the new PID and run the short HTTP health/identity check.
Starting the 15-second HTTP deadline at the queued spawn caused successful
builds to roll back while Node was still importing modules. Observed on
2026-10-10: the corrected checkout loaded those modules in 16.47 seconds,
validated server 2.10.45 and reconnected. Compiler output must also retain
stdout alongside stderr: otherwise TypeScript failures lose their details.

Installation/update failures use `ui/dialogs/error_details.py`: one resizable
report with a native read-only multiline edit, initially focused, and a Close
button (Escape). Keep the complete diagnostics selectable/copyable. Staged
foreground setup retains `_error_details` for the update caller instead of
opening a second report; background callbacks must forward their `details`.
Startup/health/rollback failures retain their stage and exception details too.

`core/api_dependencies.py` prepares end-user npm manifests. A precompiled
API omits development dependencies, promoting Babel runtime helpers,
declared storage peers and runtime imports before that omission. For the
known TypeScript/Babel build, keep compiler packages, configured Babel
modules, type packages and packages imported by source. Unknown build/Babel
configurations or lifecycle/generation scripts retain all development tools.
Remove only the root Husky prepare hook; dependency lifecycle scripts remain
enabled so native libraries can install. This does not change developer setup.

For that known build without custom hooks, use `tsc --noEmit`: Babel's next
step deletes `dist`, so declarations written there were immediately discarded.
Keep type checking and the original build for unknown scripts or generation
hooks. End-user npm installs use `--prefer-offline` to reuse cached metadata;
missing packages still come from the registry. Never force `--offline`.
If npm answers ETARGET/notarget (cached metadata older than a version upstream
now asks for), `_run_setup` retries once with `--prefer-online`.

After API validation, release `_wpp_updating` before starting reconnection
workers. HTTP connection probes are serialized per account and suppressed
during installation; an accepted auto-start has a bounded 60-second pending
window because create() may still report CLOSED while waiting for login.
Post-update offline announcements have a separate 90-second grace, cleared
on real WhatsApp connection. It keeps sending paused without blocking the
start command, and expires so a genuine failure cannot remain silent forever.

Installation instrumentation uses `[api-timing]` start/end records and a
monotonic duration. Time API startup, HTTP validation and previous-tree cleanup
separately. The healthy predecessor is renamed to a unique stale path before
the callback; a worker cleans it after WhatsApp reconnects (a 90-second cap),
so another update cannot reuse a path still being deleted. Shutdown leaves it
for the existing startup sweep. npm subprocess environments enable
timing JSON and retain 50 logs, including nested TypeScript/Babel scripts.
No command arguments or tokens are logged by the timing helpers. See
`docs/reference/test-api-update.md` for collecting a manual run.

Node receives a child-only `NODE_COMPILE_CACHE` under `data/global`, outside
the replaced API. Respect explicit user configuration and tolerate cache
creation failure. Flush the cache after the server starts because force-kill
shutdown does not guarantee Node's exit flush. The first load may cost more;
measure later starts and reinstallations before claiming a speedup.
`[node-startup]` logs Puppeteer, config, server imports, catalogue resolution,
initialization and cache flush. S3 SDK/bucket helper imports live inside the
upload branch in the patched functions.ts, leaving S3-enabled behavior intact.

> Everything that keeps alpha and stable users from being stranded, and why pid liveness needs a create time.
>
> Moved verbatim out of `CLAUDE.md` so it is read when the area is touched, not on every session. Keep it here: this is measured history, not a summary.

## Auto-updater (`client/updater.py`)
`UpdateChecker` polls the GitHub Releases API (repo from `config.GITHUB_REPO`), compares `version.__version__` against the latest tag, and on acceptance downloads the release's ZIP asset, extracts it, and hands off to a generated `.bat` script that waits for the current process to exit, kills WinZapp's own Node if it is still listening on the API port, copies files over the install dir, and relaunches — because Windows won't let a running process overwrite its own files. See "The installer's kill and relaunch" at the end of this file.

**Two release channels.** Stable releases are cut by hand; alpha builds are published automatically by `.github/workflows/alpha-release.yml` for every commit that lands on `main` (a direct push or a PR merge). Both live in the same GitHub Releases list, and the *only* thing separating them is the literal word **"alpha"** in the tag and release name — that's what `is_alpha_release()` matches on, and why the workflow bakes it into both. Deliberately *not* keyed on the API's `prerelease` flag, which `prerelease-test.yml`'s unrelated `-pre` builds also set (those are a third thing: not alpha, and not parseable as a version, so neither channel ever selects them).

`select_release()` picks the newest *eligible* release: alphas are skipped unless the user ticked **Configurações > Geral > "Verificar atualizações alpha"** (`general.alpha_updates_enabled`, install-wide via `app_settings.py`, off by default); drafts, unparseable tags, and releases with no ZIP asset always are. It compares parsed versions rather than trusting the API's date order — the list interleaves both channels, so the newest stable is routinely several entries down.

**Alpha version scheme.** An alpha is derived from the *current stable version* (`client/version.py`, kept current by `release-published.yml`'s `bump-version` job) with the commit count in the fourth component: base `0.25.0.0beta` + `git rev-list --count HEAD` → **`0.25.0.2154alpha`**. Given `_PRE_ORDER`'s alpha < beta < final, that yields `0.25.0.0beta` < `0.25.0.2154alpha` < `0.25.0.2155alpha` < `0.26.0.0beta` — an alpha outranks the stable it was cut from, alphas advance among themselves, and the *next* stable outranks every alpha, so an alpha user is pulled back onto the stable line instead of being stranded. The commit count (not a timestamp) makes it strictly monotonic, unique per commit, and immune to two commits landing in the same minute. **A date-shaped version here would be catastrophic**: `2026.x` outranks every `0.x` stable release, so once one shipped no future stable release could ever be offered again. **The one rule this relies on**: the next stable release must bump the patch component or higher (`0.25.0.x` → `0.25.1.0`/`0.26.0.0`), never just the fourth — `release.yml`'s "Reject a stable tag that alphas have already passed" step enforces that rather than trusting it, failing the `test` job before anything is built, and naming the exact tag to use instead.

Only `major.minor.patch` is read off the base version, so **none of this changes when the stable line eventually drops the `beta` suffix** and ships plain `a.b.c.d`: base `1.0.0.0` + N → `1.0.0.<N>alpha`, still newer than `1.0.0.0`, still older than `1.1.0.0` (`parse_version()` treats the suffix as optional and compares the numeric components first). The one transition that does *not* work is declaring stability by deleting the suffix while keeping the numbers (`0.25.0.0beta` → `0.25.0.0`): that leaves the numbers unchanged, so alphas published since still outrank it and every alpha user is stranded. Advance the numbers instead (`0.26.0.0`, `1.0.0.0`) — the guard catches it either way. Same reason a hotfix that only bumps the fourth component (`1.0.0.1`) is refused while alphas are out.

**Why the updater reads two endpoints.** `UpdateChecker._fetch_releases()` merges `/releases?per_page=100` *and* `/releases/latest`. The listing is the only place alphas appear but it is paged; alphas land far more often than stable releases, so given enough of them the newest stable falls off the first page and stable-channel users would silently stop being offered anything. `/releases/latest` is defined by GitHub as the newest non-draft non-prerelease, so it always resolves to the current stable no matter how many alphas exist — which is also why alphas are published with `prerelease: true`. Either request failing alone is survivable; only a total failure propagates.

`alpha-release.yml` is a deliberate near-verbatim copy of `release.yml`'s build pipeline (**keep them in sync**; only the blocks marked ALPHA-SPECIFIC differ). It has no `bump-version` job — committing an alpha version into `main`'s `version.py` would both corrupt the stable version line and push to `main`, which is this workflow's own trigger. A new commit **cancels** an in-flight alpha build (`concurrency: cancel-in-progress`), since a build superseded before it finished is of no use; to keep a cancellation from leaving a half-uploaded release behind, the release is created as a **draft** and published only once every asset is up. That last step patches the release **by its numeric id** (`steps.draft_release.outputs.id`), never by tag: a draft has no tag yet — GitHub creates it on publish — so anything resolving a release by tag name (`gh release edit <tag>`, `GET /releases/tags/<tag>`) simply does not find a draft. Alpha build #1 failed exactly there, compiling and uploading fine and then stranding the release as an invisible draft. Because cancellation is routine, a sweep step deletes leftover alpha drafts before creating each run's own.

**One prompt per machine, not one per account.** `try_begin_update()` guards the *install*; nothing guarded the *prompt*, and the two are different moments. Every account runs its own `UpdateChecker` in its own process, so a release newer than the running build was found N times and asked about N times — two accounts open meant two "a new version is available" windows for one update. Only one of them could ever have installed it (the second's `try_begin_update()` refuses while the first holds a live runtime lease), so the duplicates were never a second chance at anything. `update_coord.try_claim_update_prompt()` / `release_update_prompt()` claim it the same way the install is claimed — same `updater_lock`, same atomic write, same `(pid, create_time)` liveness so a crashed holder is recovered rather than blocking every account forever — and `UpdateChecker` releases on every path that leaves the app running. **It deliberately fails in the opposite direction to `update_state.json`**: a corrupt or unreadable state file blocks an install, because installing twice corrupts the installation, but a corrupt prompt claim must *not* block the prompt — the worst it guards against is a second dialog, while treating it as held would silently stop offering updates on every account with nothing to show the user why. The claim is held across the install rather than released on "Yes": the owning process exits into the batch installer, and a dead owner is recovered on the next check for free.

**"A dead owner is recovered" only holds if liveness can tell a reused pid apart, and for a long time it could not.** psutil is not a dependency, so every installed WinZapp takes `update_coord._default_proc_create_time()`'s ctypes fallback, which used to return the `0.0` "alive, create_time unknown" sentinel for every live process — so every lease and claim was recorded with `create_time` 0.0 (all 30 leases of a source install carried `_0.0_` in their names), and `lease_alive()` accepts 0.0 for *any* process on that pid. Windows reuses pids quickly (on another install: svchost, RuntimeBroker, msedgewebview2 and a Dell audio service sitting on pids of long-gone WinZapps), so the claim left behind by an accepted update read as alive for as long as some unrelated process held the old pid, and "check for updates" told people with a single account that another account's dialog was open. The fallback now reads the real creation time with `GetProcessTimes`, and the prompt claim uses `prompt_owner_alive()`, which holds only on a positive `(pid, create_time)` match — an unknown or 0.0 value never holds a prompt, matching `_read_prompt()`'s fail-open. `lease_alive()` deliberately keeps failing closed for install leases; do not "unify" the two.

`release.yml`'s ordering guard lives in `.github/scripts/check_stable_release_ordering.py` (tested by `tests/test_release_ordering_guard.py`) rather than inline in the workflow, because a wrong answer is expensive both ways: a false negative strands alpha users, a false positive blocks a good release. **Exception: a maintenance release of a line `main` has left.** When `origin/main:client/version.py` is on a higher `major.minor.patch` than the stable tag (2.0.0.0 cut from `release/v2.0.0.0` while `main` is on 2.1.0.0), the guard passes with an INFO line (`stable_lags_main()`): the alphas outrank that stable on purpose, so an alpha user is never pulled onto a build that lacks what their alpha has. A stable on the line `main` is still on stays blocked, and an unreadable `main` version fails closed (blocks). `release-published.yml` applies the same rule (step `lag`) and then skips both the `version.py` bump and the alpha dispatch, otherwise `main` would be reset to the old base and every new alpha would sort below the alphas already out. The workflow re-implements the comparison in a small heredoc, so change both together. Note it only compares against alpha tags `parse_version()` accepts — the repo carries historical alpha tags predating this channel, one of which (`v0.3.4.0alpha1`, trailing digit) is unparseable, and `is_newer()` returns False for an unparseable operand, so counting those would have rejected every future stable release forever.

**Stable releases can leave `main` ahead of what they ship.** (When the tag's `major.minor.patch` is below `main`'s, `bump-version` is skipped entirely — see the exception in the guard paragraph above — and the rest of this paragraph describes the case where it runs.) Stable is cut from a `release/x.x.x.x` branch, not necessarily `main` HEAD — `main` keeps taking every commit and gets its own alpha per push, so at release time `main` (and its most recent alpha) can already hold commits/features this stable release deliberately doesn't include. `bump-version`'s commit still overwrites `main`'s `client/version.py` with the new stable's number regardless, which on its own would be a problem: `select_release()` compares by parsed number only, so the instant that commit lands, alpha-channel users would be offered this stable — numerically newer, but potentially *behind* the alpha they're already running — and silently lose whatever `main` had that this release didn't. To close that, `bump-version`'s last step (`release-published.yml`) dispatches `alpha-release.yml` (its `workflow_dispatch` trigger) right after the version.py commit succeeds, building current `main` — which still has everything the previous alpha had — under the just-bumped base. That produces `X.Y.Z.<commit count>alpha` with a nonzero fourth component, so it outranks the stable's own `X.Y.Z.0` immediately: every stable release is paired with a same-numbered alpha the moment it ships, and alpha users never get pulled backwards in content, only in the visible major.minor.patch. The trigger is skipped if the version.py commit itself didn't land (`if: steps.commit_version.outcome == 'success'`) — dispatching off an unbumped base would accomplish nothing.

## Update prompts never ran for a WinZapp started with Windows, and the WPPConnect one never offered anything newer than the minimum (2026-10-03)

**Symptom.** The maintainer runs WinZapp only through autostart (`--background`) and lost WPPConnect Server updates several times; nobody who starts it with Windows was ever offered a WinZapp update either.

**Root cause, three parts.**
1. `MainWindow.__init__` (`client/main.py`) scheduled `_start_update_checker` (15 s) and `_start_wpp_update_checker` (90 s) only `if not self.background_mode`, and `_start_wpp_update_checker` (`client/main_window/updates.py`) returned early in background mode as well. The schedule is built once at construction; `restore_window()` clears `background_mode` later but nothing scheduled the checkers afterwards, so the prompts never existed for that user.
2. The periodic WPPConnect check compared the installed server only with the tag in `client/wpp_minimum_version.txt` (`_homologated_or_latest_tag`, justified as "prompting onto every release is how 2.3.2 reached people"). That made the minimum the ceiling of the offer: nobody was told about a newer server until a WinZapp release raised the file. Together with (1) the user heard about neither.
3. A dialog owned by a hidden main window opens without Windows activating it (foreground-stealing prevention), so a prompt that did run would have sat unseen and without keyboard focus.

**Fix.**
- Both checkers are scheduled regardless of `background_mode`. The `updates_enabled` setting, the pairing / ui-ready gate (`wpp_update_may_run_now`, `UpdateChecker._main_window_ready`) and the one-prompt-per-machine claim are untouched. The failed-update notice and the 2.0 reinstall notice stay foreground-only.
- `core/dialog_foreground.py`: with the main window hidden (`_window_hidden` or `background_mode`), the update prompt, the progress dialogs and the message boxes are raised with the same input-queue attach `restore_window()` uses, without un-hiding the main window. The prompt is spoken through `speak_output` BEFORE the raise, whatever the raise achieves: whether a dialog really came to the front cannot be known (a native `wx.MessageDialog` reports `IsShown()` False and has no wx handle, which is why the hidden-window message boxes are a real `wx.Dialog`, `_HiddenParentMessage`, with the native box's buttons, default and Escape). With the window visible nothing changes.
- One WPPConnect prompt per machine: `update_coord.try_claim_update_prompt(..., name=WPP_PROMPT_FILE)`, same lock, liveness and fail-open as the WinZapp claim, held across the accepted update and released on No, on a refused start, when the update ends, and in `WppUpdateChecker.stop()`. Because the reinstall deletes the shared `api/` in place, the checker looks at `wa_version_refresh.other_accounts_node_alive` BEFORE claiming or prompting and stays silent (long retry) while another account's Node is alive; `_update_wpp_server` refuses too, as defence in depth, and a refusal after a Yes remembers the release (`_declined_tag`) so it does not loop. For this check an unreadable lease file is logged and ignored (`ignore_corrupt=True`) instead of silencing the prompt for good; a failing lookup still counts as alive.
- A failed or cancelled install records its tag in `_declined_tag` (session only; persisting per install would need a settings key and an expiry rule), so a broken release is not re-attempted every 12 h; a newer release or a required update still prompts. A user cancel (`ApiSetupDialog._cancelled`) shows no "could not update" box but still restores a missing server. Hidden-window message boxes label their buttons from I18n (`button_yes`, `button_no`, `ok`), not wx's English stock labels, and cut very long text for display (full text logged).
- `UpdateChecker._schedule_retry` cancels the previous timer and clears `_force`, so a forced check that failed cannot make a later automatic retry answer "no update available".
- `_update_wpp_server` announces progress and completion in a background start too (the user just accepted); the `restore_window` after it keeps its `not background_mode` guard so the window is not un-hidden gratuitously.
- `UpdateChecker._check_once` retries a failed fetch after 30 s, 1, 2, 5 and 10 minutes before falling back to the 3 h interval (the network is often not up 15 s after a Windows login).
- `wpp_update_target(installed, minimum, latest)` (`client/updater.py`): the minimum is the **floor**, GitHub's latest the target. Installed below the minimum is the *required* kind (the mandatory gate's wording, `api_update_outdated_*`, never remembered as declined); installed at or above it but behind latest is the *recommended* kind (`wpp_update_available_msg`, Yes/No with No as default, a No is remembered for the session). GitHub unreachable offers the minimum only (a raised minimum is still offered offline) and retries in 15 min; a latest older than the minimum offers the minimum, never going backwards; unparseable versions never prompt. `ensure_wpp_version()` (the startup gate with continue/update/quit) still skips background starts, which the required kind of the periodic check now covers.

**Reversed on purpose:** the old rule "the periodic check compares only with the homologated tag" is gone.

**Known risk, mitigated.** A server update never moves the pinned wppconnect/wa-js pair (`_PATCHED_DEPENDENCY_KEYS`), but `ApiSetupDialog._run_setup` overwrites whole upstream files (`_CUSTOM_SRC_FILES`) and wipes `api/` in place before downloading-then-building, with no backup. If a newer release no longer compiles against those files, `npm run build` fails after the old `dist/` is gone; `ensure_wpp_running()` then silently skipped because `dist/server.js` was missing and the server stayed down until the next WinZapp start. Now `_finish_error` logs the failure, and `_update_wpp_server` reinstalls the bundled minimum tag once, in the same session, when the failed install left no `dist/server.js` and the minimum is a different tag (`should_roll_back`); a failed rollback is logged and leaves the old next-start recovery. Not exercised against a real failing build.

## Updates downloaded in the background (2026-10-04)

**Why.** An accepted WinZapp update opened `UpdateProgressDialog` at once, and an accepted WPPConnect Server update stopped the server and opened `ApiSetupDialog`: two modal windows that kept the user out of the app for the whole download, and for the server update kept WinZapp offline through `npm install` and the build as well — minutes. `general.background_update_downloads` (Settings > General, install-wide through `app_settings.py`, off by default, only an explicit `True` counts) moves the long part off the screen. The prompts are unchanged: the user still accepts each update.

**WinZapp (`client/update_background.py`, `client/update_package.py`).** Download, signature check and extraction were lines inside `UpdateProgressDialog._worker()`; they are `download_update_package()` now, and **both paths call that one function**, so the bytes handed to the installer are verified identically (`docs/traps/release-integrity.md`). With the option on, `_do_install()` starts it on a thread with no window and returns; when the package is ready, `_install_downloaded_update()` waits for `may_interrupt_now()` — main window up, no pairing, **no voice or video call** — shows one OK message box saying WinZapp is about to close, and calls `_do_install(extracted_dir=...)`, where the dialog runs only its install phase (other accounts asked to quit, install slot, batch installer). The per-machine prompt claim is held across the background download exactly as it is across the dialog, and handed back on every path that leaves the app running. Only quitting cancels a background download.

Three rules found in review, each with a test. **One at a time**: with the option on the main window is free during the download, so "check for updates" and "reinstall" stay within reach; `_background_version` is set for the whole flow, and while it is set `_check_once()` opens no prompt (the claim is re-entrant for its own process, so a second dialog would open, and its No would release the claim under the first download) and `_do_install()` starts no second download — both say the update is already downloading. **Every end reschedules**: `_end_background_update()` clears the flag, releases the prompt and calls `_schedule_retry()`, because `_shutting_down` goes back to False when another program cancels a Windows shutdown, and a WinZapp started with Windows would otherwise never hear of the update again. **`may_interrupt_now()` also waits** for a ringing call, for a voice message being recorded (the notice has one button), and for `_wpp_updating` — quitting over an in-place server reinstall would leave `api/` half-built with npm running. `discard_package()` removes the whole `winzapp_ext_*` directory, also when the package is the single folder inside it.

**WPPConnect Server (`client/main_window/wpp_background_update.py`, `client/core/api_staging.py`).** The new server is built in `api_staging/`, a sibling of `api/`, while the installed one keeps running: `ApiSetupDialog(api_dir=..., on_done=...)` runs the same `_run_setup()` with the dialog never shown and no message box. Only then does `_update_wpp_server(staged_dir=...)` run its usual sequence — stop, wait for the profile, install, restart — with the install step being `swap_in_staged_api()`: `api` → `api_old`, `api_staging` → `api`, the per-install state (`CARRIED_OVER`: tokens, log, `.env`) moved across, `api_old` deleted on a thread. Rules it keeps:

- **A built tree is relocatable.** Nothing in `dist/` or `node_modules/` records the absolute path it was built in (grepped over a real 920 MB install before relying on it), and the release ZIP's `dist/` has always been built on CI and run elsewhere. If a future dependency bakes its path in, this is the assumption that breaks.
- **The swap never leaves no server.** An unbuilt staging directory is refused; if the new tree cannot be moved in, the old one is renamed back; a failure is `SwapError` with `api/` as it was. The one exception is a double failure (the new tree cannot move in AND the old one cannot move back): it is logged with the old server left in `api_old`, and the in-place rollback is the net. A failed *build* never touched the installed server at all, which the in-place update cannot say (it wipes `api/` first, hence `should_roll_back`).
- **`_wpp_updating` is NOT set while building.** It tells the health checker the outage is deliberate; during the build there is no outage. `_wpp_staging` marks the build instead, and both refuse a second update (the checks in `_update_wpp_server`, which says so out loud during a build, and in `WppUpdateChecker._prompt_update`).
- **Other accounts are re-checked at the swap**, not only at the prompt: `_update_wpp_server(staged_dir=...)` goes through the same `other_accounts_node_alive` refusal, and a refusal drops the build.
- **Room for two servers.** `has_room_for_staging()` wants 3 GB free; without it the update is done in place, silently, as before (`_update_wpp_server(in_place=True)`). It is measured **after** the leftovers of an interrupted build are removed — they are over a gigabyte and would count against the update that removes them — and both happen on a thread. The in-place path sweeps those leftovers too, or with the option off nothing ever would.
- **Quitting cancels the build** (`cancel_wpp_background_update()` from the exit path), or its npm and Node would outlive the app. `_perform_shutdown()` runs on the shutdown thread, so it goes through `ApiSetupDialog.cancel_background()`: the kill happens right there, and everything wx (timer, report, `Destroy`) is handed to the main thread, where it may never run on a real exit. Calling `_on_cancel()` from that thread stopped a wx timer BEFORE the kill; had wx refused, npm survived. What an interrupted build or swap left on disk is removed when the next update starts — the moment this process holds the machine's WPPConnect update, so no other account can be building.

**Superseded by the validation flow above.** The first implementation deleted
`api_old` immediately after swapping and fell back to an in-place reinstall.
The updater now keeps it through HTTP validation and restores it on failure;
the foreground path builds beside the old API too. The reduced dependency
set was compiled with TypeScript and Babel in an isolated directory using the
installed dependency graph (56 to 23 direct development packages for server
2.10.37). No real session update was run; rollback and health orchestration
are covered by temporary-directory and stub tests.

## A WinZapp update put back an older WPPConnect Server (2026-10-06)

**Symptom.** With background downloads on, a user updated the server to 2.10.36 and then 2.10.37; each time WinZapp said it was done, and later `api/package.json` was at 2.10.30 again and the same update was offered again. The logs showed the update had worked: the swap succeeded and the restarted server logged `WPPConnect-Server version: 2.10.36`. The server logged 2.10.30 again right after the next alpha of WinZapp was installed.

**Cause.** The release ZIP carries `api/package.json` and `api/dist/`, built from `wpp_minimum_version.txt`, and the installer's `xcopy /E /Y` writes them over the installed `api/`. Since the periodic check offers releases newer than the minimum, and Force Reinstall goes to the newest one, a server newer than the bundled one is a normal state. Each WinZapp update reset it, and on the alpha channel that happens several times a day. `node_modules/` is not in the ZIP, so the tree it left mixed the bundled `dist/` and `package.json` with the newer server's `node_modules/`.

**Fix (`client/core/update_keeps_server.py`, called from `_run_batch_installer`).** Before the script is written, when the installed `package.json` version is strictly newer than the release's, the extracted release's `api/package.json` is replaced with the installed one, with the `_PATCHED_DEPENDENCY_KEYS` pins taken from the release, plus every release dependency the installed file lacks. The running build's key list is older than the release, and a runtime dependency the release adds (prom-client, zod, qrcode and ffmpeg each arrived that way) is required by its `dist/`; dropping it would leave a server that does not start. The version stays, so nothing re-offers the update. The dependency ranges stay too, and they match the `node_modules/` that is actually installed. A pin the new build moved still trips `library_drifts()`. Anything that cannot be read or compared leaves the release's file as it was.

**`dist/` still comes from the release, on purpose.** It is the only way WinZapp's own patched controllers (`api_patches/src/`) reach an install, and the WinZapp being installed may call routes only its own `dist/` serves. Skipping it would leave the newer server running the previous build's patches. The cost is that upstream source changes between the bundled tag and the installed one run as the bundled tag's until the next server update. Every upstream release from 2.10.27 to 2.10.37 changed dependencies only (they live in `node_modules/`), so today that cost is nothing. If upstream starts shipping source changes WinZapp does not override, the follow-up is to rebuild `dist/` for the installed tag after a WinZapp update (the background-update machinery can do it), not to stop copying `dist/`.

**Still open.** When a release moves a pin, the reinstall `library_drifts()` offers rebuilds at the bundled tag (`ensure_wpp_version`), so accepting it moves a newer server back to the bundled version, and the periodic check offers the newer one again. That path is rarer, and it is not a regression, because before this fix the xcopy had already downgraded those users. Fixing it means rebuilding at `max(installed, minimum)`.

## The installer's kill and relaunch, and WinZapp's own npm (2026-10-09)

**Symptom.** Reported from Windows: after a WinZapp update, WinZapp worked and the user's own npm, outside WinZapp, did not.

**Cause.** With an install folder the user cannot write to, `_run_batch_installer()` runs the script through UAC, and the script's `start "" WinZapp.exe` started WinZapp elevated — so its Node and every npm under it ran as Administrator, writing into the user's shared npm cache. The same script freed the port with `netstat | findstr :6300` (a substring: 63000-63009 matched) and `findstr :5433`, a PostgreSQL nothing has started since WinZapp moved to WPPConnect Server, killing whatever PID came out.

**Fix.**
- `core/npm_environment.py`: every npm/npx WinZapp runs (`setup_api.py`, `ApiSetupDialog._run_setup`, the missing-package repair, the browser download, the npm probe, and Node itself for start.js's npx fallback) gets `npm_config_cache` under `global_dir("npm")`, an empty userconfig of its own and no update notifier. The user's npmrc settings are forwarded as `npm_config_*` (npm exports them all to install scripts — a puppeteer mirror, node-gyp's `python`) only where that is exactly what npm reading the file did: keys of `[a-z0-9-]` only (npm reads `strict_ssl=` in a file literally, as nothing, but would read `npm_config_strict_ssl` as `strict-ssl`), not keys the API's own `.npmrc` sets (it outranks the user's file, but not the environment), never cache/userconfig/update-notifier/logs-dir. Anything else — credentials, per-registry or scoped settings, `cert`/`key`, arrays, sections, empty values, other key shapes, control characters, or a file that is not clean UTF-8 (a UTF-16 file from PowerShell 5.1's `echo > ~/.npmrc`) — keeps the user's file as npm's userconfig, as before. A NUL byte in a child environment once made every npm call and the Node launch raise; the Node launch's `except` now logs. The parser follows npm's own `ini` (5.0.0, npm 10.9.7) exactly. Never build an npm environment by hand: `tests/test_npm_environment.py` checks every spawn.
- `update_relaunch.py`: the elevated script leaves the relaunch to a waiter the non-elevated WinZapp starts. The waiter's first action creates the handoff marker and WinZapp waits for it before exiting, so the marker exists exactly when a waiter owns the relaunch; the script relaunches by itself when it is missing. When the marker is there, the script writes `<marker>.done` after its relaunch decision on every exit path; the waiter stops on that, on the script's cmd.exe disappearing, or after 1,800 checks — at least 30 minutes, since a batch script has no usable clock (keeping the marker, so a script still running does not start a second copy). A `%TEMP%` path that stays non-ASCII even in 8.3 form means no waiter: the script relaunches as before rather than the update failing. Both relaunch paths now start WinZapp with `relaunch_environment()`: no `_PYI_*` bootloader state, `PYINSTALLER_RESET_ENVIRONMENT=1`, none of the variables WinZapp sets for its Node — what a fresh start has. Not run on Windows when written.
- `update_node_kill.py`: WinZapp decides, while still running, which PIDs listen on exactly the API port with WinZapp's own `node.exe` (same file), and the script kills each only if it is still a `node.exe` on exactly that port. The PID is read as the last field on both sides, because netstat translates the state column and a translation can be two words. The 5433 kill is gone. Because `findstr LISTENING` most likely never matched on translated Windows, on those systems the Node kill is in effect new behaviour.

**Known limit: a WinZapp that is already elevated stays elevated.** If WinZapp is running as Administrator when it updates — run that way on purpose, or relaunched elevated by an older build's installer and not restarted since — it can write the install folder, `_needs_admin()` is False, the script runs at WinZapp's level and its plain relaunch inherits it. That is logged (`WinZapp is running elevated, so the update installs and relaunches it elevated`) and deliberately not changed: some people run WinZapp as administrator on purpose, and nothing tells the two apart. An older build's relaunch passes no argument, sets no variable and leaves no file, and the parent process (a cmd.exe that has exited) is not a reliable signal either. The chain ends at the next normal start (a shortcut or autostart starts it non-elevated); until then its npm still keeps out of the user's cache.

Only updates *from* a build with this fix use the new script: the script is written by the running, older WinZapp.
