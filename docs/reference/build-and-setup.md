# Environment, dev setup and building

> The full notes behind the commands in `CLAUDE.md`: uv vs venv and why both must keep working, what `setup_api.py` restores, where the bundled Node version is named, and how `build.py` picks its interpreter.
>
> Moved verbatim out of `CLAUDE.md`; the short form stays there.

## Dev setup
Two supported ways to get the Python environment, and **both must keep working** — existing checkouts, forks and scripts use the second:
```powershell
# uv (recommended for a fresh checkout: fetches Python 3.13 itself, installs from uv.lock)
uv sync
uv run setup-api                      # clones + builds client/api/ (WPPConnect Server) — one-time

# venv + pip
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt   # adds pytest, pytest-cov, pytest-asyncio
python setup_api.py
```
`pyproject.toml` (resolved into `uv.lock`) and `requirements.txt` pin the **same versions** — every package uv installs on Windows, transitive ones included — while `requirements-dev.txt` holds ranges that must accept those pins. Change a dependency in both places, then run `uv lock`. `tests/test_requirements_in_sync.py` fails when they disagree, including version markers, a stale `uv.lock`, and a transitive dependency `uv lock` pinned that `requirements.txt` does not. CI runs uv at an exact `version:` on every `setup-uv` step, pinned like the actions themselves. `requirements.txt` deliberately still carries pytest/pyinstaller, because installing only it is what forks and scripts do. The `uv run <command>` shortcuts (`winzapp`, `api`, `setup-api`, `test`, `build-onefile`, `build-installer`) live in `winzapp_tools/cli.py` and only run the script they name — nothing in the repository may require uv.


## Run the client in dev mode
```powershell
uv run winzapp          # or, in the venv:  cd client; python main.py
```
Entry point is `client/main.py`, guarded by `if __name__ == "__main__":` near the bottom of the file. There is no separate "start the API server" dev command — `main.py` launches/manages the local Node WPPConnect Server process itself (`uv run api` / `start_api.py` only exists to poke an already-built API by hand). **The client always runs from `client/`**: in dev mode `app_paths._data_root()` is `os.getcwd()/data`, so starting it from the repository root silently opens a second, empty install under `data/` and asks to pair again. `uv run winzapp` sets that working directory itself.

## Building the distributable
```powershell
uv run build-installer                        # onedir: WinZappInstaller.exe + WinZapp.zip
uv run build-onefile                          # single-file WinZapp.exe + WinZapp.zip
venv\Scripts\python.exe build.py              # same two, from the venv
venv\Scripts\python.exe build.py --onefile
```
Requires the Python environment (either route) and — for onedir only — `gcc`/`windres` in `PATH` (MSYS2 UCRT64) to compile the C installer/uninstaller stubs in `installer/`. `build.py` builds with the interpreter `winzapp_tools/build_env.py`'s `select_build_python()` picks — `WINZAPP_VENV`, then the virtual environment already running it, then `venv\` and `.venv\` in the repository — and hands itself over to it before anything reads site-packages, so a bare `python build.py` still builds with `venv\` exactly as it did when that path was hardcoded. `ensure_build_assets()` downloads the checksum-verified portable Node.js into `client/node/` when it is missing **or not exactly** `node_download_config.NODE_VERSION`, and runs `setup_api.py` when `client/api/dist/server.js` is missing; both directories stay git-ignored. `check_tools()` (step 1) also diffs every patched file in `client/api_patches/` against its live copy in `client/api/` and, on drift, re-runs `setup_api.py` automatically before continuing — a patch edited only in `client/api_patches/` without rebuilding used to ship a stale/reverted `dist/server.js` with no warning.

**The bundled Node.js version is named once, in `client/node_download_config.py`** (22.22.2, the release WPPConnect Server pins in `engines.node`). The app's own download (`ui/dialogs/node_download.py`), `build.py` and `build-windows.yml` all read it; no workflow may declare its own `NODE_VERSION` (`tests/test_node_version_single_source.py`). CI used to, and bundled 24.15.0 into releases while everything else said 22.22.2 — and since `node_runtime_needs_download()` only replaces an *older* runtime, what CI bundles is what users run.
