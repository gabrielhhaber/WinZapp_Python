# Experimental PCM48 call path

The default remains mono 16 kHzHz/60 ms. `WINZAPP_EXPERIMENTAL_CALL_PCM48=1` is a
developer opt-in, read before new bridge clients are constructed. It requires
the new native exports; an older module/DLL retains the legacy path.

The opt-in fixes a client's source/sink contract at 48 kHz, mono s16, 2880 samples
per 60 ms (5760 bytes). It does not force a wire codec. Negotiated standard Opus
uses the already-pinned Pion encoder's fullband 20 ms CELT API and combines three
coding frames without another lossy encode. Negotiated MLow still encodes at
16 kHz through a per-codec 97-tap anti-alias filter; inbound native MLow is filtered
up to the fixed 48 kHz PCM contract. This does not implement native 32 kHz MLow or
restore detail that a peer did not send. A negotiated MLow CELT escape can be
decoded in PCM48 mode; outbound MLow escape is not forced.

PCM rate, RTP clock and packet duration are independent. Late codec settings
replace encoder/decoder/order state at a packet boundary, preserving the SRTP
pipeline and sequence/ROC state. Group mixing and playout use the same duration
budgets at either PCM rate; 240 ms group queues and 480 ms native PCM queues are
sample-rate aware. Public built-in 16 kHz sources keep their original contract;
clients explicitly selecting `WithPCM48()` must supply 48 kHz sources/sinks.

The V2 C/Python boundary carries a little-endian 12-byte header (rate u32,
native record generation u64) with each PCM read. Writes must supply matching
rate/generation and a 60 ms frame. A universal 5772-byte read buffer returns at
most one frame at the actual rate, retaining the rest of decoder bursts.
Format is never inferred from byte count: 1920 bytes is either 16 kHz/60 ms or
48 k/20 ms. Legacy `CallReadAudio`/`CallWriteAudio` refuse a 48 kHz record.

The bridge binds an immutable format to its own call record, rejects stale
generation events/PCM, clears partial microphone capture when owners change,
and reports the actual rate to the existing client player. No second playback
worker or enhancement model is added. Native record replacement closes the old
queue; an old end callback cannot remove the replacement.

The native implementation and tests are exported through meowcaller 0007 and
neonize 0014. `test_call_pcm48.py` exercises bridge ownership/format behavior;
`test_call_pcm48_bindings.py` uses fake ctypes targets and also checks the
compiled exports without a client when that build is present. Native tests
cover codec fallback, clock wrap, late negotiation/SRTP, group buffering and
queue/envelope duration. The synthetic `TestPCM48ReferenceCELTPacket` exposes
a test tone packet for independent file-decoder checks; it is not account media.

Packet/format conformance does not establish peer interoperability, subjective
naturalness, device performance or live acceptance. Native MLow remains 16 kHz,
and PCM48 by itself cannot increase the detail a peer sends. This developer
option is not a default quality upgrade or a new user-facing settings control.
