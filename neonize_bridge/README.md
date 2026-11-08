# Neonize bridge (experimental)

A second way for WinZapp to reach WhatsApp, without a browser. The bridge is a
small local server that speaks the same REST and Socket.IO API as WPPConnect
Server, but connects to WhatsApp through [neonize](https://github.com/krypton-byte/neonize)
(whatsmeow). The client does not know the difference: the Neonize engine is a
custom API pointing at `http://127.0.0.1:6300`.

WPPConnect stays the default. Nothing changes unless you pick **Neonize** in
Settings, Connection (or in the question asked on the first launch).

## Compiled builds

A compiled WinZapp (the installer and the portable zip) already carries the
bridge, frozen as `neonize_bridge/neonize_bridge.exe` inside the install, next
to WPPConnect's code (`api/`; Node.js is downloaded when WPPConnect is chosen) and the
user picks one in Settings, Connection. The bridge keeps its sessions and
messages in `data/global/neonize_bridge/data/` of the install, which updates do
not replace, so each engine keeps its own connection when you switch back and
forth. `build.py` freezes the bridge with the PyInstaller it uses itself (it runs
`prepare.py` first when `neonize_bridge/.venv` is missing); `--skip-bridge` leaves
it out of a quick local build.

## Try it from a repository checkout

```
python neonize_bridge/prepare.py
```

This creates `neonize_bridge/.venv`, installs `requirements.txt` and WinZapp's
build of neonize that the bridge needs (see below). Then open WinZapp,
go to Settings, Connection, choose **Neonize (direct connection,
experimental)**, press OK and restart WinZapp. WinZapp starts the bridge by
itself; the last WinZapp window to close stops it. Sessions and messages live
in `neonize_bridge/data/`, the bridge output in `neonize_bridge/run/bridge.log`.
Pairing works by phone code or by QR code. The QR is drawn the way TeleZapp's is
(black on white, error level L, a 6-module margin), and when WhatsApp refuses a
scan (`invalid device identity HMAC`) the bridge wipes the session keys and shows
a new QR, at most 3 times (`pairing_repair.py`). Not tried on a real account yet.

Nothing the bridge sends needs `ffprobe`: `media_probe.py` reads the duration
of voice notes, audio and video from the file itself (Ogg, MP4/M4A/MOV) or from
`ffmpeg`. Video thumbnails and stickers need `ffmpeg` on the bridge's `PATH`;
`client/lib` is added automatically, and to add other folders list them in
`neonize_bridge/path_extra.txt` (one per line, ignored by git).

## Who can talk to the bridge

The bridge listens on 127.0.0.1 only, but a web page open in your browser can
also reach 127.0.0.1, so it does not trust the network. It creates a secret the
first time it runs, in `neonize_bridge/data/secret` (readable by your
user only on systems that support it). WinZapp reads that file and uses the
secret instead of the API key: `generate-token` answers only to it, every
session token is a keyed hash of the session name, the Socket.IO connection
needs a valid token, and requests whose `Host` is not this machine are refused.
A bridge you start by hand must use the same `--folder` as WinZapp, or the
client cannot find the secret. Deleting the `secret` file
makes the bridge create a new one at the next start; WinZapp then renews its
session token on its own.

## WinZapp's build of neonize

The bridge listens to what the phone does (archive, pin, mute, star, lock,
read, delete, contacts saved) instead of polling for it. Official neonize
does not forward those events yet (pull requests #227 to #232 are open), so
WinZapp builds neonize itself: the official release approved in
`neonize_upstream.json` plus the patches in `neonize_patches/` (neonize,
whatsmeow and meowcaller), compiled by `build_neonize.py`. Everything that
goes into the DLL is in this repository or pinned by commit and checksum.
How to build, change it, move to a new upstream release and publish a
build: [docs/reference/neonize-build.md](../docs/reference/neonize-build.md).

`prepare.py` downloads the build the "Neonize DLL" workflow published when
`neonize_upstream.json` names one, and builds from source otherwise (or with
`--from-source`). A download is checked against its SHA-256 and what is
inside it: only files under `neonize/` with the types the package has
(`.py`, `.pyi`, `.pyc`, `.dll`, `.typed`), no links, no paths that leave the
folder, no `.pth` or `sitecustomize.py`, nothing larger than expected.

## What works

Tested on a real account: pairing by phone code, chats, contacts and history;
text, photo, voice note and document sending and receiving; replies, mentions,
forwarding, editing, reactions, polls and pinned messages; seeing status
updates and opening their photos; read receipts both ways; archive, pin, star,
mute and delete, kept in step with the phone.

## What is untested or missing

Untested: video, sticker and GIF sending and receiving, posting or deleting
your own status, and every route added in the second round (chats and messages,
profile and privacy, contacts, labels, communities, group settings, channels,
call rejection, server management): they pass their tests against stubs but
have not run against a real account. One-to-one voice calls (placing,
answering, the audio both ways) go through meowcaller's call layer
(`call_media.py`) and have not been tried on a real account yet. Video and group
calls go through meowcaller too (neonize 0007, with ffmpeg for the video) and
have not been tried on a real account either. Missing: a few WPPConnect-only
routes: the bridge implements 210 of the 217 that WPPConnect Server defines
for WinZapp.
[COVERAGE.md](COVERAGE.md) lists every route, grouped by area. Muting and
deleting for everyone done by someone else can take up to a minute to show.
Data is not encrypted at rest. Treat it as a preview.

## Development

```
neonize_bridge/.venv/Scripts/python -m pytest neonize_bridge
neonize_bridge/.venv/Scripts/python neonize_bridge/server.py --port 6300 --folder neonize_bridge/data
```

`--log-state` turns on a technical log of app-state events. The bridge's
`requirements.txt` does not bring pytest: install `requirements-dev.txt` too,
as CI does. `build_neonize.py all --install` rebuilds and installs neonize
after a change to its patches (close WinZapp first).

To look inside the bridge WinZapp is running (there is no Chrome, so no CDP):
its HTTP API with the session's token, `run/bridge.log` and a read-only
`bridge.db`, never `session.db`; see `docs/reference/live-inspection.md`.
