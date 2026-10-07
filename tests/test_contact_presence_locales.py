"""Actual PO/MO text in the three presence views, without accounts or windows."""
import datetime
import json
import polib
import pytest

from core import i18n, translation_catalog
import core.contact_presence as presence
from core.locale_format import get_date_format, get_datetime_format, get_time_format
from main_window.chat_events import ChatEventsMixin
from tests.locales import CATALOGS_DIR, registered_locale_codes
from tests.test_contact_presence_freshness import Owner, PN
from tests.test_contact_presence_views import views
from ui.conversation_panel.conversation_info import ConversationInfoMixin
from ui.dialogs.conversation_data_dialog import ConversationDataDialog


@pytest.fixture
def clock(monkeypatch):
    real = datetime.datetime
    frozen = real(2026, 10, 7, 18, 42)

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(datetime, "datetime", Clock)
    monkeypatch.setattr(presence.time, "monotonic", lambda: 100)
    return frozen


@pytest.fixture(params=registered_locale_codes())
def catalog(request):
    locale = request.param
    return locale, polib.pofile(str(CATALOGS_DIR / locale / "LC_MESSAGES/winzapp.po"))


@pytest.fixture(params=("po", "mo"))
def runtime(request, catalog, tmp_path, monkeypatch):
    locale, po = catalog
    if request.param == "po":
        client = CATALOGS_DIR.parent / "client"
        monkeypatch.setattr(translation_catalog, "_is_frozen", lambda: False)
    else:
        # Compile only into pytest's synthetic resource directory. The installed
        # app and its translations are never inspected or modified.
        client = tmp_path / "client"
        folder = client / "languages" / locale / "LC_MESSAGES"
        folder.mkdir(parents=True)
        (folder / "winzapp.mo").write_bytes(po.to_binary())
        source = polib.pofile(str(CATALOGS_DIR / "en-US/LC_MESSAGES/winzapp.po"))
        keys = {e.msgctxt: e.msgid for e in source if e.msgctxt and not e.obsolete}
        (client / "languages/winzapp.keys").write_text(json.dumps(keys), encoding="utf-8")
        monkeypatch.setattr(translation_catalog, "_is_frozen", lambda: True)
    monkeypatch.setattr(translation_catalog, "resource_path", lambda *parts: str(client.joinpath(*parts)))
    monkeypatch.setattr(i18n, "_TRANSLATIONS_CACHE", {})
    owner = Owner()
    panel, dialog, button, text, layouts = views(owner)
    owner.settings = {"general": {"language": locale}}
    owner.i18n = i18n.I18n(owner)
    dialog._i18n = owner.i18n
    for key in ("presence_online", "online_status", "presence_unavailable",
                "presence_last_seen", "last_seen_today", "last_seen_yesterday", "last_seen_date"):
        assert owner.i18n.t(key) != key, f"Missing or fuzzy presence text: {locale}/{key}"
    return owner, panel, dialog, button, text, layouts


@pytest.mark.parametrize("case", ("online", "today", "yesterday", "older", "withheld", "expired"))
def test_real_localized_presence_in_button_shortcut_and_dialog(runtime, clock, case):
    owner, panel, dialog, button, text, layouts = runtime
    days = {"today": 0, "yesterday": 1, "older": 7}.get(case, 0)
    seen = clock - datetime.timedelta(days=days)
    data = {"lastKnownPresence": "unavailable", "lastSeen": int(seen.timestamp())}
    if case == "online":
        data["lastKnownPresence"] = "available"
    elif case == "withheld":
        data["lastSeen"] = None
    presence.merge(owner, PN, data, now=0 if case == "expired" else 100)
    ConversationInfoMixin._refresh_presence_note(panel, PN)
    ConversationInfoMixin._refresh_presence_note(panel, PN)
    spoken = ChatEventsMixin._presence_announcement_for_chat(owner, panel.conversation)
    ConversationDataDialog._populate_personal_unsafe(dialog, {})

    if case in ("withheld", "expired"):
        expected_note = "Contact"
        expected_spoken = owner.i18n.t("presence_unavailable")
        assert owner.i18n.t("online_status") not in text.text.splitlines()
    elif case == "online":
        expected_note = owner.i18n.t("online_status")
        expected_spoken = owner.i18n.t("presence_online")
        assert expected_note in text.text.splitlines()
    else:
        key = {"today": "last_seen_today", "yesterday": "last_seen_yesterday", "older": "last_seen_date"}[case]
        # Date formats belong to the locale too, not to this fixture.
        expected_note = owner.i18n.t(key).format(
            date=seen.strftime(get_date_format(owner.i18n.t("date_fmt"))),
            time=seen.strftime(get_time_format(owner.i18n.t("time_fmt"))))
        time_format = (get_time_format(owner.i18n.t("time_fmt")) if days == 0
                       else get_datetime_format(owner.i18n.t("datetime_fmt")))
        expected_spoken = owner.i18n.t("presence_last_seen").format(time=seen.strftime(time_format))
        assert expected_note in text.text.splitlines()

    assert button.text == expected_note
    assert spoken == expected_spoken and spoken != "presence_unavailable"
    assert "{time}" not in spoken and "{date}" not in button.text
    assert button.writes == len(layouts) == 1 and button.focuses == 0
    if owner.settings["general"]["language"] == "tr-TR":
        # Check the words actually rendered, independently of the catalog
        # lookup used above. Keep the existing Turkish date/time convention.
        notes = {"online": "Çevrimiçi", "today": "Son görülme bugün 18:42",
                 "yesterday": "Son görülme dün 18:42",
                 "older": "Son görülme 30.09.2026 18:42",
                 "withheld": "Contact", "expired": "Contact"}
        announcements = {"online": "Çevrimiçi", "today": "Son görülme 18:42",
                         "yesterday": "Son görülme 06.10.2026 18:42",
                         "older": "Son görülme 30.09.2026 18:42",
                         "withheld": "Şu anda durum bilgisi yok",
                         "expired": "Şu anda durum bilgisi yok"}
        assert button.text == notes[case]
        assert spoken == announcements[case]
