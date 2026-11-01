"""Run the portable, network-free verification matrix on both Mac CI architectures."""

from tests.test_macos_provenance_network import *  # noqa: F401,F403


def test_openers_carry_certificates_on_a_mac_without_python_org_python(monkeypatch):
    """The bundled OpenSSL's default CA path exists only beside python.org Python."""
    import ssl
    import urllib.request

    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")
    from winzapp_mac import provenance as p
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] == 0  # the control
    context = p._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()["x509_ca"] > 100
    for opener in (p._API_OPENER, p._ASSET_OPENER):
        handlers = [h for h in opener.handlers
                    if isinstance(h, urllib.request.HTTPSHandler)]
        assert len(handlers) == 1 and handlers[0]._context is not None
