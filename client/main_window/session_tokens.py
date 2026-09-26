"""SessionTokensMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import json
import logging
import os
import requests
import shutil
import sys
import threading
import wx
from app_paths import (
    accounts_root,
    data_path,
)
from core.api_client import api_post
from traceback import format_exc
from core import token_vault


class SessionTokensMixin:
    """WA_token vault access, the session store and cleanup of abandoned sessions.
    """

    def _token_key(self) -> bytes:
        """Return the per-install Fernet key (data_path()/secret.key) that
        backs token_vault.py, loading it lazily if needed.

        retrieve_token() (and therefore _get_wa_token()/_set_wa_token()) runs
        early in __init__, before prepare_sync() normally sets self.key —
        retrieve_secret_key() is idempotent (creates the file on first call,
        otherwise just reads it), so calling it here too is harmless; it just
        means whichever of the two call sites runs first is the one that
        actually creates the key file.
        """
        if not getattr(self, "key", None):
            self.key = self.retrieve_secret_key()
        return self.key

    def _get_wa_token(self) -> str:
        """Read the WPPConnect session token, transparently migrating a
        legacy plaintext copy (settings["privateinfo"]["WA_token"]) to
        Fernet-protected storage (settings["privateinfo"]["WA_token_protected"],
        see core/token_vault.py) the first time it's read.

        A value that fails to decrypt (corrupted, or encrypted under a
        different secret.key — e.g. settings.json copied without it) is
        treated exactly like "no token saved": retrieve_token() already
        handles that by showing the pairing dialog again, never a crash.
        """
        pi = self.settings.get("privateinfo", {})
        protected = pi.get("WA_token_protected", "")
        if protected:
            token = token_vault.unprotect_token(protected, self._token_key())
            if token:
                return token
            # Falls through to the legacy field below only so a token that
            # somehow still has a plaintext copy alongside a now-unreadable
            # protected one isn't lost — normally these are mutually exclusive.
        legacy = pi.get("WA_token", "").strip()
        if legacy:
            # One-time migration: re-save protected, remove the plaintext copy.
            self._set_wa_token(legacy)
        return legacy

    def _set_wa_token(self, token: str):
        """Write the WPPConnect session token, Fernet-protected with the
        per-install secret.key (see core/token_vault.py). Falls back to
        plaintext only if encryption genuinely fails for some reason — still
        functional, just not the hardened path. token="" clears both the
        protected and legacy fields.
        """
        pi = self.settings.setdefault("privateinfo", {})
        if not token:
            pi.pop("WA_token_protected", None)
            pi["WA_token"] = ""
            self.save_settings()
            return
        try:
            pi["WA_token_protected"] = token_vault.protect_token(token, self._token_key())
            pi.pop("WA_token", None)  # never leave a plaintext copy lying around
            self.save_settings()
        except Exception as e:
            logging.warning("[_set_wa_token] Token protection failed, falling back to plaintext: %s", e)
            pi["WA_token"] = token
            self.save_settings()
        # Track the committed session in this account's SessionStore: the new
        # token becomes our ACTIVE session; any previously-active session of
        # OURS that differs is superseded → abandoned (safe for us to close
        # later). This never touches other accounts' sessions (plan Zad 3.2).
        try:
            store = self._get_session_store()
            if store is not None:
                new_name = token.replace("/", "_").replace("+", "-").split(":")[0]
                # Under sessions_lock: register/abandon must be atomic w.r.t. the
                # shared-userDataDir cleanup so it can never delete a dir we're
                # activating (GPT r2/r3). NEVER write outside the lock — a
                # side-write reopens the TOCTOU and can corrupt sessions.json.
                # On lock timeout we skip the store update (the session still
                # works; the store is reconciled on a later register/startup).
                from coord_locks import sessions_lock, LockTimeout
                gd = getattr(self, "global_dir", None)
                def _commit():
                    for s in store.list():
                        if s.get("status") == "active" and s.get("name") != new_name:
                            store.set_status(s["name"], "abandoned")
                    store.register(new_name, token=token, status="active")
                if gd:
                    try:
                        with sessions_lock(gd):
                            _commit()
                    except LockTimeout:
                        logging.warning("[sessions] sessions_lock busy — deferring active-"
                                        "session store update (will reconcile later); NOT "
                                        "writing without the lock")
                else:
                    _commit()
        except Exception:
            logging.exception("[sessions] registering active session failed (non-fatal)")

    def _register_abandoned_session(self, token: str) -> None:
        if not token:
            return
        try:
            store = self._get_session_store()
            if store is None:
                return
            name = token.replace("/", "_").replace("+", "-").split(":")[0]
            if not name:
                return
            existing = store.get(name)
            if existing is not None and existing.get("status") == "active":
                logging.info(
                    "[sessions] not abandoning %s — the store still holds it as "
                    "active (a reused, possibly live session)", name[:12],
                )
                return

            from coord_locks import sessions_lock, LockTimeout
            gd = getattr(self, "global_dir", None)

            def _commit():
                store.register(name, token=token, status="abandoned")
                logging.info(
                    "[sessions] registered failed pairing session %s as abandoned "
                    "so it can be deregistered and its userDataDir reclaimed",
                    name[:12],
                )

            if gd:
                try:
                    with sessions_lock(gd):
                        _commit()
                except LockTimeout:
                    logging.warning(
                        "[sessions] sessions_lock busy — could not record "
                        "abandoned session %s", name[:12],
                    )
            else:
                _commit()
        except Exception:
            logging.exception(
                "[sessions] recording an abandoned session failed (non-fatal)"
            )

    def _abandon_closed_session(self, token: str) -> None:
        """Mark a session we have just deliberately CLOSED as abandoned in
        this account's SessionStore.

        Distinct from _register_abandoned_session() above, which deliberately
        refuses to touch an entry the store still holds as 'active' (it is
        meant for pairing attempts that failed, where an active entry means a
        reused, possibly live session it must not disturb). Here the opposite
        is true: we sent /close-session ourselves, so the 'active' entry is
        precisely the one that has to go.

        Leaving it 'active' is what makes _recover_active_session_token()
        unsafe — a session the user closed on purpose would come back as the
        single "unambiguous" candidate on the next launch, and the app would
        start attached to a session that is already dead instead of showing
        the pairing dialog.
        """
        if not token:
            return
        try:
            store = self._get_session_store()
            if store is None:
                return
            name = token.replace("/", "_").replace("+", "-").split(":")[0]
            if not name or store.get(name) is None:
                return

            from coord_locks import sessions_lock, LockTimeout
            gd = getattr(self, "global_dir", None)

            def _commit():
                store.set_status(name, "abandoned")
                logging.info(
                    "[sessions] marked closed session %s as abandoned", name[:12],
                )

            if gd:
                try:
                    with sessions_lock(gd):
                        _commit()
                except LockTimeout:
                    logging.warning(
                        "[sessions] sessions_lock busy — could not mark closed "
                        "session %s as abandoned", name[:12],
                    )
            else:
                _commit()
        except Exception:
            logging.exception(
                "[sessions] marking a closed session as abandoned failed (non-fatal)"
            )

    def _recover_active_session_token(self) -> str:
        """Recover a lost WA_token reference from this account's own
        SessionStore, when exactly one active, decryptable entry exists to
        recover it from.

        `paired=True` with an empty/absent token can happen while the
        underlying WPPConnect session, its Chrome userDataDir profile, and
        its SessionStore entry are all still completely intact — reported
        live (issue #155): a session that worked normally for an entire run
        showed the pairing dialog again on the very next launch, with
        sessions.json still listing that session as active and its
        token_enc still decrypting successfully. _set_wa_token("") clears
        only the settings.json reference (see that method) — it was never
        the SessionStore's job to track that, so a caller clearing the
        reference alone (connect.py's on_dialog_close()/
        on_quit_from_connect(), before they learned to leave a currently
        connected session alone) leaves this exact, recoverable state
        behind.

        Deliberately narrow: only restores when the store leaves no
        ambiguity — exactly one 'active' entry, and its token decrypts
        under this account's own secret.key. No entries, several, or one
        that fails to decrypt are all left alone; the normal pairing flow
        is the correct, safe fallback for a state this cannot resolve on
        its own, and guessing among several candidates could just as
        easily hand back the wrong session.
        """
        if not self.settings.get("privateinfo", {}).get("paired"):
            # An account that never finished pairing has nothing to recover:
            # any 'active' entry it owns belongs to an attempt that never
            # became a usable session. Enforced here rather than only at the
            # call site so the contract in the docstring above cannot be lost
            # by a future second caller.
            return ""
        store = self._get_session_store()
        if store is None:
            return ""
        try:
            active = [s for s in store.list()
                      if s.get("status") == "active" and s.get("token")]
        except Exception:
            logging.exception("[token-recovery] Failed to read the session store")
            return ""
        if len(active) != 1:
            return ""
        token = active[0]["token"]
        logging.warning(
            "[token-recovery] paired=True with no saved token, but exactly "
            "one active, decryptable session was found in the store — "
            "restoring it instead of asking to pair again."
        )
        self._set_wa_token(token)
        return token

    def _session_crypto(self):
        """Adapter exposing .encrypt/.decrypt over token_vault + this account's
        secret.key, for SessionStore (per-account WPPConnect session isolation)."""
        key = self._token_key()

        class _C:
            def encrypt(self, s: str) -> str:
                return token_vault.protect_token(s, key)

            def decrypt(self, s: str) -> str:
                v = token_vault.unprotect_token(s, key)
                if not v:
                    raise ValueError("decrypt failed")
                return v

        return _C()

    def _get_session_store(self):
        """Return this account's SessionStore (per-account sessions.json),
        creating it lazily. None in legacy single-account mode (no account dir).

        This is the backbone of session isolation (plan Zad 3.2): each account
        only ever closes WPPConnect sessions it can PROVE are its own AND
        abandoned — never another account's live session, never the current one.
        """
        store = getattr(self, "_session_store", None)
        if store is not None:
            return store
        try:
            import session_store
            acc_dir = os.path.dirname(data_path("settings.json"))
            self._session_store = session_store.SessionStore(acc_dir, self._session_crypto())
            return self._session_store
        except Exception:
            logging.exception("[sessions] SessionStore init failed (non-fatal)")
            return None

    def retrieve_token(self):
        token = self._get_wa_token()
        if not token:
            # Migration: read from legacy token.tk if WA_token not yet present
            try:
                with open(data_path("token.tk"), "r", encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    self._set_wa_token(token)
            except Exception:
                pass
        if token and ":" not in token:
            try:
                url = f"{self.wpp_server}:{self.wpp_port}/api/{token}/{self.wpp_api_key}/generate-token"
                import requests
                response = api_post(url, timeout=10)
                if response.status_code in (200, 201):
                    data = response.json()
                    hash_token = data.get("token")
                    if hash_token:
                        hash_token = hash_token.replace("/", "_").replace("+", "-")
                        token = f"{token}:{hash_token}"
                        self._set_wa_token(token)
            except Exception as e:
                logging.error("[retrieve_token] Failed to migrate WPPConnect token: %s", e)
        if not token:
            if self.background_mode:
                # No token means WhatsApp has never been paired — exit silently.
                sys.exit(0)
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('token_retrieval_failed')} {format_exc()}", self.i18n.t("error").format(app_name=self.app_name), wx.OK | wx.ICON_ERROR)
            sys.exit()
        self.token = token.replace("/", "_").replace("+", "-")
        # Seed this account's SessionStore with the current (migrated/existing)
        # session so isolation logic knows it's ACTIVE and ours (plan Zad 3.2).
        try:
            store = self._get_session_store()
            if store is not None and self.token:
                store.ensure_from_legacy_token(self.token.split(":")[0], self.token)
        except Exception:
            logging.exception("[sessions] seeding session store failed (non-fatal)")
        if self._startup_token_tail_done:
            logging.info("[retrieve_token] Token refreshed; startup audit and "
                         "session cleanup already done this launch — skipping.")
            return
        self._startup_token_tail_done = True

        # Persistent audit: which session are we starting with, and what does the
        # store think of it + every sibling. If a working session silently turned
        # 'abandoned' and a fresh (unpaired) one took over between quit and this
        # launch, THIS line proves it across the log truncation.
        # Computed before the audit block, and separately, so a diagnostic can
        # never take the STARTUP line down with it. That line is the anchor of
        # every cross-launch diagnosis this file exists for.
        try:
            login_store = self._login_store_fingerprint()
        except Exception:
            login_store = "unknown"
        try:
            store = self._get_session_store()
            listing = []
            if store is not None:
                for s in store.list():
                    listing.append(f"{s.get('name','?')[:12]}={s.get('status','?')}")
            self._shutdown_audit(
                f"STARTUP account={getattr(self,'account_id','?')} "
                f"active_session={self.token.split(':')[0]!r} "
                f"paired={self.settings.get('privateinfo',{}).get('paired')} "
                f"store=[{', '.join(listing)}] "
                f"login_store={login_store}")
        except Exception:
            pass

        # Reclaim disk from superseded sessions: each re-pair marks the old
        # WPPConnect session 'abandoned' but nothing ever deleted its userDataDir
        # (60-100 MB of Chrome profile each), so they piled up unbounded. Clean
        # them now, at startup, when they are provably not in use.
        self._cleanup_abandoned_sessions()

    def _cleanup_abandoned_sessions(self):
        """Delete this account's superseded ('abandoned') WPPConnect sessions:
        logout in Node (best-effort), remove the userDataDir, drop the store row.

        Runs on a worker thread (never the wx UI thread): deleting ~1 GB plus a
        run of HTTP timeouts must never freeze the UI — especially harmful for a
        screen-reader user (GPT r1 #a).
        """
        threading.Thread(target=self._cleanup_abandoned_sessions_worker,
                         name="session-cleanup", daemon=True).start()

    def _protected_session_names(self) -> set:
        """Every session name that is active/pairing in ANY account's
        sessions.json. userDataDir is SHARED across accounts (it lives next to
        the exe, not per-account), so before deleting a dir we must be sure no
        other account is using that name — not just our own store (GPT r1 #4/#5).

        FAIL-CLOSED: if ANY sessions.json cannot be read/parsed, raise — the
        caller aborts the whole cleanup rather than delete on an incomplete
        protected set (GPT r2). Call under sessions_lock so the set can't change
        under us between here and the rmtree.
        """
        protected = set()
        acc_root = accounts_root()
        for acc_id in os.listdir(acc_root):
            sj = os.path.join(acc_root, acc_id, "sessions.json")
            if not os.path.isfile(sj):
                continue
            # No try/except: a corrupt/unreadable sessions.json must abort the
            # cleanup (fail-closed), not silently yield a partial protected set.
            with open(sj, "r", encoding="utf-8") as f:
                data = json.load(f)
            for s in data.get("sessions", []):
                if s.get("status") in ("active", "pairing") and s.get("name"):
                    protected.add(s["name"])
        return protected

    def _cleanup_abandoned_sessions_worker(self):
        import connection_state as cs
        from coord_locks import sessions_lock, LockTimeout
        # Custom API: the user's own external WPPConnect server owns and
        # manages userDataDir sessions — its session registry is not our
        # per-account sessions.json, so we cannot PROVE which session is
        # abandoned without risking another account's live session (observed:
        # pairing a 2nd account on a custom API tore down the 1st account's
        # browser via this cleanup). Let the external server manage its own
        # sessions; never clean under custom API (plan Zad 3.2 + user config).
        if getattr(self, "wpp_custom_api", False):
            logging.info("[sessions] custom API active — skipping abandoned-session cleanup")
            return
        try:
            import session_store
            store = self._get_session_store()
            if store is None:
                return
            current = (getattr(self, "token", "") or "").split(":")[0]
            if not current:
                return  # no confirmed current session — delete nothing (GPT r1 #a)
            gd = getattr(self, "global_dir", None)
            if not gd:
                return
            # Matches _start_wpp_background()'s WINZAPP_USER_DATA_DIR, not
            # resource_path("api", "userDataDir") — the latter is a
            # --onefile launch's ephemeral extraction temp dir, already gone
            # by the time this runs, so cleanup would silently find nothing.
            udd_root = os.path.abspath(os.path.join(gd, "api", "userDataDir"))
            node_down = False  # circuit breaker: stop retrying logout once refused
            # Whole scan -> validate -> rmtree runs under the shared sessions_lock
            # so no other account can register/activate a name mid-cleanup, and
            # the protected set is rebuilt HERE (fresh, under the lock) — closing
            # the TOCTOU window on the shared userDataDir (GPT r2).
            try:
                with sessions_lock(gd):
                    try:
                        protected = self._protected_session_names()  # fail-closed
                    except Exception:
                        logging.exception("[sessions] a sessions.json is unreadable — "
                                          "aborting cleanup (fail-closed)")
                        return
                    protected.add(current)
                    stale = session_store.sessions_to_close(store.list(), current)
                    for s in stale:
                        name = s.get("name") or ""
                        if not name or name in protected:
                            continue  # never a current/active/pairing name of any account
                        # Defense-in-depth: never delete the directory of THIS
                        # process's live session, even if the store lost track
                        # of it (e.g. sessions.json missing/corrupt mid-run).
                        # Deleting it would kill our own WhatsApp page.
                        if current and (name == current or name.startswith(current + ":")):
                            continue
                        target = cs.safe_session_dir_to_delete(udd_root, name)
                        if not target:
                            logging.warning("[sessions] refusing unsafe session name for cleanup")
                            continue
                        try:
                            node_down = not self._logout_abandoned_session(
                                name, token=s.get("token"), skip=node_down)
                            # Delete then CONFIRM gone before dropping the store row.
                            # No ignore_errors: a failed delete (e.g. a lock) leaves
                            # the 'abandoned' record so we retry next start (GPT r1 #2/#c).
                            if os.path.isdir(target):
                                shutil.rmtree(target)
                            if os.path.exists(target):
                                logging.warning("[sessions] userDataDir still present after "
                                                "delete — keeping record for retry")
                                continue
                            store.remove(name)
                            logging.info("[sessions] cleaned abandoned session %s", name[:12])
                        except Exception:
                            logging.exception("[sessions] cleanup of abandoned %s failed "
                                              "(kept for retry)", name[:12])
            except LockTimeout:
                logging.info("[sessions] sessions_lock busy — skipping cleanup this start")
        except Exception:
            logging.exception("[sessions] _cleanup_abandoned_sessions failed (non-fatal)")

    def _logout_abandoned_session(self, name: str, token: str = None,
                                  skip: bool = False) -> bool:
        """Best-effort logout by SESSION NAME (never the token — a token in the
        URL can be mis-parsed and leak to logs; the token goes in Bearer only,
        GPT r1 #1). Returns True if Node is reachable (so the caller's circuit
        breaker keeps trying), False on connection-refused (Node down — skip the
        """
        if skip or not name:
            return not skip
        if not token:
            logging.info(
                "[sessions] no stored token for %s — cannot deregister it at "
                "WhatsApp; removing its local profile only", name[:12],
            )
            return True
        signature = token.split(":", 1)[1] if ":" in token else token
        try:
            api_post(
                f"{self.wpp_server}:{self.wpp_port}/api/{name}/logout-session",
                headers={"Authorization": f"Bearer {signature}"}, timeout=5,
            )
            return True
        except requests.exceptions.ConnectionError:
            logging.info("[sessions] Node not reachable — skipping further logouts")
            return False
        except Exception:
            return True  # unknown/other error: Node likely up, keep trying others
