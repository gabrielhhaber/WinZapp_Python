"""A request-owned waiting stream, stopped/freed before screen-reader output."""


class ProcessingCue:
    def __init__(self, main_window):
        self.main_window = main_window
        self._sound = None

    def start(self):
        self.stop()
        window = self.main_window
        system = getattr(window, "sound_system", None)
        if not system or not getattr(system, "enabled", False) or getattr(window, "background_mode", False):
            return
        try:
            from core.sound_system import DEFAULT_PACK_ID, load_sound, resolve_sound_event_path
            active = window.get_active_sound_pack()
            pack_id = active.get("id") if active else DEFAULT_PACK_ID
            config = window.settings.get("sound_events", {}).get(pack_id, {}).get("photo_describing", {})
            if not config.get("enabled", True):
                return
            path = resolve_sound_event_path(active, window._default_sound_pack,
                                            "photo_describing", config.get("path", ""))
            if not path:
                return
            # A private looping stream must not trigger global BASS reinit or
            # create an unowned fallback stream which could outlive this job.
            self._sound = load_sound(system, path, event_key="photo_describing", pack_id=pack_id,
                                     looping=True, allow_device_recovery=False)
            self._sound.play()
        except Exception:
            # Missing/stale audio must never cancel a photo request or expose
            # native exception messages. The readable status still works.
            self.stop()

    def stop(self):
        sound, self._sound = self._sound, None
        if sound is None:
            return
        try:
            sound.stop()
        except Exception:
            pass  # BASS may already have freed the device/handle.
        finally:
            release = getattr(sound, "free", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    pass
