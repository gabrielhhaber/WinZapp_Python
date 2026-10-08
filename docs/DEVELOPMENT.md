# Developing WinZapp

This page is for people who want to run WinZapp from source, change it or build it.
If you only want to use WinZapp, go back to the [README](../README.md), which explains how to download and install it.

Rules for contributors (where code goes, tests, translations) are in [CLAUDE.md](../CLAUDE.md). Longer background notes are in [docs/reference](reference/).
Bugs and ideas go to [GitHub Issues](https://github.com/gabrielhhaber/WinZapp_Python/issues).

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

Run one test process at a time, without xdist workers. A local full run is
reserved for changes spanning shared helpers or several subsystems. Real
dialog tests (`wxgui`) are skipped locally: `--run-wx-gui` and
`WINZAPP_RUN_WX_GUI_TESTS=1` are CI-only. Synthetic `load` tests are skipped
unless `--run-load` is passed or CI is set; remote-model `network` tests have
their own explicit opt-in. A local green run therefore does not replace CI's
native-dialog checks or manual screen-reader acceptance.

The source and test ownership map is in
[docs/reference/architecture-and-tests.md](reference/architecture-and-tests.md).
The [2026-10-08 audit](reference/repository-audit-2026-10-08.md) records the
shortcut CI failures, documentation corrections and local verification limits.

Every release build is gated on the full test suite passing (see [.github/workflows/release.yml](../.github/workflows/release.yml)) — a failing test suite stops the build before any release is created.

---

## Building

### Automated (recommended)

Pushing a stable version tag triggers the [release workflow](../.github/workflows/release.yml), which runs the test suite and, if it passes, builds `WinZappInstaller.exe` and `WinZapp.zip` on GitHub's own servers and attaches them to a **draft** release. The maintainer then signs the draft with the offline release key and publishes it (requires the [GitHub CLI](https://cli.github.com/)):

```powershell
git tag v1.2.3.0
git push origin v1.2.3.0
# wait for the Release Build workflow to finish, then:
venv\Scripts\python.exe .github\scripts\release_signing.py sign-stable v1.2.3.0 --key <path to stable-primary.pem>
```

Releases are signed so that the auto-updater only installs builds the maintainer vouched for with a key that never lives on GitHub — see [`client/core/release_signature.py`](../client/core/release_signature.py).

### Local build (fallback)

The build downloads the checksum-verified portable Node.js into `client/node/` when it is missing or is not exactly the version in `client/node_download_config.py`, and runs `setup_api.py` on its own when the API has not been built. The default onedir build additionally requires MSYS2 with GCC/windres in `PATH`, used to compile the C installer/uninstaller stubs (including static zlib, `mingw-w64-ucrt-x86_64-zlib`, which comes with gcc).

```powershell
# With uv (and GCC/windres in PATH for the onedir build):
uv run build-installer             # onedir build: WinZappInstaller.exe + WinZapp.zip
uv run build-onefile               # single-file build: WinZapp.exe + WinZapp.zip (no GCC/windres needed)

# Or with the venv:
python build.py                    # onedir build
python build.py --onefile          # single-file build
```

The resulting files are written to the `dist/` directory.
