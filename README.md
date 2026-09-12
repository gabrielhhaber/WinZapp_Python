# WinZapp

WinZapp is a **free, self-hosted, open-source desktop WhatsApp client for Windows**, built primarily for **accessibility for blind and low-vision users**.
It is designed from the ground up to work with screen readers (NVDA, JAWS, Narrator) through [accessible-output2](https://github.com/accessibleapps/accessible_output2), with a fully keyboard-navigable interface built on plain wxPython controls rather than custom-drawn UI.

The application is split into two processes that run together locally:
1. **Client (Python 3.13 + wxPython):** all UI, business logic, local storage, notifications, and sounds.
2. **WPPConnect Server (Node.js):** a locally-run WhatsApp Web automation gateway, built from the upstream [wppconnect-team/wppconnect-server](https://github.com/wppconnect-team/wppconnect-server) project with a small set of patches WinZapp maintains on top. The client talks to it over local HTTP (`http://127.0.0.1:6300/api/...`) and Socket.IO.

---

## Key Features

### Accessibility
* Built entirely from standard wx controls (`wx.ListCtrl`, `wx.TextCtrl`, standard dialogs/menus) so screen readers read them reliably, instead of custom-drawn or owner-drawn UI.
* List updates are batched so a screen reader receives one accessibility event per change instead of a flood during bulk updates (e.g. syncing history).
* Dialog titles and list items resolve to human-readable contact/group names rather than raw phone numbers or WhatsApp JIDs.
* Playback controls for voice notes directly inside the conversation view.

### Messaging
* Text, voice notes, images, videos, documents, contacts, replies/quotes, @mentions, reactions, message edits and deletes, read receipts, and typing/recording indicators.
* Local message history stored in an encrypted SQLite database (`messages.db`), with a background-managed connection so the UI never blocks on disk I/O.
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

## Development Environment

### Prerequisites
* **Python 3.13**
* **Node.js** (used by `setup_api.py` to build the WPPConnect Server; a portable copy can also be placed at `client/node/`)
* **Git**
* For building the installer locally only: **GCC** and **windres** (available via [MSYS2](https://www.msys2.org/), UCRT64 toolchain)

### Steps to run locally

```powershell
# 1. Clone the repository
git clone https://github.com/gabrielhhaber/WinZapp_Python.git
cd WinZapp_Python

# 2. Create and activate a virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt   # adds pytest and friends, for running tests

# 4. Set up the WPPConnect Server (clones and builds client/api/)
python setup_api.py

# 5. Start the client in development mode
cd client
python main.py
```

`setup_api.py` clones WPPConnect Server into `client/api/`, restores WinZapp's own patched files on top, then installs its Node dependencies and builds it. Re-run it whenever `client/api/` needs to be rebuilt from scratch — it preserves `node_modules` across re-clones.

### Running tests

```powershell
pytest                                   # full suite, from the repository root
pytest tests/test_database.py            # a single file
pytest tests/test_database.py::TestChats::test_upsert_chat_creates_record  # a single test
```

Tests cover the async SQLite storage layer and the pure-logic pieces of the client (name resolution, notification formatting, message classification, etc.) using small stand-in objects, since the wxPython UI classes cannot be instantiated without a running `wx.App`.

Every release build is gated on the full test suite passing (see [.github/workflows/release.yml](.github/workflows/release.yml)) — a failing test suite stops the build before any release is created.

---

## Building

### Automated (recommended)

Pushing a stable version tag triggers the [release workflow](.github/workflows/release.yml), which runs the test suite and, if it passes, builds `WinZappInstaller.exe` and `WinZapp.zip` on GitHub's own servers and attaches them to a **draft** release. The maintainer then signs the draft with the offline release key and publishes it (requires the [GitHub CLI](https://cli.github.com/)):

```powershell
git tag v1.2.3.0
git push origin v1.2.3.0
# wait for the Release Build workflow to finish, then:
venv\Scripts\python.exe .github\scripts\release_signing.py sign-stable v1.2.3.0 --key <path to stable-primary.pem>
```

Releases are signed so that the auto-updater only installs builds the maintainer vouched for with a key that never lives on GitHub — see `client/core/release_signature.py`.

### Local build (fallback)

Requires the portable Node.js runtime placed at `client/node/` and the WPPConnect Server built at `client/api/dist/server.js` (via `setup_api.py`). The default onedir build additionally requires MSYS2 with GCC/windres in `PATH`, used to compile the C installer/uninstaller stubs.

```powershell
# With the virtual environment active (and GCC/windres in PATH for the onedir build):
python build.py             # onedir build: WinZappInstaller.exe + WinZapp.zip
python build.py --onefile   # single-file build: WinZapp.exe + WinZapp.zip (no GCC/windres needed)
```

The resulting files are written to the `dist/` directory.

---

## License and Disclaimer

WinZapp is licensed under the GNU General Public License v3.0 (see [LICENSE](LICENSE)). It works by automating the WhatsApp Web interface and is not built on any official WhatsApp/Meta API. Use of this software is at your own risk. This project is not affiliated with, maintained by, or endorsed by Meta Platforms, Inc.
