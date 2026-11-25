# WinZapp for macOS

A native macOS build of WinZapp, made for VoiceOver users. Everything
Mac-specific lives in this folder; WinZapp's Windows code is unchanged and
the Windows build is unaffected.

Maintained by Rocco Fiorentino (@rfiorentino1). Windows changes never need
to be tested on a Mac; see "What Windows changes can break" below.

## How it works

`winzapp_mac/` is a compatibility layer installed before WinZapp's
`client/main.py` runs (by `launcher.py` in development, by a PyInstaller
runtime hook in the app). It swaps Windows-only pieces for Mac equivalents
and adapts WinZapp's UI to how Mac apps and VoiceOver behave:

| Module | What it does on the Mac |
|---|---|
| `listctrl.py`, `native_rows.py` | `wx.ListCtrl` is invisible to VoiceOver on macOS (wxGenericListCtrl, custom-drawn). Every list becomes a native table; rows answer VO-Space (activate, like Enter), VO-Shift-M (the row's context menu) and offer the context-menu items as VoiceOver actions (VO-Command-Space), read from WinZapp's own menu handlers so new items appear automatically. wx rebuilds the whole native table for every row it writes, inserts or deletes (`src/osx/cocoa/listbox.mm`, "TODO optimize"), which threw away the cell VoiceOver was on; those writes now change only that row. |
| `accessibility_mac.py` | `wx.Accessible` does nothing on macOS; its names, descriptions and shortcuts are bridged to NSAccessibility, and unlabelled fields take the label before them, as NVDA does. |
| `speech.py` | Speech goes to VoiceOver as NSAccessibility announcements (no AppleScript setting needed); the system voice is the fallback instead of SAPI. |
| `vo_focus_mac.py` | After a chat opens, VoiceOver is pointed at the message field once the chat list stops changing, and announcements made meanwhile (pinned messages) are spoken after it. |
| `layout_mac.py` | The messages list keeps a few rows of height when voice playback, recording, a quote or the composer show extra controls: the room comes from the Chats list, which keeps a few rows too. A zero-height list disappears from VoiceOver. |
| `keymap_mac.py` | Shortcuts follow one rule — Ctrl becomes Command, Alt becomes Command-Option, Ctrl+Alt becomes Control-Command — with exceptions where the rule would hit a macOS command (e.g. Exit is Command-Q, archive is Control-Command-A). Menus, hints and the shortcuts help speak the Mac keys. Option+letter keeps typing characters. |
| `menubar_mac.py` | Settings, About and Quit in the application menu, and in release builds Check for updates under About; Chats and Messages menus mirroring the selected row's context menu; the Windows self-updater is off. |
| `notify_mac.py` | Native notifications with reply and quick reactions as actions; Focus modes apply. |
| `launch_mac.py` | Ends wxWidgets 3.2's launch wait as soon as macOS finishes launching (it otherwise idles inside `wx.App()` until an unrelated event arrives, the fix wxWidgets 3.3.2 made) and logs `[STARTUP_TIMING] wx.App:` lines for each phase of `wx.App()`. |
| `lifecycle_mac.py` | Closing the window keeps WinZapp running (it still receives messages); the Dock icon brings it back; quitting leaves the Dock immediately. |
| `server_mac.py` | Stops the Node server on quit (the Mac equivalent of `taskkill /F /T`). |
| `paths_mac.py` | Data and the paired session live in `~/Library/Application Support/WinZapp`; the app installs its bundled server runtime there at launch, after an update in a single APFS clone; the browser WhatsApp runs in (`api/.cache`) is carried over, minus versions the new runtime no longer pins, and the old runtime is deleted in the background. |
| `sound_mac.py`, `audio_mac.py` | Universal BASS dylibs and the Opus plugin; bundled ffmpeg; the microphone recovers when CoreAudio restarts. |
| `spell_mac.py`, `camera_mac.py`, `hotkey_mac.py`, `platform_mac.py` | Spell checking with the Mac's dictionaries, the camera through AVFoundation, the global hotkey through Carbon's RegisterEventHotKey (no Accessibility permission), Show in Finder, macOS region and language. |
| `strings_mac.py` | Mac wording: a string that describes Windows has a Mac variant beside it in `client/languages` (`<key>_macos`), used in its place on the Mac. |
| `focus_mac.py` | WinZapp's quiet-hours gate follows macOS Focus (Developer ID builds with the Communication Notifications entitlement). |
| `coord_mac.py` | Which recorded account process is still alive: the app ships psutil (real creation times), and a lease whose pid is now another program counts as gone, so leases left by past quits no longer block updates or start-up. |
| `updater_mac.py` | Signed release builds update from the macOS release feed named in their Info.plist; without one the updater is off. |
| `sparkle_mac.py`, `sparkle_policy.py`, `update_accounts.py`, `install_on_quit.py` | Release builds with a Sparkle key update through Sparkle 2's standard windows; they download in the background and install when WinZapp quits; the official tag is checked before Sparkle offers or downloads a release, and the update window says what changed since the running version (`sparkle_notes.py`; see "Updates through Sparkle"). `sparkle_policy.py`, `update_accounts.py` (the other accounts, the update slot, the stored verdicts) and `install_on_quit.py` (the update armed for quit, ending all accounts with it) are pure Python, tested on every platform. |
| `version_mac.py` | A release build runs as its Info.plist release tag (About, update checks), not the unstamped `client/version.py`. |

## Building

Needs Python 3.13, Homebrew's PortAudio (`brew install portaudio`) and Go
(`brew install go`, at least the version neonize's `go.mod` asks).

    python3 macos/build_app.py --zip

builds `macos/dist/WinZapp.app` for the Mac it runs on (Apple Silicon or
Intel) and `WinZapp-macOS-<arch>.zip`. It installs the Python
dependencies into `.pydeps/`, downloads the pinned Node.js and ffmpeg
(SHA-256 checked), runs `setup_api.py`, and bundles the WPPConnect server
and its headless Chrome inside the app. The server's `node_modules` keeps
only what the server loads at run time (`macos/runtime_prune.py`: the
packages a Windows end-user install keeps, cut down to what `dist/` and
`start.js` reach through `package-lock.json`; the build stops if any of
their imports no longer resolves), about a third of the files: fewer to
unpack when an update installs, and to copy at the first launch after it. `.github/workflows/build-macos.yml`
does this for both architectures on pull requests that touch the Mac build.

The app also carries the Neonize engine: `macos/bridge_bundle.py` freezes the
Neonize bridge (`neonize_bridge/`, with neonize's
`neonize-darwin-<arch>.dylib` compiled from source, hence Go) into
`Contents/Resources/neonize_bridge`, and the signing gives it the same
entitlements as Node. The freeze runs in a child process while the app's own
steps run, so it adds little to the build's time; its output is in
`macos/build/bridge.log`. It runs from inside the app and keeps its sessions in
Application Support (`data/global/neonize_bridge/`). `--no-bridge` builds
without it (WPPConnect only, no Go needed). libmagic is not bundled: without
Homebrew's, the bridge takes mimetypes from the file signature. A release
workflow that builds the app needs `actions/setup-go` before `build_app.py`.

Development run without building: `python3 macos/launcher.py`.

Tests: `PYTHONPATH=.pydeps python3 -m pytest macos/tests -c /dev/null --rootdir macos/tests`.
They never show a window.

## What Windows changes can break

The layer never edits WinZapp's files; it replaces WinZapp functions and
methods at startup. So on the Windows side:

- **Renaming or removing** a function, class or method the layer patches or
  reads fails `tests/test_macos_layer_contract.py` in the normal `pytest`
  run, on any platform, naming the file and line in `macos/winzapp_mac` to
  update. Nothing else in `client/` is constrained.
- **A new string that mentions Windows** shows "macOS" in its place on the
  Mac. For better wording, add `<key>_macos` next to it in every locale
  (checked by the same tests as any other key).
- Everything else, including behaviour changes inside a patched method, is
  the Mac maintainer's to follow up; the Windows build never runs Mac code.

## Not yet on the Mac

- **Recording microphone + computer audio.** Windows uses WASAPI; the Mac
  needs a Core Audio process-tap recorder (macOS 14.2+), which would also
  allow a separate VoiceOver volume like the NVDA one. The button is shown
  dimmed and says so.

## Opening a downloaded build

A build without `WINZAPP_SIGN_IDENTITY` (such as the pull-request workflow's)
is ad-hoc signed, not notarized. macOS blocks a downloaded copy the first time: open it once, then allow it
in System Settings, Privacy & Security ("Open Anyway").

## Commit provenance

Mac releases are published from the maintainer's fork, so the Apple
signature proves who built an update, not that its code went through this
repository. A Mac release must therefore DECLARE an official tag and commit,
and the updater checks that this tag exists in
`gabrielhhaber/WinZapp_Python` and points at the declared commit. That is a
declaration checked against our repository. It does not prove the zip was
built from that commit; the stronger guarantee needs an attestation from a
workflow of the official repository, or a maintainer signature over the zip
hash like Windows. Code: `winzapp_mac/provenance.py` (pure standard library,
tested on any platform by `tests/test_macos_provenance.py`).

**Threat model.** The attacker controls the releases repository (its assets,
its CI secrets) and can publish any zip and any provenance there. They cannot
push a tag to the official repository without write access there, which is
the same trust the Windows releases rest on. A Mac release that cannot name
one of our tags and its commit must not install.

**The running version is the official tag the app was built from**
(Info.plist `WinZappReleaseTag`, written by `build_app.py` before signing,
next to `WinZappSourceCommit`). It is never `client/version.py`, which is
the unstamped placeholder in every tagged commit (CI stamps it only at
build), so an old genuine release would otherwise count as newer. A build without a valid
`WinZappReleaseTag` (a development build) offers and installs no update, and
the log says why. Versions compare as integers; an alpha or beta is older
than the stable of the same number; an equal tag is not newer. The same tag,
without its `v`, is the version the whole app shows and uses: `version_mac`
sets `version.__version__` from it at startup (About, the update check's
User-Agent, `UpdateChecker`'s "is it newer" check), and `build_app.py`
writes its numbers (no `alpha`/`beta`, which Apple's integer format does
not allow) as `CFBundleShortVersionString`. Without it, the unstamped `version.py` of the
tag's checkout would make `UpdateChecker` offer the running release on every
check.

**What is verified, in this order.** Any failure, or any answer that is not a
clean 200, is a refusal: nothing is installed. Verification failures now
distinguish DNS, connection, timeout, TLS/certificate, access denial, rate
limit, missing resource and server failure. The selected language explains
whether fetching the provenance file or checking the official tag failed;
diagnostics never include the exception's URL, body or certificate text.
Only transient DNS/connection/timeout/5xx failures get one retry after one
second, within the same 30-second request budget. A 403 is a rate limit only
with a quota/Retry-After hint; other 403 responses remain access denials.
A server response carrying Retry-After is reported with that wait instead
of being retried early.
429 and confirmed quota failures respect Retry-After (seconds or HTTP date)
and the primary quota reset when supplied; otherwise the wait is 60 seconds.
Further checks on that host in this process wait out that interval without
another request. This is a negative cooldown, never a cached approval.
TLS, redirects, oversized answers and provenance mismatches are not retried
or bypassed. Every request has a per-read timeout of 15 s and a total
deadline of 30 s, including its retry; each read is made in small chunks and
waits at most for what is left of the deadline, so a server trickling bytes
cannot hold a request open past it.

1. The release has `WinZapp-macOS-provenance-<arch>.json` (fetched from the
   releases repository by tag; untrusted content, at most 16 KB, https). It
   must have exactly the fields `schema` (1), `version`, `source_repo`,
   `source_commit`, `artifacts`, with `source_repo` equal to the official
   repository (pinned in the code), `source_commit` 40 lowercase hex,
   `version` a release tag (`v1.2.3.4`, optionally `alpha`/`beta`), at most 8
   artifacts of name to 64-hex sha256. Every pattern is ASCII and a full
   match (no trailing newline, no other scripts' digits); deeply nested JSON
   is a refusal, not a crash.
2. `version` equals the tag of the release being installed, is newer than the
   running tag, and `source_commit` is not the commit already running. A
   genuine provenance of an older release cannot ride on a newer tag, and an
   older release is refused as a downgrade.
3. Over HTTPS to `api.github.com/repos/gabrielhhaber/WinZapp_Python` only,
   unauthenticated, no redirects, 256 KB cap: `git/ref/tags/<tag>` exists,
   and, peeled through annotated tag objects (`git/tags/<sha>`, whose
   answer must be for the object asked), is exactly `source_commit`.
4. The downloaded zip (at most 2 GB and 30 minutes in total; the partial
   file and the temporary folder are deleted on any failure) has the sha256 the
   provenance lists, and the release's `SHA256SUMS.txt` entry.
5. As before: the app inside has the running app's Apple Team ID, passes
   `codesign --verify --deep --strict`, and `spctl` (notarized).

**Why the tag and not "the commit exists in our repo".** GitHub serves a
fork's commits through the parent's `/commits/<sha>` API, so that answers 200
for code that was never in our repository. The tag is fork-proof. A compare
against `main` would also prove "reachable from main", but costs another call
against the 60 an hour unauthenticated limit and would refuse a hotfix tag on
another branch, so it is not used. Provenance is one file per architecture
(`-arm64`, `-x86_64`) so the two build jobs never merge or overwrite a shared
file.

Sparkle builds check the tag only (steps 1 to 3, twice), not the zip's
bytes; "Updates through Sparkle" below says why that is the same guarantee
under this threat model, and what remains.

**What it does not prove.** That the zip was built from that commit. A
releaser can build a modified tree and name an honest commit. `build_app.py`
refuses a dirty tracked tree and a HEAD that is not the tag, but that runs on
the releaser's machine, and untracked files are not checked. Only a
reproducible build, or an attestation from an official-repository workflow
verified by the updater, or a maintainer signature over the zip hash, would
prove it. A compromised maintainer account is out of scope here as it is for
Windows. Verification needs GitHub's API, so it cannot run offline.

**Known limit: tag protection is only as strong as the repository's tag
rules.** The ruleset "Allow tag creation only for admins" (id 20587521)
excludes `refs/tags/v*alpha`, so any collaborator with write access can
create or move an alpha tag, and an alpha provenance is only as trustworthy as
that. Recommendation to the maintainer: cover alpha tags too, or restrict
them to the alpha workflow. This change touches no repository setting.

**How a release is produced (Rocco).** There is nothing to stamp: the tag is
the version.

1. Fetch the official tags (`git fetch https://github.com/gabrielhhaber/WinZapp_Python.git
   --tags`) and check out the maintainer's tag on a clean tree. If the
   checkout has no tags, set `WINZAPP_RELEASE_TAG=<tag>`; HEAD must still be
   that tag's commit. Do not edit `client/version.py` (a modified tracked
   file makes `build_app.py` refuse the build).
2. On each architecture:

       WINZAPP_MAC_RELEASES_REPO=rocco-labs/WinZapp_Python \
       WINZAPP_SIGN_IDENTITY=... python3 macos/build_app.py --zip

   `build_app.py` resolves the tag against the official API before building
   and stops if HEAD differs, tracked files are modified, or a release build
   has no tag. It writes `WinZappReleaseTag` and `WinZappSourceCommit` into
   Info.plist before signing, and
   `macos/dist/WinZapp-macOS-provenance-<arch>.json` next to the zip.
3. Publish a release in the fork with the same tag name as the official one,
   with `WinZapp-macOS-<arch>.zip`, `WinZapp-macOS-provenance-<arch>.json`
   and a `SHA256SUMS.txt` covering both zips, for both architectures.

An untagged build makes no provenance and does not self-update.

**What the maintainer does.** Nothing new: tag as today. The tag is the
approval.

**Optional, recommended: artifact attestations.** In the workflow that builds
the release, `actions/attest-build-provenance` (pinned by SHA, with
`id-token: write` and `attestations: write` on that job only) signs the zip's
digest with the workflow's identity, and `gh attestation verify <zip> --repo
<repo>` checks it. Run from an official-repository workflow it would show
which workflow built the zip, and a future updater could require it. It needs
no secret, but whether a release workflow exists is decided in issue #343;
nothing here adds one.

## Updates through Sparkle

Release builds whose Info.plist carries a Sparkle key update through
[Sparkle 2](https://sparkle-project.org) (2.10.0, pinned with its SHA-256 in
`build_app.py`), whose standard windows VoiceOver reads well. Help > "Check
for updates" and WinZapp's automatic check open Sparkle instead of WinZapp's
own dialog; the menu and Settings are unchanged. "Check for updates
automatically" is Sparkle's scheduled check (every 3 hours, as before),
applied as soon as it changes; "check for alpha updates" is Sparkle's
`alpha` channel. One account process runs the scheduled checks
(`update_coord` claim `sparkle_updater.json`). Nothing is downloaded before
the user chooses Install, as before.

**Language.** A Sparkle build lists WinZapp's languages in
`CFBundleLocalizations` (English, Portuguese (Brazil), Portuguese (Portugal),
Spanish, Polish, Turkish, Romanian). macOS then runs the app in the Mac's
language when it is one of those. On a Mac in one of those languages, Sparkle's
windows appear in that language (checked against Sparkle 2.10.0's own
translations), and so do the parts of WinZapp that macOS itself provides
(standard panels and text menus, system menu entries such as Services and the
Window menu's), which follow the same rule, even when WinZapp itself is set to
another language. On a Mac in English, or in a language WinZapp does not have
(German, for example), nothing changes: English, as before. System Settings >
General > Language & Region > Applications can give WinZapp its own language,
which these parts then follow. WinZapp's own text keeps following WinZapp's
language setting.

**What is checked, and what is not.** Against the threat model under "Commit
provenance" (whoever controls the releases repository controls its CI
secrets: the Developer ID certificate, the notary key, SHA256SUMS.txt, the
provenance files, and Sparkle's EdDSA key), a check of the zip's bytes adds
nothing that attacker could not also produce. So the Sparkle path checks the
one thing they cannot fake, and nothing else of its own:

1. **Before Sparkle shows or downloads a release**
   (`updater:shouldProceedWithUpdate:...`, Sparkle's veto before anything is
   shown or downloaded, for the background download too):
   `updater_mac.release_provenance()` asks the official repository that the
   release's tag exists, points at the commit its provenance declares, and is
   newer than the running tag; and the appcast item must name exactly that
   tag in both its versions, point at exactly that tag's
   `WinZapp-macOS-<arch>.zip` in the releases repository over https, offer no
   deltas, and be an application. Otherwise it is refused: silently for a
   scheduled check, in Sparkle's error window when the user asked. The answer
   needs the network and the veto is synchronous, so Sparkle first aborts
   silently and checks again once the answer is in (a second or two; an
   answer older than 10 minutes is asked again).
2. **In the background**, with "check for updates automatically" on (it
   governs Sparkle's scheduled checks and its automatic downloads alike;
   `SUAllowsAutomaticUpdates` and `SUAutomaticallyUpdate` are on), a
   scheduled check downloads a release that passed step 1 without a window
   and leaves Sparkle's installer waiting for WinZapp to quit, as in other Mac
   apps. Quitting installs it. Sparkle's next check shows it in its window as
   downloaded, with "Install and Relaunch"; WinZapp says it ("...has been
   downloaded and will be installed when you quit WinZapp. To install it now,
   choose Help > Check for updates, or switch to WinZapp.") and posts it as a
   notification. Sparkle's installer watches only one process of the app and
   installs as soon as that one ends, and every account is a process of the
   app: so while such an update waits, ending any account (Close current
   account, an account switch) ends all of them first ("Closing the other
   WinZapp accounts..."), and the ones the user did not close are opened
   again in the background once the installer is done. No second provenance
   check is possible here (the app is gone when it installs): step 1 is its
   gate.
3. **When the user chooses Install** in Sparkle's update window, the item is
   checked again (an update Sparkle resumes skips step 1), the other accounts
   quit ("Closing the other WinZapp accounts..."), and WinZapp claims
   `update_coord`'s update slot, which keeps every account from starting
   until the update is over. Only then does Sparkle download, or, for an
   update already downloaded in the background, install and relaunch. If an
   account cannot be closed, nothing is downloaded and WinZapp says so. If the
   update then does not install (the download fails or is cancelled, Sparkle
   gives up, the last check refuses), the slot is given back and the accounts
   closed for it are opened again in the background, and WinZapp says so.
4. **The download** must be that same https asset
   (`updater:willDownloadUpdate:withRequest:`); anything else is sent to a URL
   that fails.
5. **At "Ready to Install"**, after a download the user started, the official
   repository is asked again and the item checked again; only then does
   "Install and Relaunch" reach Sparkle. Anything else (the tag withdrawn or
   moved, GitHub unreachable, an error) cancels Sparkle's prepared installer,
   says why, and gives the update slot back; the release is offered again at
   the next check. Quitting WinZapp while that window is open installs the
   update on quit, as a background download does (the other accounts are
   already closed, and the update slot stays held until WinZapp is gone).
6. **Sparkle's own checks** come on top, as integrity of the download: a
   signed appcast (`SURequireSignedFeed`, `SUVerifyUpdateBeforeExtraction`, a
   signing failure that never expires), the zip's EdDSA signature, its
   code-signing checks and its refusal of a downgrade.

**Release notes.** Sparkle's window shows what changed between the version
running and the one offered, as the Windows updater's "What's new?" does
(#488): the changelog of WinZapp's language at the offered tag compared with
the one this app shipped, and for an alpha the pull requests of every alpha
between the two, from the official repository's GitHub releases
(`sparkle_notes.py`). They are fetched when the window is about to open (it
waits up to 5 seconds for them) and handed to Sparkle through
`standardUserDriverWillShowReleaseNotesText:...`. When they cannot be fetched
the appcast's own notes are shown: for an alpha, its GitHub release's pull
requests first, then the changelog section of the release it heads to.

**A scheduled check that finds an update** while WinZapp is not the active
app (or is, but Sparkle chooses not to interrupt) does not show Sparkle's
window until WinZapp is next activated; that is Sparkle's standard behaviour.
WinZapp says it (as its own updater speaks an update while its window is
hidden) and posts it as a notification VoiceOver reads, in words that say how
to act on it: "A new version of WinZapp is available (v...). To install it,
choose Help > Check for updates, or switch to WinZapp." (Sparkle's gentle
scheduled update reminders.) Activating the notification, or "Check for
updates", opens Sparkle's window at once.

**"Ready to Install"** is a window Sparkle shows without making it key, so
WinZapp's own window would keep VoiceOver. WinZapp says it ("...ready to
install. Choose Install and Relaunch in the Software Update window.", with
Sparkle's own names for both in each language) and brings the window forward
and key when that takes focus from no one: WinZapp is already the active app,
or the user started this check. After a scheduled check, with another app in
front, it does not take focus from that app: a notification says it, and
activating it brings the window.

**log.log** has a line for each step of an update, successful or not
(`[sparkle_mac]`): the official tag and commit confirmed, the item checks,
the update shown or announced, where its release notes came from, the user's
choice, the other accounts asked to quit, the update slot claimed, the
download started and finished, Sparkle's installer started, a background
download ready to install on quit, Ready to Install (and whether it was
brought forward or announced), the last check, Install handed to Sparkle.

Sparkle's update window also has its own "Automatically download and install
updates in the future" box; what it sets lasts until WinZapp's setting is
applied again (next launch, or a change in Settings).

Not checked on this path, deliberately: SHA256SUMS.txt, the provenance
file's zip hash, the Team ID, `codesign` and `spctl` of the new app
(`updater_mac` still checks them). **Remaining gap:** an update installed on
quit was checked against the official repository when it was offered, not
when it installs; a tag withdrawn in between still installs. So does one
whose "Ready to Install" window is open when WinZapp ends without its own
quit path (a crash, a force quit, a power cut, a SIGKILL): Sparkle's
installer then installs what passed the checks at steps 1 and 3.

**Which account is still running** is `update_coord`'s runtime leases (pid
and creation time). The Mac build ships psutil for real creation times, and
`coord_mac` tells update_coord whether a pid is still a WinZapp process at
all. Before that every lease recorded 0.0, quitting (which ends in
`os._exit`) never removed it, and a leased pid reused by any program counted
as a running account: leases from months of quits made every update refuse
"another account is running". They are now reaped as they are read.

Without `WINZAPP_SPARKLE_PUBLIC_KEY` at build time nothing changes:
`updater_mac` as above. If Sparkle.framework cannot load or start, the app
falls back to `updater_mac` too. Builds before Sparkle (and builds without
`WinZappReleaseTag`, such as 3895) cannot update through it; the releases
keep their zips, provenance and SHA256SUMS.txt, so older builds update to
the first Sparkle build through `updater_mac` and Sparkle takes over from
there.

**Building with Sparkle.** `build_app.py` always embeds Sparkle.framework
in `Contents/Frameworks` without its XPC services (they serve sandboxed
apps; Sparkle documents removing them), signs `Autoupdate`, `Updater.app`
and the framework one by one with the hardened runtime as Sparkle documents,
and writes the Sparkle keys only when `WINZAPP_SPARKLE_PUBLIC_KEY` (the
public EdDSA key) and `WINZAPP_SPARKLE_FEED_BASE` (the https folder the
appcasts are served from) are set, on a release build. CFBundleVersion of a
release is `provenance.bundle_version(tag)`, `2.1.0.42581` for
`v2.1.0.4258alpha`: four integers that Sparkle orders exactly like the tags
(the last digit is the channel: alpha 1, beta 2, stable 3).

**Publishing.** `macos/release/appcast.py` signs each published zip with
Sparkle's own `sign_update` and keeps `appcast-<arch>.xml` (one per
architecture, signed), published with GitHub Pages. The workflow change and
the one-time setup are in `macos/release/README.md`.
