"""Public keys the auto-updater trusts to sign a release.

Each entry is a raw 32-byte Ed25519 public key, base64-encoded. They are
written here by ``.github/scripts/release_signing.py generate`` — never by
hand, and never together with the private half, which does not belong in this
repository or anywhere on GitHub.

What each list protects, and why there are two:

- ``STABLE_PUBLIC_KEYS`` belong to keys that live OFFLINE, on the maintainer's
  own machine or removable media. A stable release is only accepted when one
  of them signed it, so a stolen GitHub account alone cannot ship a stable
  update to anyone: the attacker would also need a file that never touches
  GitHub. Keep a primary AND a backup key here — losing the only key strands
  every stable user on their current build forever, since the replacement key
  can only be delivered by an update the old key signed.
- ``ALPHA_PUBLIC_KEYS`` belong to the key alpha-release.yml signs with, held as
  a secret in the ``alpha-release`` environment so alphas keep publishing on
  every push to main with nobody at the keyboard. That automation is exactly
  what makes it weaker: anyone who can get code onto main can make CI sign it.
  So an alpha key is accepted ONLY for a release that is an alpha by both its
  tag and its signed version string — a user who never opted into alphas is
  never exposed to it.

Both empty means signing has not been set up yet, and the updater falls back to
the checksum-only behaviour it had before. Filling one and not the other is
refused by tests/test_release_signature.py: a half-configured install would
reject every release of the other channel.
"""

STABLE_PUBLIC_KEYS: "tuple[str, ...]" = ()

ALPHA_PUBLIC_KEYS: "tuple[str, ...]" = ()
