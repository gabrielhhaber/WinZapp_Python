"""Process-creation flags for heavy child processes that must not starve the PC.

`npm install`, the TypeScript build and the update's file copy keep every disk
and core busy for minutes. At normal priority they starved the screen reader's
speech and froze the whole machine half-way through an update (issue #564).
BELOW_NORMAL_PRIORITY_CLASS lets the screen reader, and everything the user is
doing, win the CPU; the child's own children inherit it.
"""

import subprocess


def background_creationflags(flags: int = 0) -> int:
    """`flags` plus BELOW_NORMAL_PRIORITY_CLASS (0 where the platform has none)."""
    return flags | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
