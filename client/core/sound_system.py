#WinZapp's Sound System Module

import ctypes
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
import wx
import sound_lib, sound_lib.output
from sound_lib import stream
from sound_lib.main import bass_call
import sound_lib.main as _bass_main

from core.audio_devices import find_output_device_index, find_default_output_device_index
from core.alert_tones import (
    ALERT_TONE_COUNT, alert_tone_choice_keys, discover_alert_tone_choices,
    resolve_alert_tone_path,
)


# ── Import plugin modules (early, so their symbols are available) ─────────────
try:
    import sound_lib.external.pybassopus as _pybassopus
except Exception as _e:
    _pybassopus = None

try:
    import sound_lib.external.pybass_aac as _pybass_aac
except Exception as _e:
    _pybass_aac = None


class SoundSystem:
    def __init__(self, main_window, sound_dir):
        self.enabled = False
        self.main_window = main_window
        self.sound_dir = sound_dir
        # Friendly name of the output device Settings currently has
        # configured (""  == system default) — kept so a later playback
        # failure can be attributed to it and reported. Set by
        # apply_output_device(), read by handle_playback_failure().
        self._configured_output_device = ""
        self._warned_output_failure = False
        # Optional SEPARATE output device for one-shot UI effect sounds (the
        # Sound class), so alerts can play on a different device than voice/
        # conversation audio. None = route effects to the main output device
        # like everything else (the maintainer's original single-device model).
        # A concrete BASS device index means every effect channel is explicitly
        # routed there on play via BASS_ChannelSetDevice. Set by
        # apply_effects_device(); an empty configured name leaves it None.
        self._effects_device = None
        self._configured_effects_device = ""
        logging.info("[sound_system] sound_dir = %s (exists=%s)", sound_dir, os.path.isdir(sound_dir))

    def _load_bass_plugin(self, dll_name: str) -> bool:
        """Load a BASS plugin DLL via BASS_PluginLoad with an absolute path.

        Called after BASS Output() is initialised so both the logger and BASS
        device are ready. pybassopus/pybass_aac may import without error even
        when their internal libloader search fails silently — so we always call
        this explicitly with the real path.
        """
        candidates_dirs = []
        if getattr(sys, 'frozen', False):
            exe_dir = os.path.dirname(sys.executable)
            candidates_dirs += [exe_dir, os.path.join(exe_dir, 'lib')]
        if hasattr(sys, '_MEIPASS'):
            candidates_dirs += [sys._MEIPASS, os.path.join(sys._MEIPASS, 'lib')]
        _src_lib = os.path.join(os.path.dirname(__file__), '..', 'lib')
        candidates_dirs.append(os.path.normpath(_src_lib))

        logging.info("[sound_system] Looking for %s in: %s", dll_name, candidates_dirs)

        for d in candidates_dirs:
            path = os.path.join(d, dll_name)
            logging.info("[sound_system] Checking %s (exists=%s)", path, os.path.isfile(path))
            if not os.path.isfile(path):
                continue
            
            # Temporarily add the specific DLL directory to Windows DLL search path
            cookie = None
            if sys.platform == 'win32' and hasattr(os, 'add_dll_directory'):
                try:
                    cookie = os.add_dll_directory(d)
                except Exception as e:
                    logging.debug("[sound_system] os.add_dll_directory failed for %s: %s", d, e)

            # Keep track of current working directory to restore it later
            old_cwd = os.getcwd()
            try:
                # Change directory to where the DLL resides so dependencies like libopus-0.dll are resolved locally
                os.chdir(d)
                # Load DLL dependency search paths locally using win32 API SetDllDirectoryW if available
                try:
                    ctypes.windll.kernel32.SetDllDirectoryW(d)
                except Exception:
                    pass

                # Pre-load all potential Opus dependency DLL names if present in same directory
                for _op_name in ["libopus-0.dll", "opus.dll", "libopus.dll"]:
                    _op_dep = os.path.join(d, _op_name)
                    if os.path.isfile(_op_dep):
                        try:
                            ctypes.WinDLL(_op_dep)
                            logging.info("[sound_system] Pre-loaded Opus dependency: %s", _op_dep)
                        except Exception as _e:
                            logging.debug("[sound_system] Failed pre-loading %s: %s", _op_dep, _e)

                # Pre-load the plugin DLL itself via WinDLL so Windows handles dependent symbols
                try:
                    ctypes.WinDLL(path)
                except Exception as _e:
                    logging.debug("[sound_system] WinDLL pre-load for %s: %s", path, _e)

                # Use sound_lib's internal bass instance or WinDLL
                bass_dll = getattr(_bass_main, "bass", None) or ctypes.WinDLL(os.path.join(d, "bass.dll") if os.path.isfile(os.path.join(d, "bass.dll")) else "bass.dll")
                BASS_PluginLoad = getattr(bass_dll, "BASS_PluginLoad", None)
                if not BASS_PluginLoad:
                    bass_dll = ctypes.WinDLL("bass.dll")
                    BASS_PluginLoad = bass_dll.BASS_PluginLoad
                
                # 1) Try standard UTF-8 string
                BASS_PluginLoad.restype  = ctypes.c_ulong
                BASS_PluginLoad.argtypes = [ctypes.c_char_p, ctypes.c_ulong]
                handle = BASS_PluginLoad(dll_name.encode('utf-8'), 0)
                
                # 2) Try Unicode (BASS_UNICODE = 0x80000000) with full path if relative UTF-8 failed
                if not handle:
                    BASS_UNICODE = 0x80000000
                    BASS_PluginLoad.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong]
                    handle = BASS_PluginLoad(path, BASS_UNICODE)

                # 3) Try relative path UTF-8
                if not handle:
                    BASS_PluginLoad.argtypes = [ctypes.c_char_p, ctypes.c_ulong]
                    handle = BASS_PluginLoad(path.encode('utf-8'), 0)

                if handle:
                    logging.info("[sound_system] BASS_PluginLoad OK: %s (handle=%s)", path, handle)
                    return True
                else:
                    try:
                        err = bass_dll.BASS_ErrorGetCode()
                    except Exception:
                        err = "?"
                    logging.warning("[sound_system] BASS_PluginLoad=0 for %s (BASS error=%s)", path, err)
            except Exception as _ex:
                logging.warning("[sound_system] BASS_PluginLoad exception for %s: %s", path, _ex)
            finally:
                # Restore original CWD and clean SetDllDirectoryW
                try:
                    os.chdir(old_cwd)
                    if sys.platform == 'win32':
                        ctypes.windll.kernel32.SetDllDirectoryW(None)
                except Exception:
                    pass
                if cookie:
                    try:
                        cookie.close()
                    except Exception:
                        pass
        return False

    def start(self):
        self.enabled = True
        self.output = sound_lib.output.Output()
        # Load BASS plugins AFTER Output() so BASS device is initialised
        opus_loaded = self._load_bass_plugin('bassopus.dll') or self._load_bass_plugin('bass_opus.dll')
        if not opus_loaded and _pybassopus is not None:
            try:
                if hasattr(_pybassopus, "BASS_OpusInit"):
                    _pybassopus.BASS_OpusInit()
                    opus_loaded = True
                    logging.info("[sound_system] pybassopus.BASS_OpusInit OK")
            except Exception as _e:
                logging.debug("[sound_system] pybassopus.BASS_OpusInit failed: %s", _e)

        if not opus_loaded:
            logging.warning("[sound_system] bassopus.dll not loaded — OGG Opus playback will fail")
        if not (self._load_bass_plugin('bass_aac.dll') or self._load_bass_plugin('bassaac.dll')):
            logging.warning("[sound_system] bass_aac.dll not loaded")

    def _switch_to_default_device(self):
        """Actually switch BASS back to the system default device.

        sound_lib.output.Output.set_device(-1) does NOT work for this:
        internally it calls BASS_Init(-1) (which correctly resolves -1 to
        the real default device) but then ALSO calls BASS_SetDevice(-1)
        unconditionally — and BASS_SetDevice rejects -1 outright ("illegal
        device number"; -1 is only ever valid as a BASS_Init() argument).
        That call raised every single time, silently swallowed by the
        try/except this used to wrap it in, so "switch to default" was a
        no-op that lied about succeeding: the previously-selected device
        stayed active. Free + reinit directly instead, skipping Output.
        set_device()'s own broken second call.
        """
        if self.output._device == -1:
            return
        self.output.free()
        self.output.init_device(device=-1)

    def apply_output_device(self, device_name: str, warn_on_failure: bool = False) -> bool:
        """Switch the single process-wide BASS output device to the one
        named `device_name` (every stream anywhere in the app plays through
        it — there is only one). An empty name means "system default".

        On failure the device is left on the system default (never in a
        half-freed state) and, if `warn_on_failure`, a message box names the
        device and explains playback fell back to the default. Returns
        whether the requested device is the one actually active afterwards.
        """
        self._configured_output_device = device_name or ""
        self._warned_output_failure = False
        if not device_name:
            self._switch_to_default_device()
            return True

        idx = find_output_device_index(device_name)
        ok = False
        if idx is not None:
            try:
                self.output.set_device(idx)
                ok = True
            except Exception as e:
                logging.warning(
                    "[sound_system] Failed to switch output device to '%s': %s",
                    device_name, e,
                )
        if not ok:
            self._switch_to_default_device()
            if warn_on_failure:
                self._warn_device_failure("output", device_name)
        return ok

    def _ensure_device_inited(self, device: int) -> bool:
        """BASS_Init an extra output device on demand so an effect channel can
        be routed to it (the main Output() device is always ready). Returns True
        if the device is usable. Idempotent: 'already initialised' (BASS error
        14) counts as success.

        CRITICAL: BASS_Init(device) also makes `device` BASS's *current* device,
        which every stream created afterwards inherits — so initialising the
        effects device would silently send voice/conversation streams to it too
        (bug: everything ended up on the effects device). Save the current
        device before BASS_Init and restore it after, so this only ADDS a device
        without moving where new streams land.
        """
        try:
            from sound_lib.external.pybass import BASS_Init, BASS_GetDevice, BASS_SetDevice
            try:
                prev = BASS_GetDevice()
            except Exception:
                prev = None
            try:
                bass_call(BASS_Init, device, 44100, 0, 0, None)
            except Exception as exc:
                if not ("14" in str(exc) or "already" in str(exc).lower()):
                    raise
            finally:
                # Restore the current device so we didn't hijack new streams.
                if prev is not None and prev != device:
                    try:
                        BASS_SetDevice(prev)
                    except Exception:
                        pass
            return True
        except Exception as exc:
            logging.warning("[sound_system] BASS_Init failed for effects device %s: %s", device, exc)
            return False

    def apply_effects_device(self, device_name: str, warn_on_failure: bool = False) -> bool:
        """Choose the output device for UI effect sounds and PIN effect channels
        to it so they don't follow the voice output when it's switched.

        An empty name means "system default device" — but resolved to the
        CONCRETE default-device index, not left as None. That distinction is the
        whole fix: with the maintainer's single-process-Output model, switching
        the voice output frees + re-inits that one BASS device, and effects that
        weren't pinned to a specific device rode along with it (bug: picking a
        non-default voice output dragged the effect sounds off the default
        device too, even though effects were set to "default"). Pinning effects
        to the resolved default index keeps them there regardless.

        A named device is resolved to its BASS index instead. Either way the
        device is BASS_Init'd and stored so Sound.play() routes effect channels
        to it. Returns whether a concrete device was resolved.

        This never switches the process-wide Output; it only sets up a device
        that effect channels are individually routed to, so voice/conversation
        audio keeps playing on the main output.
        """
        self._configured_effects_device = device_name or ""
        if device_name:
            idx = find_output_device_index(device_name)
        else:
            idx = find_default_output_device_index()
        if idx is not None and self._ensure_device_inited(idx):
            self._effects_device = idx
            return True
        # Couldn't resolve/init a concrete device — fall back to None (effects
        # play on the current process device, i.e. the old shared behaviour).
        self._effects_device = None
        if warn_on_failure and device_name:
            self._warn_device_failure("output", device_name)
        return False

    def _warn_device_failure(self, kind: str, device_name: str):
        """Show the "device failed to open" message box, unless running in
        the no-UI --background autostart mode."""
        if getattr(self.main_window, "background_mode", False):
            return
        i18n = self.main_window.i18n
        key = "audio_device_failed_output" if kind == "output" else "audio_device_failed_input"
        wx.CallAfter(
            wx.MessageBox,
            i18n.t(key).format(device=device_name),
            i18n.t("error").format(app_name=self.main_window.app_name),
            wx.OK | wx.ICON_WARNING,
        )

    def handle_playback_failure(self) -> bool:
        """Called when playing a BASS stream raises, on some already-active
        output device configured earlier (not the default). Falls back to
        the system default device and warns once per session — further
        failures on the same device this session stay silent so a broken
        device doesn't spam a message box on every single sound.

        BASS_Free()/BASS_Init() during that fallback invalidates every BASS
        stream that existed before it — including whichever one just failed
        to play, so the caller must not retry play() on that same stream/
        channel object (it'll raise the same "invalid handle" error again);
        it needs to reopen a fresh one from the same source file instead.
        This also reloads every cached Settings > Sound Events sound
        (main_window.load_sounds()) so those recover for future calls too,
        without needing every one of their many call sites to handle this.

        Returns True if it just performed that fallback (caller should open
        and play a brand new stream rather than retry the old one).
        """
        if not self._configured_output_device or self._warned_output_failure:
            return False
        self._warned_output_failure = True
        name = self._configured_output_device
        self._switch_to_default_device()
        try:
            self.main_window.load_sounds()
        except Exception:
            logging.exception("[sound_system] load_sounds() failed while recovering from a playback failure")
        self._warn_device_failure("output", name)
        return True



# ── Sound event registry ────────────────────────────────────────────────────
# Every one-shot UI sound the app plays, as (settings key, default filename in
# a pack's folder) pairs, listed (and individually enable/path-customizable)
# in Settings > Sound Events. message_background is included here too so it
# gets the same enable + custom-path override as any other event; the Alert
# Tones tab's "Padrão" choice (and the per-conversation "Padrão" override)
# ultimately resolve to this same event — see
# MainWindow._resolve_message_background_path().
SOUND_EVENTS: list[tuple[str, str]] = [
    ("startup", "startup.ogg"),
    ("error", "error.ogg"),
    ("spelling_error", "textError.ogg"),
    ("qrcode_loaded", "qrcode_loaded.ogg"),
    ("waiting_pairing", "waiting_pairing.ogg"),
    ("pairing_code_updated", "pairing_code_updated.ogg"),
    ("connected", "connected.ogg"),
    ("synchronizing", "synchronizing.ogg"),
    ("sync_complete", "sync_complete.ogg"),
    ("offline_mode", "offline_mode.ogg"),
    ("voicemsg_startrecording", "voicemsg_startrecording.ogg"),
    ("voicemsg_pauserecording", "voicemsg_pauserecording.ogg"),
    ("voicemsg_discard", "voicemsg_discard.ogg"),
    ("voicemsg_send", "voicemsg_send.ogg"),
    ("message_current", "message_current.ogg"),
    ("message_foreground", "message_foreground.ogg"),
    ("message_background", "message_background.ogg"),
    ("call_incoming", "call_incoming.ogg"),
    ("message_sent", "message_sent.ogg"),
    ("audio_transition_next", "audio_transition_next.ogg"),
    ("audio_transition_end", "audio_transition_end.ogg"),
]

# ── Soundpacks ───────────────────────────────────────────────────────────────
# A soundpack is a subfolder of client/sounds/ containing a *.pack.json
# manifest ({"name": ..., "events": {event_key: relative_path}, "alerts":
# {alert_key: relative_path}}) plus the .ogg files it references. Paths in
# the manifest are always relative to the pack's own folder — never absolute,
# since an absolute path baked in at manifest-authoring time would point at
# the wrong install location on a different machine.
PACK_MANIFEST_SUFFIX = ".pack.json"
DEFAULT_PACK_ID = "default"


def _find_pack_manifest(folder: str) -> "str | None":
    """Return the path to the *.pack.json manifest directly inside `folder`."""
    try:
        for name in os.listdir(folder):
            if name.lower().endswith(PACK_MANIFEST_SUFFIX):
                candidate = os.path.join(folder, name)
                if os.path.isfile(candidate):
                    return candidate
    except OSError:
        pass
    return None


def _load_pack(pack_id: str, folder: str) -> "dict | None":
    manifest = _find_pack_manifest(folder)
    if not manifest:
        return None
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        "id": pack_id,
        "name": str(data.get("name") or pack_id),
        "dir": folder,
        "events": data.get("events") if isinstance(data.get("events"), dict) else {},
        "alerts": data.get("alerts") if isinstance(data.get("alerts"), dict) else {},
    }


def discover_sound_packs(sounds_root: str) -> dict:
    """Scan sounds_root for immediate subfolders containing a *.pack.json
    manifest. Returns {pack_id: pack_dict}, pack_id being the folder name."""
    packs: dict = {}
    try:
        entries = os.listdir(sounds_root)
    except OSError:
        return packs
    for entry in entries:
        folder = os.path.join(sounds_root, entry)
        if not os.path.isdir(folder):
            continue
        pack = _load_pack(entry, folder)
        if pack:
            packs[entry] = pack
    return packs


def _pack_relative_file(pack: dict, rel_path) -> str:
    """Join a pack-relative path onto the pack's dir. Returns '' if the path
    is missing or (a corrupted/hand-edited manifest) absolute — a manifest
    must never carry an absolute path, so one is treated as not resolvable
    rather than trusted."""
    if not rel_path or not isinstance(rel_path, str) or os.path.isabs(rel_path):
        return ""
    pack_dir = os.path.abspath(pack["dir"])
    candidate = os.path.abspath(os.path.join(pack_dir, rel_path))
    try:
        if os.path.commonpath((pack_dir, candidate)) != pack_dir:
            return ""
    except ValueError:
        return ""
    return candidate


def resolve_sound_event_path(active_pack, default_pack, event_key: str, override_path: str = "") -> str:
    """Resolve the file to play for a Sound Events entry, in priority order:
    1. `override_path` (a per-event custom path the user set), if it exists.
    2. The active pack's own file for this event.
    3. The default pack's file for this event — covers packs that don't
       define every event, or a broken/hand-edited settings.json leaving an
       empty/stale override path. This is the fallback the previous
       (non-pack-aware) implementation was missing: an empty or invalid
       stored path used to silently produce no sound instead of falling
       back to the bundled default.
    Returns '' if nothing resolves at all (caller falls back to NullSound).
    """
    if override_path and os.path.isfile(override_path):
        return override_path
    if active_pack:
        p = _pack_relative_file(active_pack, active_pack.get("events", {}).get(event_key, ""))
        if p and os.path.isfile(p):
            return p
    if default_pack and default_pack is not active_pack:
        p = _pack_relative_file(default_pack, default_pack.get("events", {}).get(event_key, ""))
        if p and os.path.isfile(p):
            return p
    return ""


def validate_soundpack_folder(folder: str):
    """Check that `folder` is a valid, importable soundpack.

    Returns (ok: bool, error_i18n_key: str, parsed: dict|None). On success
    error_i18n_key is '' and parsed has 'name'/'events'/'alerts' (paths still
    relative to `folder`, not yet pointed at the copied destination).
    """
    if not folder or not os.path.isdir(folder):
        return False, "soundpack_import_error_no_folder", None

    manifest = _find_pack_manifest(folder)
    if not manifest:
        return False, "soundpack_import_error_no_manifest", None

    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False, "soundpack_import_error_bad_manifest", None

    if not isinstance(data, dict) or not data.get("name") or not isinstance(data.get("events"), dict):
        return False, "soundpack_import_error_bad_manifest", None

    events = data.get("events") or {}
    alerts = data.get("alerts") if isinstance(data.get("alerts"), dict) else {}
    for rel_path in list(events.values()) + list(alerts.values()):
        if not isinstance(rel_path, str) or not rel_path:
            continue
        if os.path.isabs(rel_path):
            return False, "soundpack_import_error_absolute_path", None
        if not os.path.isfile(os.path.join(folder, rel_path)):
            return False, "soundpack_import_error_missing_file", None

    return True, "", {"name": str(data["name"]), "events": events, "alerts": alerts}


def import_soundpack(source_path, sounds_root: str):
    """Validate `source_path` (folder or .zip archive) as a soundpack, then
    copy/extract it into `sounds_root` as a new subfolder (named after the
    source folder, de-duplicated if one with that name already exists).

    Returns (ok: bool, error_i18n_key: str, new_pack_id: str|None).
    """
    if not source_path:
        return False, "soundpack_import_error_no_folder", None

    temp_dir_obj = None
    source_folder = source_path

    # If user selected a .zip file, extract it to a temporary folder first
    if os.path.isfile(source_path) and source_path.lower().endswith(".zip"):
        try:
            temp_dir_obj = tempfile.TemporaryDirectory()
            with zipfile.ZipFile(source_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir_obj.name)
            source_folder = temp_dir_obj.name
            # If the zip extracted a single root folder containing the pack,
            # use that subfolder.
            entries = [os.path.join(source_folder, e) for e in os.listdir(source_folder)]
            dirs = [e for e in entries if os.path.isdir(e)]
            if len(dirs) == 1 and not validate_soundpack_folder(source_folder)[0]:
                source_folder = dirs[0]
        except Exception:
            if temp_dir_obj is not None:
                try:
                    temp_dir_obj.cleanup()
                except Exception:
                    pass
            return False, "soundpack_import_error_invalid_zip", None

    ok, err_key, _data = validate_soundpack_folder(source_folder)
    if not ok:
        if temp_dir_obj is not None:
            try:
                temp_dir_obj.cleanup()
            except Exception:
                pass
        return False, err_key, None

    base_name = os.path.basename(os.path.normpath(source_folder)) or "soundpack"
    safe_name = "".join(c for c in base_name if c.isalnum() or c in ("-", "_")) or "soundpack"
    dest_id = safe_name
    suffix = 2
    while os.path.exists(os.path.join(sounds_root, dest_id)):
        dest_id = f"{safe_name}_{suffix}"
        suffix += 1

    dest_folder = os.path.join(sounds_root, dest_id)
    try:
        shutil.copytree(source_folder, dest_folder)
    except OSError:
        if temp_dir_obj is not None:
            try:
                temp_dir_obj.cleanup()
            except Exception:
                pass
        return False, "soundpack_import_error_copy_failed", None

    if temp_dir_obj is not None:
        try:
            temp_dir_obj.cleanup()
        except Exception:
            pass
    return True, "", dest_id


class NullSound:
    """Returned when a sound file can't be loaded — all methods are no-ops."""
    def play(self): pass
    def stop(self): pass


class Sound(stream.FileStream):
    def __init__(self, sound_system, file, event_key=None, pack_id=None,
                 looping=False, *args, **kwargs):
        self.sound_system = sound_system
        self.event_key = event_key
        # Which soundpack this event's enabled/path settings live under —
        # settings["sound_events"] is keyed {pack_id: {event_key: {...}}},
        # not flat, since a pack switch can carry different enabled states.
        self.pack_id = pack_id
        if os.path.isfile(os.path.join(self.sound_system.sound_dir, file)): #sound is a file on disk
            self.file = os.path.join(self.sound_system.sound_dir, file)
        else: #sound is coming from memory
            self.file = file
        if looping:
            kwargs["flags"] = kwargs.get("flags", 0) | stream.BASS_SAMPLE_LOOP
        super().__init__(*args, file=self.file, **kwargs)

    def play(self):
        super().stop()
        # Each sound event can be individually enabled/disabled from the
        # Settings > Sound Events tab. Sounds not tied to an event (e.g. the
        # background notification tone, resolved dynamically elsewhere) always
        # play — there's nothing here to gate them on.
        if self.event_key is not None and self.pack_id is not None:
            events = self.sound_system.main_window.settings.get("sound_events", {})
            pack_events = events.get(self.pack_id, {})
            if not pack_events.get(self.event_key, {}).get("enabled", True):
                return
        try:
            # Route this effect channel to the pinned effects output device
            # (None = play on the current process device, legacy behaviour).
            # Switching the voice output frees + re-inits BASS devices, so
            # re-ensure ours is initialised before routing to it; best-effort —
            # a routing failure must never silence the sound.
            dev = self.sound_system._effects_device
            if dev is not None:
                try:
                    self.sound_system._ensure_device_inited(dev)
                    self.set_device(dev)
                except Exception as exc:
                    logging.debug("[sound_system] effect set_device(%s) failed: %s", dev, exc)
            # restart=True: sound_lib's Channel.play() defaults to False,
            # which resumes from wherever BASS_ChannelStop above left the
            # read position instead of seeking back to 0. Two consecutive
            # play() calls close together (e.g. two messages arriving back
            # to back) would stop the still-playing channel and then resume
            # it near its own tail — audibly indistinguishable from a single
            # play, which is exactly the "sounds like only one message
            # arrived" bug this was reported as.
            super().play(restart=True)
        except Exception:
            # The configured output device may have gone away mid-session
            # (unplugged, disabled) after having worked fine earlier — fall
            # back to the system default. Retrying super().play() on `self`
            # would still fail: handle_playback_failure()'s BASS_Free()/
            # BASS_Init() invalidates this exact stream's BASS handle too
            # (confirmed live — same "invalid handle" error), so recovering
            # THIS particular play() call means reopening a fresh stream
            # from the same file rather than reusing `self`.
            # handle_playback_failure() itself already reloads every cached
            # Sound Events sound via main_window.load_sounds(), so future
            # calls to those (e.g. self.main_window.startup_sound) recover
            # on their own without going through this fallback again.
            if self.sound_system.handle_playback_failure():
                try:
                    stream.FileStream(file=self.file).play()
                except Exception:
                    pass


def load_sound(sound_system, file, event_key=None, pack_id=None, looping=False):
    """Create a Sound, returning NullSound if the file can't be opened."""
    try:
        return Sound(
            sound_system, file, event_key=event_key, pack_id=pack_id,
            looping=looping,
        )
    except Exception as e:
        logging.warning("[sound_system] Could not load sound '%s': %s", file, e)
        return NullSound()


class AlertPreviewController:
    """Wires a single "preview this sound" wx.Button to play/stop whatever
    file `resolve_path()` currently points at (an Alert Tones combo choice,
    a per-conversation sound choice, ...). The button's label flips between
    "play" and "stop" and flips back on its own once playback finishes —
    polled via a short wx.Timer since sound_lib has no completion callback.

    Multiple controllers can share a `group`: list (pass the same list to
    each) so starting one stops any other that's currently previewing —
    handy for the private/group pair on the Alert Tones tab, where only one
    preview at a time makes sense.
    """

    def __init__(self, main_window, button, resolve_path, group=None,
                 play_label_key="preview_sound_play", stop_label_key="preview_sound_stop"):
        self.main_window = main_window
        self.button = button
        self.resolve_path = resolve_path
        self.group = group if group is not None else [self]
        if self not in self.group:
            self.group.append(self)
        self.play_label_key = play_label_key
        self.stop_label_key = stop_label_key
        self._sound = None
        self._timer = wx.Timer(button)
        button.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        button.Bind(wx.EVT_BUTTON, self._on_click)

    def _on_click(self, event):
        if self._sound is not None:
            self.stop()
            return

        path = self.resolve_path()
        if not path or not os.path.isfile(path):
            wx.MessageBox(
                self.main_window.i18n.t("preview_sound_error"),
                self.main_window.i18n.t("error").format(app_name=self.main_window.app_name),
                wx.OK | wx.ICON_ERROR, self.button.GetTopLevelParent(),
            )
            return

        for other in self.group:
            if other is not self:
                other.stop()

        try:
            snd = Sound(self.main_window.sound_system, path)
            snd.play()
        except Exception as e:
            logging.warning("[sound_system] Preview playback failed for '%s': %s", path, e)
            wx.MessageBox(
                self.main_window.i18n.t("preview_sound_error"),
                self.main_window.i18n.t("error").format(app_name=self.main_window.app_name),
                wx.OK | wx.ICON_ERROR, self.button.GetTopLevelParent(),
            )
            return

        self._sound = snd
        self.button.SetLabel(self.main_window.i18n.t(self.stop_label_key))
        self._timer.Start(300)

    def _on_timer(self, event):
        if self._sound is None or not self._sound.is_playing:
            self.stop()

    def stop(self):
        self._timer.Stop()
        if self._sound is not None:
            try:
                self._sound.stop()
            except Exception:
                pass
            self._sound = None
        self.button.SetLabel(self.main_window.i18n.t(self.play_label_key))

    def refresh_label(self):
        """Call after a language change to relabel the button correctly for
        its current play/stop state."""
        key = self.stop_label_key if self._sound is not None else self.play_label_key
        self.button.SetLabel(self.main_window.i18n.t(key))
