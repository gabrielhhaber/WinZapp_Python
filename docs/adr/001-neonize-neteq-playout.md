# ADR-001: Device-clock NetEq for native Neonize PCM

**Status:** Proposed; implemented on the feature branch, pending maintainer review.
**Date:** 2026-11-24
**Deciders:** WinZapp maintainers

## Context

Issue [#768](https://github.com/gabrielhhaber/WinZapp_Python/issues/768) requests
NetEq's Expand, Merge and time stretching. The existing playback reservoir
absorbs arrival jitter but drops old samples and fades through underruns. A
second native timer/reservoir would introduce another clock and buffering delay.
The current transport delivers decoded PCM; encrypted RTP and stateful
MLow/Opus decoding remain owned by meowcaller.

## Decision

Use `neteq = 1.0.1`, without its default features, behind a small serialized
C ABI in `native/call_neteq`. This is Security Union's Rust port of WebRTC
revision `6341cca49ffcad6fcb4603bd6cd1325fd0fe28bd`, rather than Chromium's C++
binary. Cargo.lock pins every transitive dependency. The upstream project
reports sample/operation parity with that C++ revision; our tests exercise our
integration, not independent certification of that upstream claim.

The optional DLL ships inside the existing Neonize artifact, with notices and
checksums. Build it with pinned Rust/MSVC and static CRT. Include its sources,
lockfile and build helper in the artifact identity. Load only a fixed installed
bridge path; do not download or accept a DLL path over HTTP.

Protocol 2 carries source SSRC, negotiated clock and PCM-unit RTP timestamps.
The client player performs its existing FIR conversion, then inserts L16 PCM
into NetEq at the device rate. The output callback requests 10 ms blocks as
needed, retaining partial blocks across arbitrary device periods. It replaces
the legacy adaptive reservoir for those frames; no second timer or prebuffer
is added. The existing stream-owner gate also serializes native lifetime and
prevents a retired callback from advancing a replacement instance.

## Options considered

| Option | Benefits | Costs |
| --- | --- | --- |
| Build full C++ libWebRTC | Original implementation; potential later codec/APM/device integration | Large Chromium/depot_tools toolchain, version coupling and new build burden |
| Pinned Rust NetEq port | Native adaptive playout, small DLL, upstream parity harness, isolated ABI | New Rust build dependency; port still needs live acceptance and upstream maintenance |
| Keep the existing reservoir | No new build dependency | Does not implement NetEq's concealment and time stretching |

## Consequences

This is **PCM playout NetEq**, not a complete native WASAPI/APM/RTP engine.
Meowcaller's authenticated reorder/decoder policy stays in place. Its codec
state may advance through a missing predecessor, but synthetic concealment PCM
is not inserted again when the timed sink owns direct-call playout. NetEq
conceals by the device clock even when no later packet arrives.

Live translation, unsupported device rates, older bridge/DLL versions and
unavailable/failed components retain compatible playback. Group calls
(2026-11-25, protocol 3) give each sender its own NetEq instance and sum them
in the device callback; without NetEq they keep the participant mixer. Supported output
rates are 8, 16, 32 and 48 kHz. Source/codec epochs and large discontinuities
reset NetEq; device changes close its instance. Protocol 1 and WPPConnect's
audio/control contract stay compatible.

## Validation and follow-up

- Native tests cover loss/recovery, sustained bursts, acceleration, arbitrary
  device periods, strided multichannel writes and ABI bounds.
- Go tests cover source timestamps, wrap, bounded queues and legacy handoff.
- Python tests cover source resets, callback ownership, optional loading,
  negotiation and fallback; the compiled DLL is tested without devices.
- Real calls and hardware acceptance remain necessary. Full encoded-packet
  NetEq/custom codec integration and native WASAPI/APM are separate work.

## Amendment (2026-11-26): optional download

The maintainers asked (#768) that the component be an optional
per-component download, like the local AI models, so a light WinZapp need not
carry it. The loader now also accepts one more fixed path: the copy
downloaded from Settings > Calls into `global_dir("call_components")`, from a
`neteq-*` release of this repository pinned by tag, exact size and SHA-256
(`client/core/call_neteq_component.py`). It is hashed before every load and
tried before the bridge-folder copy; PATH is still never searched and no path
comes over HTTP. The installer still bundles the DLL until the maintainers
decide to drop it.

Sources: [pinned crate](https://docs.rs/crate/neteq/1.0.1),
[WebRTC baseline](https://webrtc.googlesource.com/src/+/6341cca49ffcad6fcb4603bd6cd1325fd0fe28bd/api/neteq/neteq.h).
