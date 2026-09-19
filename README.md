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
* **uv** — optional but recommended: it installs Python 3.13 and the locked dependencies for you (`winget install --id=astral-sh.uv -e`). A plain `venv` + `pip` works just as well.
* **Node.js 22.x** — used by `setup_api.py` to build the WPPConnect Server, which pins `engines.node` exactly and is only verified on that line. A portable copy at `client/node/` is preferred over whatever is on `PATH`, and `uv run build-onefile` puts the right one there for you. A system Node on another major is refused with a message rather than allowed to fail later: on Node 26 the Chromium download stops part-way, reports nothing, and leaves a folder that makes every later run fail. Set `WINZAPP_ALLOW_SYSTEM_NODE=1` to use one anyway.
* **Git**
* For building the installer locally only: **GCC** and **windres** (available via [MSYS2](https://www.msys2.org/), UCRT64 toolchain)

### Steps to run locally

```powershell
# 1. Clone the repository
git clone https://github.com/gabrielhhaber/WinZapp_Python.git
cd WinZapp_Python
```

Then pick **one** of the two ways to set up Python. Both install the same pinned versions.

**With uv** (recommended; it downloads Python 3.13 itself if needed):

```powershell
uv sync                  # creates .venv from the committed uv.lock
uv run setup-api         # clones and builds the WPPConnect Server into client/api/
uv run winzapp           # starts the client in development mode
```

**With venv and pip:**

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt   # adds pytest and friends, for running tests
python setup_api.py                   # clones and builds the WPPConnect Server into client/api/
cd client
python main.py                        # starts the client in development mode
```

Always start the client from inside `client/` (`uv run winzapp` does that for you): in development mode its data — accounts, pairing, messages — lives in the `data/` folder of the current directory.

`setup_api.py` clones WPPConnect Server into `client/api/`, restores WinZapp's own patched files on top, then installs its Node dependencies and builds it. Re-run it whenever `client/api/` needs to be rebuilt from scratch — it preserves `node_modules` across re-clones.

#### uv shortcuts

After `uv sync`, these run the matching script without having to remember its path:

```powershell
uv run winzapp           # the client (it starts and manages the API itself)
uv run api               # only an already-built WPPConnect API
uv run setup-api         # clone, patch and build the API
uv run build-onefile     # portable single-file build, no GCC/windres needed
uv run build-installer   # installer + ZIP; requires MSYS2 GCC/windres
uv run test              # the test suite, without opening wx dialogs
```

#### Changing a dependency

Pins live in both `pyproject.toml` and `requirements.txt` / `requirements-dev.txt`. Change both, then run `uv lock`; `tests/test_requirements_in_sync.py` fails if they disagree.

### Running tests

```powershell
pytest                                   # full suite, from the repository root (prefix with `uv run` under uv)
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

The build downloads the checksum-verified portable Node.js into `client/node/` when it is missing or is not exactly the version in `client/node_download_config.py`, and runs `setup_api.py` on its own when the API has not been built. The default onedir build additionally requires MSYS2 with GCC/windres in `PATH`, used to compile the C installer/uninstaller stubs.

```powershell
# With uv (and GCC/windres in PATH for the onedir build):
uv run build-installer             # onedir build: WinZappInstaller.exe + WinZapp.zip
uv run build-onefile               # single-file build: WinZapp.exe + WinZapp.zip (no GCC/windres needed)

# Or with the venv:
python build.py                    # onedir build
python build.py --onefile          # single-file build
```

The resulting files are written to the `dist/` directory.

---

## License and Disclaimer

WinZapp is licensed under the GNU General Public License v3.0 (see [LICENSE](LICENSE)). It works by automating the WhatsApp Web interface and is not built on any official WhatsApp/Meta API. Use of this software is at your own risk. This project is not affiliated with, maintained by, or endorsed by Meta Platforms, Inc.
