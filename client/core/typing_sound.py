"""Stoppable typing cue with fallback for missing or undecodable custom files."""
from core.sound_system import NullSound, load_sound, resolve_sound_event_path


def load_typing_sound(system, active, default, override, pack_id):
    override = override if isinstance(override, str) else ""
    tried = set()
    for pack, fallback, path in ((active, default, override), (active, default, ""), (default, None, "")):
        resolved = resolve_sound_event_path(pack, fallback, "typing", path)
        if not resolved or resolved in tried:
            continue
        tried.add(resolved)
        sound = load_sound(system, resolved, event_key="typing", pack_id=pack_id,
                           allow_device_recovery=False)
        if not isinstance(sound, NullSound):
            return sound
    return NullSound()
