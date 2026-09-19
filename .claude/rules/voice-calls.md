---
paths:
  - "client/core/call_audio.py"
  - "client/core/call_matching.py"
  - "client/api_patches/src/util/callMediaBridge.ts"
  - "client/api_patches/src/controller/callController.ts"
  - "client/api_patches/src/middleware/socketAuth.ts"
  - "pulse_audio_lifecycle.py"
---

# Voice calls

**Read `docs/traps/voice-calls.md` before changing these files.** Short form:

Python owns only the two ends of the audio; the camera path fails closed by three independent things. Peer JIDs arrive in different forms — compare through `call_matching.py`. The sync stand-down is bounded and lives where a round is decided; the PulseAudio block is live code for remote-API installs.
