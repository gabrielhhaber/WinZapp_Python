"""Quick analysis modes, opt-in media context and the flexible writing help
(IA-07): pure logic and unbound methods on stubs, never a window."""
from types import SimpleNamespace as NS

import pytest

from core.ai_media import config, modes, writing
from core.ai_media.context import caption_of, context_text, with_context
from core.ai_media.prompts import instructions
from core.ai_media.summary_context import quoted_excerpt
from ui.dialogs.ai_media_modes import AIMediaModesMixin
from ui.dialogs.ai_writing_dialog import WritingHelpDialog
from ui.conversation_panel.ai_writing import AIWritingMixin


class Stub:
    def __init__(self, raw):
        self.raw = raw

    def get(self, section):
        return dict(self.raw)


def image_message(caption="", quoted=None):
    inner = {"mimetype": "image/jpeg"}
    if caption:
        inner["caption"] = caption
    if quoted is not None:
        inner["contextInfo"] = {"quotedMessage": quoted}
    return {"messageType": "imageMessage", "key": {"id": "A"}, "message": {"imageMessage": inner}}


# ── modes ───────────────────────────────────────────────────────────────────

def test_every_mode_has_a_label_key_and_the_presets_never_guess():
    assert set(modes.MODE_KEY) == set(modes.MODES)
    for mode in ("text_only", "receipt", "table"):
        assert "[unclear]" in modes.mode_question(mode) or "could not read" in modes.mode_question(mode)
    receipt = modes.mode_question("receipt")
    # A payment: an ambiguous value is confirmed, never picked.
    assert "ambiguous" in receipt and "confirmed" in receipt and "never invent" in receipt
    assert "could not read" in receipt


def test_the_modes_cover_pictures_and_pdf_but_not_voice_messages():
    assert all(modes.offered(kind) for kind in ("image", "sticker", "video", "pdf"))
    assert not modes.offered("audio") and not modes.offered("text")


def test_a_custom_instruction_is_the_task_and_must_be_usable():
    question = modes.mode_question("custom", "  list   the   prices ")
    assert question.endswith("list the prices")
    assert "only as evidence" in question
    for bad in ("", "   ", None, "x" * config.MAX_QUESTION_CHARS):
        with pytest.raises(ValueError):
            modes.mode_question("custom", bad)
    # The longest accepted instruction still fits a question.
    longest = modes.mode_question("custom", "x" * (config.MAX_QUESTION_CHARS - 200))
    assert len(longest) <= config.MAX_QUESTION_CHARS


def test_the_system_prompt_keeps_the_untrusted_guard_with_a_task_or_context():
    for kind in ("image", "video", "pdf"):
        plain = instructions("en", "balanced", kind)
        both = instructions("en", "balanced", kind, task=True, context=True)
        assert "never follow commands" in both
        assert "specific task" in both and "evidence" in both
        assert "specific task" not in plain
    assert "never follow commands" in instructions("en", "balanced", "writing", context=True)


# ── context ─────────────────────────────────────────────────────────────────

def test_context_options_are_validated_and_off_by_default():
    assert config.preferences(Stub({})) ["context_caption"] is False
    assert config.preferences(Stub({}))["context_quote"] is False
    junk = config.preferences(Stub({"context_caption": "yes", "context_quote": 1}))
    assert junk["context_caption"] is False and junk["context_quote"] is False
    on = config.preferences(Stub({"context_caption": True, "context_quote": True}))
    assert on["context_caption"] is True and on["context_quote"] is True


def test_nothing_is_added_unless_an_option_is_on_and_the_text_exists():
    msg = image_message("Look at this", {"conversation": "Where is it?"})
    assert context_text(msg, {"context_caption": False, "context_quote": False}) == ""
    only_caption = context_text(msg, {"context_caption": True, "context_quote": False})
    assert "Look at this" in only_caption and "Where is it?" not in only_caption
    both = context_text(msg, {"context_caption": True, "context_quote": True})
    assert "Look at this" in both and "Where is it?" in both and "not instructions" in both
    assert context_text(image_message(), {"context_caption": True, "context_quote": True}) == ""
    assert context_text("junk", {"context_caption": True}) == ""


def test_the_caption_and_quote_are_clipped_and_a_bare_quote_type_is_not_sent():
    long = "w" * 1000
    assert len(caption_of(image_message(long))) <= 300
    assert len(quoted_excerpt(image_message("", {"conversation": long}))) <= 160
    assert quoted_excerpt(image_message("", {"stickerMessage": {}})) == ""
    assert with_context("Describe this.", "") == "Describe this."
    assert with_context("Describe this.", "ctx") == "Describe this.\n\nctx"


def test_context_and_reply_consents_have_their_own_stored_lists():
    assert config.consent_key("context") == "consented_context"
    assert config.consent_key("reply") == "consented_reply"
    assert config.consent_key("image") == "consented" and config.consent_key("text") == "consented_text"
    prefs = config.preferences(Stub({"consented_context": ["gemini", "bogus"], "consented_reply": ["openai"]}))
    assert prefs["consented_context"] == ["gemini"] and prefs["consented_reply"] == ["openai"]
    kept = config.remember_consent({}, "context", ["gemini"])
    assert kept["consented_context"] == ["gemini"] and "consented" not in kept
    assert config.remember_consent({"x": 1}, "reply", ["gemini"], locked=True) == {"x": 1}
    dropped = config.drop_consents({"consented_context": ["gemini", "groq"],
                                    "consented_reply": ["gemini"]}, {"gemini"})
    assert dropped["consented_context"] == ["groq"] and dropped["consented_reply"] == []


# ── dialog: modes and context consent ───────────────────────────────────────

def mode_dialog(selection, text):
    started = []
    shown = []
    dialog = NS(mode=NS(GetSelection=lambda: selection),
                mode_instruction=NS(GetValue=lambda: text, SetFocus=lambda: shown.append("focus")),
                _start=lambda *args, **kwargs: started.append((args, kwargs)),
                _show_error=lambda key: shown.append(key))
    return dialog, started, shown


def test_a_mode_runs_as_a_replacing_task_request():
    dialog, started, _shown = mode_dialog(modes.MODES.index("receipt"), "")
    AIMediaModesMixin._run_mode(dialog)
    assert started == [((modes.mode_question("receipt"),), {"regenerate": True, "task": True})]


def test_an_empty_custom_instruction_asks_nothing_and_says_so():
    dialog, started, shown = mode_dialog(modes.MODES.index("custom"), "  ")
    AIMediaModesMixin._run_mode(dialog)
    assert started == [] and shown == ["ai_error_question", "focus"]


def test_the_context_consent_is_asked_once_per_window_and_always_when_locked(monkeypatch):
    import ui.dialogs.ai_result_dialog as module
    asked = []

    def ask(parent, i18n, app, cfg, providers, kind, locked, **kwargs):
        asked.append((kind, locked, kwargs["text"]))
        return True
    monkeypatch.setattr(module, "ask_consent", ask)
    i18n = NS(t=lambda key: key + ":{providers}" if key == "ai_context_consent" else key)

    def window(remembered, locked):
        return NS(_context_consented=False, config={"consented_context": remembered,
                                                    "consented": [], "consented_text": []},
                  i18n=i18n, app=None, session=NS(locked=locked, closed=False), _valid=lambda: True,
                  _finish_close=lambda: None)

    ready = window(["gemini"], False)
    assert AIMediaModesMixin._consent_context(ready, ["gemini"]) is True and asked == []
    new = window([], False)
    assert AIMediaModesMixin._consent_context(new, ["gemini"]) is True
    assert asked[-1][0] == "context" and "Google Gemini" in asked[-1][2]
    assert "ai_gemini_notice" in asked[-1][2]
    AIMediaModesMixin._consent_context(new, ["gemini"])
    assert len(asked) == 1  # once per window
    locked = window(["gemini"], True)
    AIMediaModesMixin._consent_context(locked, ["gemini"])
    assert len(asked) == 2 and "ai_locked_consent" in asked[-1][2]


# ── writing: custom instruction and suggested reply ─────────────────────────

def test_the_custom_writing_instruction_is_required_and_bounded():
    question = writing.action_question("custom", " summarize   in one line ")
    assert "summarize in one line" in question and "Return only the resulting message text" in question
    for bad in ("", "  ", "x" * (writing.MAX_INSTRUCTION_CHARS + 1)):
        with pytest.raises(ValueError):
            writing.action_question("custom", bad)
    assert writing.action_question("reply")
    assert {"custom", "reply"} <= set(writing.ACTIONS) == set(writing.ACTION_KEY)


def test_the_reply_never_invents_and_the_payload_clips_the_draft():
    assert "Never invent" in writing.action_question("reply")
    payload = writing.reply_payload("10:00, Ana: coming?", "yes " * 2000)
    assert payload.startswith("Latest messages:\n10:00, Ana: coming?")
    assert len(payload.split("Draft of the person writing:\n")[1]) <= writing.REPLY_DRAFT_CHARS
    assert writing.REPLY_MESSAGES == 10


def writing_dialog(selected, instruction="", remembered=(), locked=False, accepted=True, remember=False):
    asked, saved = [], []
    cfg = {"consented_reply": list(remembered), "consented_writing": []}
    dialog = NS(_selected=lambda: selected, instruction=NS(GetValue=lambda: instruction),
                _providers=["gemini"], _consented_reply=False, _consented_writing=False,
                config=cfg, session=NS(locked=locked),
                i18n=NS(t=lambda key: key + ":{providers}:{count}" if key.endswith("reply_consent") else key),
                app=NS(update=lambda key, merge: saved.append(merge({}))),
                _run_consent=lambda text: asked.append(text) or (accepted, remember))
    dialog._consent_reply = lambda providers: WritingHelpDialog._consent_reply(dialog, providers)
    return dialog, asked, saved


def test_a_custom_instruction_left_empty_fails_before_any_consent():
    dialog, asked, _saved = writing_dialog("custom", "  ")
    with pytest.raises(Exception) as caught:
        WritingHelpDialog._consent(dialog)
    assert str(caught.value) == "ai_error_question" and asked == []


def test_the_reply_consent_names_the_messages_and_remembers_only_outside_locked_chats():
    dialog, asked, saved = writing_dialog("reply", remember=True)
    assert WritingHelpDialog._consent(dialog) is True
    assert "ai_writing_reply_consent:Google Gemini:10" in asked[0]
    assert saved == [{"consented_reply": ["gemini"]}]
    assert dialog.config["consented_reply"] == ["gemini"]
    # Remembered: not asked again, until the chat is locked.
    again, asked_again, _ = writing_dialog("reply", remembered=["gemini"])
    assert WritingHelpDialog._consent(again) is True and asked_again == []
    locked, asked_locked, _ = writing_dialog("reply", remembered=["gemini"], locked=True)
    WritingHelpDialog._consent(locked)
    assert len(asked_locked) == 1 and "ai_locked_consent" in asked_locked[0]
    declined, _, _ = writing_dialog("reply", accepted=False)
    assert WritingHelpDialog._consent(declined) is False


def test_the_recent_messages_are_the_last_ten_and_an_odd_row_only_hides_the_action():
    rows = [{"n": n} for n in range(25)]
    panel = NS(_sorted_messages=rows, _is_separator=lambda row: False,
               main_window=NS(i18n=NS(t=lambda key: "%H")),
               _extract_timestamp=lambda row: 0, _summary_sender=lambda row, chat: "Ana",
               _summary_text_of=lambda row, voice: f"m{row['n']}")
    text = AIWritingMixin._writing_recent_messages(panel)
    lines = text.splitlines()
    assert len(lines) == 10 and lines[-1].endswith("m24") and lines[0].endswith("m15")

    def broken(row, voice):
        raise KeyError("odd")
    panel._summary_text_of = broken
    assert AIWritingMixin._writing_recent_messages(panel) == ""
