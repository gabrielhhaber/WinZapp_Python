# Building neonize for the Neonize bridge

> How WinZapp builds the neonize package (Python + the Go DLL with whatsmeow)
> that `neonize_bridge/` runs, how to change it, and how to move it to a new
> upstream release. The same model as WPPConnect Server: official upstream at
> an approved version, WinZapp's changes as patches.

## What is built from what

| Piece | Upstream | Pinned in `neonize_bridge/neonize_upstream.json` | WinZapp's patches |
| --- | --- | --- | --- |
| neonize (Python package + `goneonize/` Go glue) | github.com/krypton-byte/neonize (Apache-2.0) | `neonize.approved[-1]`: version and full commit | `neonize_patches/neonize/` |
| whatsmeow (WhatsApp protocol, inside the DLL) | go.mau.fi/whatsmeow (MPL-2.0) | `whatsmeow.version`, the one neonize's go.mod requires | `neonize_patches/whatsmeow/` |
| meowcaller (voice calls, inside the DLL) | github.com/purpshell/meowcaller (MIT) | `meowcaller.commit` | `neonize_patches/meowcaller/` |
| protobuf toolchain | protoc, protoc-gen-go, mypy-protobuf | `toolchain` (protoc checked by SHA-256) | none |

The patches are plain `git format-patch` files (`--zero-commit`, so an
unchanged export is byte for byte the same). Each tree's patches apply in
order on top of its pin. Generated protobuf code (`Neonize.pb.go`,
`Neonize_pb2.py/.pyi`) is never in a patch: the build regenerates it from
`Neonize.proto` with the pinned toolchain, which reproduces upstream's own
generated files exactly (checked on 0.6.0).

What the patches carry today:

- **neonize 0001**: neonize 0.6.0's `SendSPRMessage` sets two
  `SendRequestExtra` fields that no released whatsmeow has, so 0.6.0 does not
  build against its own pin (its GitHub release has no binaries). The call
  refuses after validating its input; dropping the fields would send the
  targeted text to the whole group. Remove this patch once the pin has them.
- **neonize 0002**: app-state events (archive, pin, mute, read, lock, delete
  chat, delete for me, star: events 46-53), `fetch_app_state` /
  `request_app_state_recovery`, the `DecodePatchInfo` fix, transfers to and
  from disk with progress and cancel, `update_group_request_participants`
  (upstream pull requests krypton-byte/neonize #227-#232, by reffehcsoibaf).
- **neonize 0003**: voice calls through meowcaller (`calls.go`, exported
  functions, no protobuf) and the session database in WAL with a 30 s busy
  timeout.
- **neonize 0004**: `ContactUpdate` (54), `PushNameUpdate` (55) and
  `AppStateSyncComplete` (56). A full app-state sync (right after pairing)
  does not emit `ContactUpdate`: whatsmeow stores those names in bulk, and
  `AppStateSyncComplete` for `critical_unblock_low` says when they are there
  (`contact_events.py` reloads them).
- **neonize 0006**: raw info queries and MEX (GraphQL) queries
  (`goneonize/rawiq.go`: `SendIQ` / `SendMexIQ`, JSON in and out; the
  client's `send_iq` / `send_mex`), `AppStateMutation` (57) for every app
  state mutation, and `MutationInfo.Remove` so `send_app_state` can send a
  REMOVE. The bridge's custom lists, community joins, channel edits, status
  privacy and contact removal use them (`neonize_bridge/COVERAGE.md`).
- **neonize 0007**: `MediaConn`, the media upload hosts and token, for the
  unencrypted upload of a catalog product's picture (`media_upload.py`).
- **neonize 0011**: the sync client's `get_linked_group_participants` reads
  the `ReturnFunctionWithError` the Go side sends (it parsed another message
  and failed with "Wire format was corrupt"; the async client was right). It
  gives `community-participants` and the community total in `group-info`.
- **whatsmeow**: `events.Lock` for lock-chat mutations; button, list and flow
  replies without the `<biz>` node (the server refuses them with 479);
  `MutationInfo.Operation` (REMOVE) and `events.AppStateMutation`, emitted for
  every mutation with its collection and version, also during a full sync
  (except the contacts a full sync stores in bulk); the `<biz>` node of a
  native flow message (buttons, Pix, orders), as WhatsApp Web sends it;
  `sendAppState` under `appStateSyncLock`, as `fetchAppState` already was
  (0008: two sends, or a send and a notification's fetch, applied a 409's
  conflicting patches on a stale version and broke the collection's LTHash;
  the bridge also serializes its own calls, `app_state_recovery.serialize`);
  the sender key only for the devices that do not hold it (0009: a group
  message used to encrypt it for every device of the group, 1250 ms against
  158 ms to a person. The memory is in RAM, so the first message to each group
  after a start still distributes it, and a failed send forgets the group. A
  remembered device whose session is gone gets the key again: whatsmeow
  deletes a number's sessions when its identity changes (a reinstall, a new
  phone), and the device comes back under the same JID without the key, while
  a retry receipt is answered only from the recent-message cache a restart
  empties. Sessions or LID mappings that cannot be read send it to everyone);
  a status goes to its recipients' numbers, each once (0010: the contact
  store keys a saved name by the JID of the mutation that carried it, and a
  `contact[lid]` mutation put a `<user>@lid` among the numbers of the status
  recipients; from the first one on, the server answered every status with
  400. A LID whose number is unknown is left out).
- **meowcaller**: go.mod/go.sum on neonize's whatsmeow pin; accept 2 s after
  Answer when no `mute_v2` arrives; resynchronize the SRTP receive ROC.
  meowcaller is pinned at 27a3c6b1, its last commit on `go.mau.fi/whatsmeow`:
  later ones move to a whatsmeow fork (hypermeow).
- **neonize 0015**: `CallAddParticipant` adds a number or LID to the call this
  client is in (meowcaller's `Call.AddParticipant`; `RingParticipant` for someone
  already on the call's list), WhatsApp's "Add participant"; Python
  `CallClient.add_participant` (`neonize_bridge/call_invites.py`).
- **neonize 0016**: a channel update (`NewsletterMessage`) carries whatsmeow's
  `MessageID`, `Timestamp` and `Type`, which the encoder dropped: a channel's
  updates could not be deduplicated nor sorted (`neonize_bridge/channel_updates.py`).
- **neonize 0017 / whatsmeow 0011**: a channel update carries the server's
  `is_sender="true"` (`NewsletterMessage.IsSender`), the attribute WhatsApp Web keys an
  update `fromMe` from: a channel's admin sees their own updates as sent.
- **neonize 0013 / meowcaller 0005-0006**: bounded PCM reads without multi-frame
  truncation, on-time microphone underruns, negotiated MLow/Opus codecs and
  authenticated packet ordering, including late accepted codec settings.
  See [call audio](neonize-call-audio.md) for
  clocks, synthetic tests and quality limits. The bridge also filters the
  48-to-16 kHz microphone conversion and owns each receive reader independently
  of state-event order.
- **meowcaller 0008**: an info-level `audio receive summary` every 5 s while
  relay packets arrive (RTP by kind, payload types, authentication failures,
  packets that released no frame, frames and samples out). A call whose log
  shows `quadros do outro lado=0` says there where inbound audio stops; no
  summary at all means the relay delivered nothing.
- **meowcaller 0009**: the caller stops the callee's other devices when one
  answers or declines, one `<terminate>` per rung device (`accepted_elsewhere`,
  `rejected_elsewhere`). WhatsApp leaves this to the caller; without it the
  phone went on ringing beside the device that answered. A callee that already
  saw the caller's `<mute_v2>` also accepts on Answer, without the two seconds
  of patch 0002, because the caller only stops those devices on `<accept>`.
  **Not yet validated on a live call.** The device that answered is told by
  its device number; an `<accept>` with a bare `from` while the call's peer is
  still bare counts as the phone (device 0). Before a release, call a contact
  with a phone and a companion device and answer on each in turn.
- **meowcaller 0010**: diagnostics only, no behaviour change. For every call it
  logs the relay endpoints (name, `is_fna`, token ids; never an address), the
  one media runs on (`*`), the first STUN packets the relay sends back, and
  whether relay data arriving after media started would have picked another
  endpoint. Added for issue #689: on an inbound call the native layer reported
  `quadros do outro lado=0` for the whole call and never saw a peer SRTCP.

## Toolchain (once)

```powershell
winget install GoLang.Go BrechtSanders.WinLibs.POSIX.UCRT
```

Go must be at least the `go` line of neonize's `goneonize/go.mod`, and gcc a
64-bit MinGW (`gcc -dumpmachine` starts with `x86_64`). Open a new terminal
afterwards so both are on the PATH. protoc, protoc-gen-go and mypy-protobuf
are fetched by the build into `neonize_bridge/neonize_build/tools/`.

## Building

```powershell
neonize_bridge\.venv\Scripts\python.exe neonize_bridge\build_neonize.py all --install
```

`all` prepares the workspace if it is missing, regenerates the protobuf
code, compiles the DLL and, with `--install`, puts the package in
`neonize_bridge/.venv` (removing the metadata a PyPI neonize left there).
`requirements.txt` and its lock carry neonize's own dependencies, never
neonize: every place the bridge runs or is tested (`prepare.py`, the release
build, `ci.yml`'s bridge job, `neonize-dll.yml`) uses this build. A throwaway
test virtualenv takes the lock and then
`build_neonize.py install --venv <that venv>`. **Close WinZapp first**:
the running bridge keeps the DLL open, and `install` refuses rather than
leave half a package. `prepare.py` does the same for a fresh checkout
(downloading the published build instead when `release` is set).

Output in `neonize_bridge/neonize_build/dist/`: `neonize/` (the package with
`neonize-windows-amd64.dll`), the zip, `SHA256SUMS.txt` and
`BUILD_INFO.json` (the exact inputs: neonize commit, whatsmeow, meowcaller,
a hash of each tree's patches, the zip's SHA-256).

## Changing neonize, whatsmeow or meowcaller

`neonize_bridge/neonize_build/` (git-ignored) holds three git checkouts:
`neonize/`, `whatsmeow/`, `meowcaller/`. In each, tag `upstream` is the pin
and branch `winzapp` has one commit per patch.

1. `build_neonize.py prepare` (only when the workspace is missing or should
   be rebuilt from the patches; it refuses to discard uncommitted changes or
   commits not exported yet, unless `--force`).
2. Edit in the checkout and commit (a new commit for a new change; amend or
   `git rebase` to change an existing patch). A new event takes the next
   free id in `Neonize.proto` (`//57`...), a `case` in `main.go` and the
   registry in `neonize/events.py` and `neonize/aioze/events.py`.
3. `build_neonize.py all --install`, then the bridge's tests.
4. `build_neonize.py export` and commit `neonize_bridge/neonize_patches/`
   with the bridge change that uses it.

A bridge handler for an event only WinZapp's build has registers behind
`if hasattr(nev, "...")`, so an install still on an older build keeps
starting (`contact_events.register_events`).

## Moving to a new upstream neonize

```powershell
neonize_bridge\.venv\Scripts\python.exe neonize_bridge\build_neonize.py check --ref 0.6.1
```

`check` rebuilds the workspace on that ref (a tag, branch or commit),
applies every patch, regenerates and builds. The "Neonize DLL" workflow runs
it weekly on upstream `master`; red there means "time to port", nothing
else depends on it.

- A patch that no longer applies stops `git am` in
  `neonize_build/<tree>/`: resolve it there (`git am --continue`), finish
  the build, `export`.
- When upstream's go.mod moves whatsmeow, update `whatsmeow.version` and port
  the whatsmeow patches the same way; `prepare` refuses a go.mod whose
  whatsmeow differs from the manifest.
- A patch upstream merged becomes empty: drop it from the series.

Then approve it, which records the commit the tag resolves to:

```powershell
neonize_bridge\.venv\Scripts\python.exe neonize_bridge\build_neonize.py approve --ref 0.6.1 --version 0.6.1
```

Commit the manifest, the patches and the bridge's tests run against the new
build (the workflow does that on the pull request).

## Publishing a build

`prepare.py` installs a published build so that people without Go can run
the bridge. To publish one:

1. Run the "Neonize DLL" workflow by hand with `release_tag` (it must start
   with `neonize-` and not contain `alpha`: this repository's releases are
   also WinZapp's update channel, and `tests/test_alpha_updates.py` pins that
   the updater ignores such a tag). It builds, runs the bridge's tests and
   creates a **draft** release with the zip, `SHA256SUMS.txt` and
   `BUILD_INFO.json`.
2. Review and publish the draft.
3. Record it in `neonize_upstream.json`:
   `"release": {"tag": "...", "asset": "neonize-<version>-winzapp-windows-amd64.zip", "sha256": "<from BUILD_INFO.json>"}`.

Until `release` names one, `prepare.py` builds from source.
