# Neonize call audio

The bridge keeps its public PCM contract: microphone input is s16le mono
48 kHz; the validated per-call native source/sink is mono 16 or 48 kHz in
60 ms frames. A 48 kHz PCM contract alone says nothing about encoded speech
bandwidth: the ordinary MLow encoder still uses its supported 16 kHz path.
These source fixes do not establish parity with Telegram or Signal.

## Answer and stream ownership

Native `Active`/`ready` can arrive during `Answer`, before its return. Audio
reader ownership is independent of the state-change notification, so answering
still starts exactly one reader. Relay readiness alone cannot enable a ringing
call's microphone. A reader returning after end/rejoin cannot emit its old PCM
or release the replacement reader's claim. Microphone FIR history and partial
frames belong to one call record and are cleared on ownership changes.

A reentrant lifecycle gate orders model transitions, terminal events, PCM
emissions and microphone writes. Native audio reads stay outside that gate;
returning PCM is rechecked inside it. Terminal removal precedes emission, so
a callback rejoining the same ID cannot have its replacement removed. No model
mutex is held across the transport callback.

## Socket media ownership and ingress

New paired client/bridge versions use `mediaProtocol=1`. Each bridge call
record publishes a nonsecret random `mediaEpoch` in `callstate`, and includes
it on remote PCM/JPEG. Client microphone and camera/control envelopes carry
that owner. Microphone sessions additionally carry an increasing `streamEpoch`;
camera frame/stop/start use the capture's `epoch`. Call ID alone is insufficient
because the same ID can be rejoined. No person, key, raw audio or payload bytes
are added to diagnostic logs.

An audio session binds once. Conflicting owner changes are rejected, not used
to relabel its queued or in-flight audio. The first unbound-to-bound transition
clears unowned queues/reservoir and resets conversion history; captured/playout
units retain their original tag for a check after processing. End/rejoin creates
a new call record and audio session. Matching-ID state events with a conflicting
bound token are ignored, and the last 50 retired tokens cannot resurrect a call.
This is a fail-closed ownership policy, not automatic recovery from a missing
terminal event or a new native generation with no observed transition.

Socket handlers only enqueue. Audio and video have separate FIFO workers,
bounded count/bytes and 200 ms frame deadlines; deadline/record ownership is
checked again after waiting for native lifecycle work. The model publishes
the current owner reference while locked, so ingress never waits for that lock
and stop controls are retained. A camera start cannot erase/undo stop of the
same or a newer capture. Starting a newer capture invalidates the old encoder.
ffmpeg construction does not hold the audio lifecycle gate. Native calls already
in progress are not forcibly interrupted by a queue deadline.

Legacy envelopes remain accepted on an unbound legacy call. Once that call's
microphone binds protocol 1, untagged input is refused. WPPConnect and older
Neonize peers retain their legacy contract; the new ownership guarantees must
not be claimed for an endpoint that does not exchange these tags. They also do
not prove speech intelligibility, physical output routing, or installed acceptance.

## PCM and clocks

`call_audio_pcm.py` uses a 97-tap streaming Blackman FIR before 3:1 decimation
(1 ms group delay). It preserves sample count, packet-boundary continuity and
speech level; it adds no AGC, compressor or noise suppression. Synthetic tests
measure passband level and alias attenuation. The existing client processing
and echo cancellation remain responsible for capture/playback processing.

Native `callPCMQueue` retains multi-frame decoder output across short C reads
instead of dropping bytes beyond the caller's capacity. Capacity is eight
60 ms frames; overload drops oldest samples. Microphone capacity is three
frames (180 ms); a missing capture frame yields on-time silence instead of
blocking a 60 ms sender tick for 150 ms.

## Negotiated codec and packet handling

Absent settings still select MLow. Explicit `use_mlow_codec_v1=false` selects
Opus for the real encoder and decoder, not just a log label. Both use 16 kHz
PCM. Opus encodes 60 ms SILK wideband at 24 kbit/s VBR, using Pion Opus pinned
to `v0.1.1-0.20261026214112-e7b8aa55508c` (MIT); no new native codec DLL is
installed. Opus RTP timestamps advance at 48 kHz (2880 per 60 ms), while MLow
uses 16 kHz. Receive timestamps are converted by accumulated deltas, including
uint32 wrap and fractional carry. Reception statistics retain the wire clock.

Both media directions read the authoritative settings at packet boundaries
under one codec mutex. Late accept settings therefore replace encoder and
per-participant decoder/reorder state and change the RTP stride together,
without replacing SRTP keys, sequence or ROC. There is no startup-hook window
that can lose an update. Old decoded PCM remains valid PCM; the direct playout
buffer resets when the new timestamp origin arrives. Authentication diagnostics
describe each wire arrival once; concealed PCM is labelled separately.

Only SRTP-authenticated packets enter a per-participant bounded reorder queue.
Late/duplicate packets never reach the stateful decoder twice. A missing
predecessor is released when another packet arrives after 60 ms, or the queue
reaches three packets; there is no timer that invents packets while idle.
Opus uses decoder PLC for gaps up to 120 ms before decoding the following
packet. The MLow port has no PLC API: its fallback is a short decay to silence,
not FEC or reconstructed speech. Large discontinuities retain the existing
playout reset. Group rosters use the negotiated codec for current and future
receivers; existing SRTP keys, ROC handling and participant gates are preserved.

## Evidence and limits

### Output stream callback ownership

The common Python call-output path also fences each opened OutputStream's
callback, independently of call/media ownership. Before stop/reopen, its owner
is retired under the reservoir's existing lock. Each callback carries its own
owner, rate and echo-reference tap; owner validation and sample consumption
are atomic under that same lock. A callback delayed before fill cannot drain
a replacement stream. One already past fill can finish only into its old tap.
No callback takes the output-swap lock, logs or reopens a device. Same-rate
reopen keeps the adaptive target and cumulative underrun/drop counts; a new
rate resets the rate-specific learning as before. Candidate/start failures
retire their owners too. Optional reference-copy/append errors leave rendered
audio intact; required fill errors still produce silence.

Fake stream tests reproduce failed close, delayed callbacks, rate changes,
start/candidate failure and reference failure. This does not force a broken
driver to release its device or establish the root cause of a reported call.
The policy applies to the shared Python output path of both call engines.

### Content-free Python/native boundary measurements

The bridge logs `call audio signal summary` at most once per five seconds
when media arrives, plus the pending window on an observed terminal event.
There is no idle timer and no guaranteed final report after a process crash.
Windows belong to the exact call record; an ended/rejoined ID gets a new one.
Existing `call audio delivery counters` remain cumulative and flat.

The two fixed stages are `mic_native_accepted` (after a successful native
write return) and `remote_native_pcm` (after owner/rate/generation validation,
before emission to the client). The first means the native layer accepted a
buffer, not that it encoded or sent it. The second means a native PCM buffer
exists, not that a codec decoded intelligible speech or a speaker played it.

Each window stores numeric aggregates only: valid/invalid frames, samples,
duration, rates, all-zero frames, signed16 full-scale boundary sample count,
RMS, per-frame-centered AC RMS, RMS of frame means (DC), and peak in dBFS.
AC/DC powers are weighted by sample count, including a mixed-rate window;
duration is calculated separately at each rate. Opposite-sign constant frames
remain DC. No samples gives null levels; valid digital silence gives -120 dBFS.
Full-scale boundaries alone do not prove clipping. Changing signal, level or
nonzero AC does not prove speech, intelligibility, correct routing or audibility.

Measurements accept nonempty contiguous s16le mono at 16/48 kHz, at most
500 ms per frame. Invalid or failed measurements do not reject/change audio;
their numeric counts are separate. No audio, packet contents, identifiers,
keys or exception detail is retained in these summaries. Reporting resets the
signal window, not delivery counters. Generated-input tests check analytic
levels, invalid input, record ownership, report windows and failure isolation;
they do not execute a native codec, device or live call.

Tests use generated PCM, fake call events and synthetic authenticated RTP.
They cover answer ordering, end/rejoin ownership, FIR packet splits and
stopband, multi-frame C reads, clipping, queue bounds, codec output, packet
order/loss/duplicates and sequence/timestamp wrap. Where FFmpeg is available,
an independent libopus decoder checks an Ogg stream made by the call encoder.
PR CI also builds the Windows DLL and runs the bridge suite against that build.

These checks are source, synthetic audio and build evidence. They do not
measure real network quality, hardware latency, microphone/speaker behavior,
echo or end-to-end call acceptance. A published alpha and live user acceptance
are separate gates; no codec is forced against WhatsApp negotiation.
