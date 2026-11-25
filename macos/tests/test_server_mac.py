"""server_mac.py: what the Mac quit kills on the account's port."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client")]

from winzapp_mac import server_mac  # noqa: E402


def test_the_account_s_own_node_is_killed_on_quit():
    assert server_mac.owns_port_listener(types.SimpleNamespace(wpp_custom_api=False))
    assert server_mac.owns_port_listener(types.SimpleNamespace())


def test_the_shared_neonize_bridge_or_a_custom_server_is_left_alone():
    """The Neonize engine is a custom API on port 6300 that every account
    shares: killing the listener on one account's quit would cut the others
    and skip the bridge's own shutdown."""
    assert not server_mac.owns_port_listener(types.SimpleNamespace(wpp_custom_api=True))
