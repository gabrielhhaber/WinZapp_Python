---
paths:
  - "client/core/accessible_speech.py"
  - "client/core/focus_cloak.py"
  - "client/ui/conversations.py"
  - "client/status_panel.py"
  - "client/status_tab/*.py"
  - "client/ui/conversation_panel/*.py"
  - "client/main_window/shortcuts.py"
  - "client/main_window/chat_list.py"
  - "client/main_window/chat_lock.py"
  - "client/ui/navigation.py"
---

# Screen reader speech

**Read `docs/traps/screen-reader-speech.md` before changing these files.** Short form:

Suppressing a focus announcement is done by the focus cloak (MSAA state without FOCUSED, disarmed after ~500 ms), never by cancelling speech. A list row must not be rewritten while focus is moving off it — during a voice-note sequence hold the repaints of the focused row and of the row the chain just left (only those: `core/chain_repaint_guard.py`) and release them when the sequence ends or is paused.

Switching to a chat panel (Alt+1, Alt+4, locked, navigation list) goes through `show_chat_panel()` only: a real switch from another panel hides the open conversation's pane (the conversation stays open, never reshown by a switch: that was a perceptible delay), but pressing Alt+1/Alt+4 inside the panel the conversation is already visible in switches nothing and keeps it (`keep`), focuses the panel's chat list (never a message list), and never rebuilds, loads or requests anything. The conversation returns only on an explicit ask (Alt+M, Alt+2, Alt+3: `reveal_open_conversation()`, in the panel it belongs to) or by opening a chat.
