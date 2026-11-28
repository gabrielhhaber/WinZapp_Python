"""What a summary adds to a message: saved descriptions, polls and quoted excerpts."""
from types import SimpleNamespace

from core.ai_media.summary import message_text
from core.ai_media.summary_context import enrich_text, poll_text, quote_prefix
from core.transcription import stored


def described(msg_type, text, caption=None):
    result = SimpleNamespace(text=text, language=None, language_probability=None,
                             model_id="gemini", backend=stored.ONLINE_BACKEND, vad_used=True)
    inner = {"caption": caption} if caption else {}
    return {"messageType": msg_type, "message": {msg_type: inner},
            stored.TRANSCRIPTION_KEY: stored.record_from_result(
                result, 1.0, kind=stored.DESCRIPTION_KIND)}


def test_a_saved_description_replaces_the_bare_marker():
    msg = described("imageMessage", "A red door.")
    assert enrich_text(msg, message_text(msg)) == "[Image: A red door.]"


def test_a_caption_is_kept_after_the_description():
    msg = described("videoMessage", "A dog runs.", caption="look")
    assert enrich_text(msg, message_text(msg)) == "[Video: A dog runs.] look"


def test_media_without_a_saved_description_stays_as_it_was():
    msg = {"messageType": "imageMessage", "message": {"imageMessage": {}}}
    assert enrich_text(msg, "[Image]") == "[Image]"


def poll(votes=None):
    options = [{"name": "Pizza", "votes": votes}, {"name": "Sushi"}]
    return {"messageType": "pollCreationMessage",
            "message": {"pollCreationMessage": {"name": "Dinner?", "options": options}}}


def test_poll_lists_its_options_and_never_invents_votes():
    assert poll_text(poll()) == "[Poll] Dinner? - options: Pizza; Sushi"
    assert poll_text(poll(votes=3)) == "[Poll] Dinner? - options: Pizza (3 votes); Sushi"
    assert poll_text(poll(votes=1)).count("(1 vote)") == 1
    assert enrich_text(poll(), "[Poll]").startswith("[Poll] Dinner?")


def test_a_plain_message_is_no_poll():
    assert poll_text({"messageType": "conversation", "message": {"conversation": "hi"}}) == ""


def test_reply_gets_an_excerpt_of_the_quoted_message():
    reply = {"messageType": "extendedTextMessage", "message": {"extendedTextMessage": {
        "text": "yes", "contextInfo": {"quotedMessage": {"conversation": "Coming at 8?"}}}}}
    assert quote_prefix(reply) == "[in reply to: Coming at 8?] "
    assert enrich_text(reply, "yes") == "[in reply to: Coming at 8?] yes"


def test_top_level_context_and_media_quotes_are_understood():
    reply = {"contextInfo": {"quotedMessage": {"audioMessage": {}}}, "message": {"conversation": "ok"}}
    assert quote_prefix(reply) == "[in reply to: voice message] "


def test_long_quotes_are_clipped_and_empty_ones_dropped():
    long = {"contextInfo": {"quotedMessage": {"conversation": "x" * 500}}}
    assert len(quote_prefix(long)) < 200
    assert quote_prefix({"contextInfo": {"quotedMessage": {}}}) == ""


def test_nothing_is_added_to_an_empty_text():
    assert enrich_text({"messageType": "x"}, "") == ""
    assert enrich_text(None, "text") == "text"
