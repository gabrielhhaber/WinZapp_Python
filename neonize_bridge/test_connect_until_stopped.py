"""A failed event loop disconnects the client (server.connect_until_stopped)."""
import server


class Client:
    def __init__(self, fail):
        self.fail, self.calls = fail, []

    def connect(self):
        self.calls.append("connect")
        if self.fail:
            raise RuntimeError("proto: required field neonize.Receipt.Type not set")

    def disconnect(self):
        self.calls.append("disconnect")


def test_a_failed_event_loop_lets_go_of_the_whatsapp_socket():
    cli = Client(fail=True)
    server.connect_until_stopped(cli)
    assert cli.calls == ["connect", "disconnect"]


def test_a_loop_stopped_on_purpose_is_left_alone():
    cli = Client(fail=False)
    server.connect_until_stopped(cli)
    assert cli.calls == ["connect"]


def test_a_disconnect_that_fails_is_not_raised():
    class Stubborn(Client):
        def disconnect(self):
            raise RuntimeError("already gone")
    server.connect_until_stopped(Stubborn(fail=True))
