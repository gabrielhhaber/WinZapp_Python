"""Structural guards for authenticated remote Socket.IO access."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_socketio_requires_the_same_session_credential_as_rest():
    index = _source("client/api_patches/src/index.ts")
    auth = _source("client/api_patches/src/middleware/socketAuth.ts")

    assert "io.use(socketAuthMiddleware" in index
    assert "authenticateSocketSession" in auth
    assert "bcrypt.compare(session + secureToken, token)" in auth
    assert "socket.handshake.auth" in auth
    assert "socket.handshake.headers?.apikey" in auth
    assert "new Error('unauthorized')" in auth


def test_authenticated_socket_is_bound_to_one_session_before_call_audio():
    index = _source("client/api_patches/src/index.ts")
    bridge = _source("client/api_patches/src/util/callMediaBridge.ts")

    assert "const session = socketSession(sock);" in index
    assert "sock.join(`session:${session}`);" in index
    assert "registerCallAudioSocket(sock, logger, session);" in index
    assert "session !== authenticatedSession" in bridge


def test_python_client_sends_socket_auth_payload_and_legacy_header():
    main = _source("client/main.py")

    assert 'headers={"apikey": self.token}' in main
    assert '"token": self.token' in main
    assert '"session": self.token.split(":", 1)[0]' in main


def test_setup_api_restores_socket_auth_patch_after_reclone():
    setup_api = _source("setup_api.py")
    in_app_setup = _source("client/ui/dialogs/api_setup.py")
    build = _source("build.py")

    assert '"src/middleware/socketAuth.ts"' in setup_api
    assert '"src/middleware/socketAuth.ts"' in in_app_setup
    assert '"src/middleware/socketAuth.ts"' in build
