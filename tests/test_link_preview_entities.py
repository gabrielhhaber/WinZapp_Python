"""HTML entities in link previews are decoded when sent and when read.

Reported: many preview descriptions showed ``&amp;``/``&eacute;`` instead of
the real characters (the title was fine). Sites double-escape their Open
Graph text, so one unescape pass is not enough.
"""

from unittest import mock

from core.link_preview import decode_html_entities, fetch_link_preview
from core.utils import link_preview_text


def test_decode_html_entities_handles_double_escaped_text():
    assert decode_html_entities("Caf&amp;eacute; &amp;amp; p&amp;atilde;o") == "Café & pão"
    assert decode_html_entities("a &eacute; b") == "a é b"
    assert decode_html_entities("sem entidade") == "sem entidade"
    assert decode_html_entities("") == ""
    assert decode_html_entities(None) == ""


def test_link_preview_text_decodes_entities_on_read():
    ext = {"title": "T&amp;iacute;tulo", "description": "Descri&amp;ccedil;&amp;atilde;o &amp;amp; mais"}
    assert link_preview_text(ext, "http://x") == "Título. Descrição & mais. http://x"


def test_fetch_link_preview_decodes_description_entities():
    page = (
        b'<html><head><meta property="og:title" content="Not&amp;iacute;cia">'
        b'<meta property="og:description" content="Educa&amp;ccedil;&amp;atilde;o &amp;amp; sa&amp;uacute;de">'
        b"</head></html>"
    )
    response = mock.Mock()
    response.headers = {"Content-Type": "text/html"}
    response.encoding = "utf-8"
    response.iter_content.return_value = [page]
    with mock.patch("core.link_preview.requests.get", return_value=response):
        preview = fetch_link_preview("http://example.com")
    assert preview["title"] == "Notícia"
    assert preview["description"] == "Educação & saúde"
