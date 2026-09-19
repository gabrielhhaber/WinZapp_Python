---
paths:
  - "client/core/accessible_speech.py"
  - "client/core/focus_cloak.py"
  - "client/ui/conversations.py"
  - "client/status_panel.py"
---

# Screen reader speech

**Read `docs/traps/screen-reader-speech.md` before changing these files.** Short form:

Suppressing a focus announcement is done by the focus cloak (MSAA state without FOCUSED, disarmed after ~500 ms), never by cancelling speech. A list row must not be rewritten while focus is moving off it — hold repaints and release them when the sequence ends.
