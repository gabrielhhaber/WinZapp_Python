# Neonize call audio

The bridge keeps its public PCM contract: microphone input is s16le mono
48 kHz; the validated per-call native source/sink is mono 16 or 48 kHz in
60 ms frames. A 48 kHz PCM contract alone says nothing about encoded speech
bandwidth: the ordinary MLow encoder still uses its supported 16 kHz path.
These source fixes do not establish parity with Telegram or Signal.

## Transport boundary (issue #768, first step of tier C)

`core/call_audio_transport.py` defines `CallAudioTransport` for outgoing
mono s16le PCM and start/stop controls, and `CallAudioFrame` /
`CallAudioReceiver` for incoming PCM. `CallAudioSession` accepts an injected
transport; existing callers still get `SocketIOCallAudioTransport`. Outgoing
JSON/base64 encoding belongs to that adapter rather than the device pipeline.
The default adapter preserves the existing event names and envelopes for
both WPPConnect and Neonize, including media/stream ownership tags.

Capture, processing, muting, desktop mixing, ownership checks and the
`stop(notify_bridge=False)` device-switch policy remain in the session.
The Socket.IO receive adapter (`core/call_audio_socketio.py`) converts
base64, binary and Node Buffer envelopes to `CallAudioFrame`. Direct
transports can construct a frame from PCM without a Socket.IO envelope and
deliver it to the same `CallAudioReceiver` callback. Legacy frames retain
the two-argument callback; tagged frames carry their original `media_epoch`.
Malformed explicit tags are rejected, never treated as untagged legacy PCM.

Socket events still check account and media ownership before decoding, with
the existing rate-limited rejection counters. The selected session rechecks
ownership and stop state before queueing, so a reconnection during decoding
or a delayed callback after a device switch cannot enqueue into a different
call. Existing public audio/video handler names remain available.
No native transport is selected or downloaded, and no buffer, resampler,
WASAPI mode or audio quality setting changes.
This boundary is preparation for a native transport, not NetEQ integration
or evidence of reduced latency. Tier C's native loop and optional component
loading/fallback remain unimplemented.

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
(1 ms group delay). When `Calls.resample_pcm48_to16` is available, the FIR runs
in Go through `CallResamplePCM48To16` (neonize patch 0033). The bridge retains
the 96-sample history and incomplete capture bytes per microphone stream;
the native function is stateless and accepts at most 100 ms plus history.
Changing calls, stopping a stream or replacing it discards that history as
before. PCM48 calls bypass the converter; the native PCM rate/generation
checks and the 60 ms writer contract are unchanged.

An older binding/DLL, a failed call or an invalid native result selects the
Python implementation. Since the caller owns the history, fallback retries
the same window without dropping or replaying samples. Only the exception
type is logged, once per failed converter instance. No native codec is forced.
Both paths preserve sample count, packet-boundary continuity and speech level;
they add no AGC, compressor or noise suppression. Synthetic Go tests compare
the exact output hash with the Python reference and measure passband level
and alias attenuation. The compiled-package binding test checks the C boundary
in CI. The existing client processing and echo cancellation remain responsible
for capture/playback processing.

This removes the Python FIR loop when the matching DLL is installed. It does
not remove Socket.IO or change playback thresholds.

## One playback reservoir for direct Neonize audio

The bridge's `chamadaAtiva` queue now advertises meowcaller's optional
`BufferedAudioSink` capability (neonize 0034, meowcaller 0037). Its downstream
device callback already owns an adaptive reservoir, so decoded direct-call PCM
is delivered immediately instead of waiting for the next RTP packet and a
second native prefill. Sinks without the capability retain the original native
timestamp alignment and prefill. The group mixer retains its existing policy.

Authentication, packet ordering and codec PLC still precede delivery. The
immediate path keeps natural decoder lengths, completes a short predecessor
with bounded silence, excludes already delivered overlapping PCM, and handles
RTP wrap. It does not enqueue an elapsed network outage after a full frame.
A new receive SSRC or codec clock resets this timeline, so its fresh origin is
not mistaken for duplicate audio. Sink changes hand pending PCM over once;
flush/drain never replay PCM already sent to a buffered sink.

Python keeps its existing Neonize 120 ms prebuffer, 240 ms adaptive ceiling,
360 ms cap, fades and underrun/refill policy. No WPPConnect setting changes.
Synthetic Go schedules and the real Python reservoir show first playback at
60 ms instead of 120 ms for regular 60 ms packets. Bounded jitter and burst
scenarios preserve PCM without additional underruns; a longer outage retains
the existing starvation/refill behavior. This is local timing evidence, not a
measurement of provider, hardware or live end-to-end call latency.

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
