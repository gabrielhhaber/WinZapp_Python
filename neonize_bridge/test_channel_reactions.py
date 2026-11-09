"""channels/reactions: the edit mutation with only settings.reaction_codes."""
import channel_reactions as cr


def test_the_choice_becomes_the_mutations_settings():
    channel, variables = cr.reactions_request({"id": "120363000000000001@newsletter", "value": "basic"})
    assert channel == "120363000000000001@newsletter"
    assert variables == {"newsletter_id": channel,
                         "updates": {"settings": {"reaction_codes": {"value": "BASIC"}}}}


def test_anything_else_is_refused():
    for body in ({"id": "1@newsletter", "value": "BLOCKLIST"}, {"id": "5511@s.whatsapp.net", "value": "ALL"},
                 {"value": "ALL"}, None):
        assert cr.reactions_request(body) is None
