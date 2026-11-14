"""The connection engine: how WinZapp reaches WhatsApp.

Three ways, all spoken to through the same WPPConnect-shaped REST/Socket.IO
API, so the rest of the client does not care which one is behind it:

* ``neonize``    -- the neonize bridge (``neonize_bridge/``), a direct
  connection without a browser. It answers on a fixed local address. The
  engine the pickers list first and preselect (still labelled experimental).
* ``wppconnect`` -- the bundled WPPConnect server (Node + a browser), labelled
  alternative. Still what an install with no stored choice runs on, and the
  fallback wherever Neonize has no bridge installed.
* ``custom``     -- any WPPConnect-compatible server the user types in.

``connection.wpp_custom_api`` stays the switch the rest of the client reads
(``neonize`` and ``custom`` both turn it on). The choice itself is stored in
the install-wide ``connection.wpp_engine`` (app_settings._CONNECTION_GLOBAL).
It used to be inferred from the address and port, but the port is per account
while the engine is shared by all of them: an account on an automatic port
read as "custom" after another one switched the install to neonize, and a
real WPPConnect typed in at 127.0.0.1:6300 read as neonize.
``engine_from_connection()`` still infers it for an install that predates the
setting (and for a stored value that contradicts ``wpp_custom_api``); the
neonize bridge is a WPPConnect-compatible server at a fixed local address, so
"custom API pointing at that address" is how an older install chose it.
Pure functions, no wx.
"""

WPPCONNECT = "wppconnect"
NEONIZE = "neonize"
CUSTOM = "custom"
# The order the pickers list them in; the first one offered is the one a
# picker preselects, so Neonize is what a new install pairs with by default.
ENGINES = (NEONIZE, WPPCONNECT, CUSTOM)

# i18n key of each engine's name.
LABEL_KEYS = {
    NEONIZE: "connection_engine_neonize",
    WPPCONNECT: "connection_engine_wppconnect",
    CUSTOM: "connection_engine_custom",
}

NEONIZE_SERVER = "http://127.0.0.1"
NEONIZE_WS_SERVER = "ws://127.0.0.1"
NEONIZE_PORT = 6300


def engine_of(custom_api: bool, server: str, ws_server: str, port) -> str:
    """The engine the connection settings stand for."""
    if not custom_api:
        return WPPCONNECT
    try:
        port = int(port)
    except (TypeError, ValueError):
        return CUSTOM
    if fixed_address(NEONIZE) == ((server or "").strip().rstrip("/"),
                                  (ws_server or "").strip().rstrip("/"), port):
        return NEONIZE
    return CUSTOM


def engine_from_connection(conn) -> str:
    """The engine of a ``connection`` settings block: the stored
    ``wpp_engine`` when it agrees with ``wpp_custom_api``, otherwise what the
    block's addresses stand for (engine_of)."""
    conn = conn if isinstance(conn, dict) else {}
    custom_api = conn.get("wpp_custom_api") is True
    stored = conn.get("wpp_engine")
    if stored in ENGINES and uses_custom_api(stored) == custom_api:
        return stored
    return engine_of(custom_api, conn.get("wpp_server"), conn.get("wpp_ws_server"),
                     conn.get("wpp_port"))


def engine_to_keep(stored, custom_api, server, ws_server, port) -> str:
    """What ``wpp_engine`` holds from now on: the stored value, or -- for an
    install older than the setting -- the engine its connection keys stood
    for."""
    return stored if stored in ENGINES else engine_of(custom_api, server, ws_server, port)


def uses_custom_api(engine: str) -> bool:
    """Whether the client talks to a server it did not start itself."""
    return engine != WPPCONNECT


def loaded_engine(main_window) -> str:
    """The engine this process was started with (WPPCONNECT when unknown)."""
    return getattr(main_window, "wpp_engine", WPPCONNECT)


def uses_profile_backup(engine: str) -> bool:
    """Whether the browser-profile backup (restore points of the WhatsApp login
    profile) means anything for the engine. Neonize has no browser profile."""
    return engine != NEONIZE


def fixed_address(engine: str):
    """(server, websocket server, port) the engine always uses, or None when
    the user (custom) or the bundled server (wppconnect) decides."""
    if engine == NEONIZE:
        return NEONIZE_SERVER, NEONIZE_WS_SERVER, NEONIZE_PORT
    return None


def addresses_editable(engine: str) -> bool:
    """Only the custom engine lets the user type the server, websocket,
    port and key."""
    return engine == CUSTOM


def index_of(engine: str) -> int:
    """The engine's row in a picker; an unknown one is WPPConnect's, like
    engine_at() and loaded_engine()."""
    return ENGINES.index(engine if engine in ENGINES else WPPCONNECT)


def engine_at(index: int) -> str:
    return ENGINES[index] if 0 <= index < len(ENGINES) else WPPCONNECT


def first_run_engines(bridge_available: bool) -> tuple:
    """The engines the first-run question offers. Neonize needs a bridge
    installed (a compiled build ships one; a repository checkout needs
    ``neonize_bridge/prepare.py``), so it is not offered to someone who could
    only end up with an error; Settings > Connection still lists it."""
    return tuple(e for e in ENGINES if e != NEONIZE or bridge_available)


def can_choose(engine: str, bridge_available: bool, current: str) -> bool:
    """Whether Settings > Connection may switch to ``engine``. The same rule as
    first_run_engines(): Neonize needs a bridge installed, except for an
    install that already runs on it (its bridge may be mid-repair)."""
    return engine != NEONIZE or bridge_available or current == NEONIZE


def should_offer_neonize(engine: str, bridge_available: bool, already_offered: bool) -> bool:
    """Whether the one-time "Neonize is available" notice is due: only to an
    install still on WPPConnect (a custom server is the user's own choice),
    only when a bridge is installed to switch to, and never twice. A start in the
    background gets it too: it is an important notice."""
    return engine == WPPCONNECT and bridge_available and not already_offered


def needs_restart(loaded: str, chosen: str) -> bool:
    """Changing the engine only takes effect after a restart."""
    return chosen != loaded
