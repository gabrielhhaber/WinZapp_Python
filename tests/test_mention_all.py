"""core.mention_all.says_everyone: the text's own word for "everyone"."""

import pytest

from core.mention_all import says_everyone


@pytest.mark.parametrize("text", [
    "@todos bom dia", "Bom dia @Todos!", "@all hi", "hey @everyone",
    "@wszyscy cześć", "@toți salut", "@tümü merhaba", "@الكل مرحبا",
])
def test_the_words_whatsapp_and_every_locale_write(text):
    assert says_everyone(text)


@pytest.mark.parametrize("text", [
    "", "bom dia", "@5511999999999 oi", "todos sem arroba",
    "mail@todos.com", "@todoseles", "@@todos",
])
def test_anything_else_is_not_everyone(text):
    assert not says_everyone(text)


def test_the_current_locale_word_is_added():
    assert not says_everyone("@ognuno ciao")
    assert says_everyone("@ognuno ciao", "Ognuno")
