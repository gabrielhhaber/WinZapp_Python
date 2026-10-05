# WinZapp

WinZapp is a **free, self-hosted, open-source desktop WhatsApp client for Windows**, built primarily for **accessibility for blind and low-vision users**.
It is designed from the ground up to work with screen readers (NVDA, JAWS, Narrator) through [accessible-output2](https://github.com/accessibleapps/accessible_output2), with a fully keyboard-navigable interface built on plain wxPython controls rather than custom-drawn UI.

---

## Download and install

Only download WinZapp from this repository's Releases page: <https://github.com/gabrielhhaber/WinZapp_Python/releases>. Copies of WinZapp on other websites, file-sharing services or app stores are not ours, and we cannot vouch for what they contain.

### Which file to download

Every release lists these files under "Assets":

* **WinZappInstaller.exe**: the installer. This is the right choice for most people.
* **WinZapp.zip**: the same program as a ZIP file. Extract it to a folder on a local disk and run `WinZapp.exe` from there.
* **SHA256SUMS.txt**: the SHA-256 checksum of the two files above, used to check your download (see "Check your download").
* **SHA256SUMS.txt.sig**: a digital signature of the checksum file. It is present on newer releases (currently the alpha releases).

### Stable and alpha releases

* **Stable releases** are made by hand and are the recommended choice. The newest one is marked "Latest" on the Releases page.
* **Alpha releases** are marked "Pre-release". They are built automatically from the `main` branch for every change, they are not tested, and they may contain bugs. Their version numbers end in "alpha".

If you are not sure which to pick, download the release marked "Latest".

### System requirements

* Windows 10 or Windows 11, 64-bit. The bundled Node.js runtime is a 64-bit Windows build, and the code targets Windows 10 and 11. We have not published a tested list of older builds.
* An internet connection, and a WhatsApp account on your phone.
* A screen reader is optional. WinZapp works with NVDA, JAWS and Narrator.

### Install with the installer

1. Download `WinZappInstaller.exe` from the Releases page.
2. Run it. By default it installs into `%LOCALAPPDATA%\WinZapp`, which does not need administrator rights. You can choose another folder with the Browse button.
3. Two check boxes, both checked by default, create a desktop shortcut and a Start menu shortcut.
4. Press Install, then start WinZapp.

The installer also registers an uninstaller, so you can remove WinZapp from Windows Settings, Apps. Install on a local disk: WinZapp cannot run from a network folder such as `\\server\share`.

### First start

The release includes the program and a portable Node.js runtime, so you do not need to install Node.js. It does not include the WPPConnect Server's own dependencies (the files that let WinZapp talk to WhatsApp Web). On first start WinZapp downloads and sets them up, including a browser component (Chromium) that it uses to reach WhatsApp Web. This needs an internet connection and shows a progress window. Starting the API for the first time can then take up to 3 minutes while its database is prepared; WinZapp tells you to wait. Later starts skip these steps.

### Link your WhatsApp account

On first start WinZapp asks you to connect your account. You have two choices:

* **Connect with phone number**: enter your number and WinZapp shows a pairing code. On your phone, open WhatsApp, Linked devices, Link a device, and choose to link with a phone number instead.
* **Connect with QR code**: scan the QR code with WhatsApp on your phone, under Linked devices.

WinZapp is a linked device, like WhatsApp Web. Your phone stays your main device.

### Where your data is stored

WinZapp keeps everything next to the program, in a `data` folder: your accounts, message history, downloaded media and logs. With the installer that is `%LOCALAPPDATA%\WinZapp\data`. Back this folder up if you want to keep your history when moving to another computer.

### Updates

WinZapp checks GitHub Releases for new versions and offers to download and install them. You can also choose Help, Check for updates. Every version of the updater checks the SHA-256 checksum of the download. Builds from the 2.0 line onward also require a valid signature and refuse a release that has none. The updater in the stable version 1.1.1.0 only checks the checksum, because signatures did not exist yet when it was built.

By default you only receive stable releases. To also receive alpha releases, check "Check for alpha updates (unstable)" in Settings, General tab.

### Check your download

Every release has a `SHA256SUMS.txt` file with one line per file: the checksum, then the file name. Newer releases start the file with a line beginning with `#`, such as `# winzapp-version: 2.0.0.3904alpha`; that is a comment, not a checksum, so ignore it. The signature file `SHA256SUMS.txt.sig` currently exists only on the alpha releases; the stable release 1.1.1.0 has none. To check a download in Windows PowerShell, run this in the folder where you saved the file, and compare the result with the line for that file in `SHA256SUMS.txt`:

```powershell
Get-FileHash .\WinZappInstaller.exe -Algorithm SHA256
```

The two values must be identical. If they differ, delete the file and download it again from the Releases page.

Be aware of the limits of this check. `SHA256SUMS.txt` is published on the same page as the files, so it protects you against a corrupted download, not against someone who can edit the release. For that reason newer releases (currently the alpha releases) are published as immutable, so they cannot be changed afterwards. Older stable releases, including the current "Latest" release, are not immutable. Builds from the 2.0 line onward also verify a signature made with keys that are not stored on GitHub. How this works is described in [docs/traps/release-integrity.md](docs/traps/release-integrity.md). Checking the signature by hand is not something we document for users yet; the updater does it for you.

### macOS

The repository contains a macOS version for VoiceOver users, described in [macos/README.md](macos/README.md). No official Mac builds are published from this repository yet. Whether and how to publish them is being discussed in [issue 343](https://github.com/gabrielhhaber/WinZapp_Python/issues/343). Until that is decided, please do not trust Mac downloads from other sources.

---

## Get help and report problems

Report bugs and ask for features on [GitHub Issues](https://github.com/gabrielhhaber/WinZapp_Python/issues). Search the existing issues first. The bug form asks for your WinZapp version (Help, About the program) and your Windows version.

When you report a bug, attach `log.log` and, for anything about being asked to pair again or losing your session, also `shutdown_audit.log`. Both are in the `logs` folder of your account, under `data\accounts\<account id>\logs` inside the WinZapp folder. If you use several accounts, each has its own folder there; open the one with the most recently modified `log.log`. `log.log` is replaced every time WinZapp starts, so copy it before reopening the program. Remove phone numbers, names and messages from the files before you attach them.

---

## Key Features

### Accessibility
* Built entirely from standard wx controls (`wx.ListCtrl`, `wx.TextCtrl`, standard dialogs/menus) so screen readers read them reliably, instead of custom-drawn or owner-drawn UI.
* List updates are batched so a screen reader receives one accessibility event per change instead of a flood during bulk updates (e.g. syncing history).
* Dialog titles and list items resolve to human-readable contact/group names rather than raw phone numbers or WhatsApp JIDs.
* Playback controls for voice notes directly inside the conversation view.

### Messaging
* Text, voice notes, images, videos, documents, contacts, replies/quotes, @mentions, reactions, message edits and deletes, read receipts, and typing/recording indicators.
* Local message history stored in a SQLite database (`messages.db`) whose message contents are encrypted, with a background-managed connection so the UI never blocks on disk I/O.
* Outgoing sends go through a background queue with automatic retry and duplicate-delivery protection for ambiguous network failures.

### JID handling
WhatsApp uses several different identifier formats for the same contact (`@s.whatsapp.net`, the legacy `@c.us`, and `@lid` for linked/multi-device identities). WinZapp normalizes these to a single canonical form per contact, bridges `@lid` identities to phone numbers as they are resolved, and handles the Brazilian 8/9-digit mobile number variants transparently.

### Auto-updater
* Checks GitHub Releases for new versions and can download and install updates automatically.
* Before overwriting files, it stops any stray WPPConnect Server (port 6300) or PostgreSQL (port 5433) processes still holding a lock on them.

### Security
* The WhatsApp session token and local message payloads are encrypted at rest with a per-install Fernet key.
* Downloaded release assets and the portable Node.js runtime are checksum-verified before use.

---

## How it works

The application is split into two processes that run together locally:
1. **Client (Python 3.13 + wxPython):** all UI, business logic, local storage, notifications, and sounds.
2. **WPPConnect Server (Node.js):** a locally-run WhatsApp Web automation gateway, built from the upstream [wppconnect-team/wppconnect-server](https://github.com/wppconnect-team/wppconnect-server) project with a small set of patches WinZapp maintains on top. The client talks to it over local HTTP (`http://127.0.0.1:6300/api/...`) and Socket.IO.

---

## For developers and contributors

Instructions for running WinZapp from source, running the tests and building the installer are in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). Contribution rules (where code goes, tests, translations) are in [CLAUDE.md](CLAUDE.md).

---

## License and Disclaimer

WinZapp is licensed under the GNU General Public License, version 3 or, at your option, any later version (GPL-3.0-or-later; see [LICENSE](LICENSE)). It works by automating the WhatsApp Web interface and is not built on any official WhatsApp/Meta API. Use of this software is at your own risk. This project is not affiliated with, maintained by, or endorsed by Meta Platforms, Inc.
