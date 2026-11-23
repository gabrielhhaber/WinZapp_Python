# Polish terminology and UI wording

Read this file before writing or reviewing Polish `msgstr` values in
`translations/pl/LC_MESSAGES/winzapp.po`, including translations added alongside
a feature. Apply the same terms to Polish help and changelog text.

These native-speaker-reviewed rules take precedence over older help, changelogs
and unreviewed catalog wording. An AI-generated `msgstr` does not become a
reviewed term merely by being committed or passing gettext tests. Use code to
establish what a feature does; use this glossary to name that meaning in Polish.

## Review provenance

The Polish terminology records native-speaker reviews by
[@djgraco](https://github.com/djgraco), including
[PR #481](https://github.com/gabrielhhaber/WinZapp_Python/pull/481) and
[PR #561](https://github.com/gabrielhhaber/WinZapp_Python/pull/561).
For the encryption notice, @djgraco reported checking the wording directly in
the Polish WhatsApp phone app in
[this comment on PR #561](https://github.com/gabrielhhaber/WinZapp_Python/pull/561#issuecomment-6277771919).
Keep this distinction between a contributor's documented verification and an
independently verifiable official source when recording future decisions.

## Contents

- [Choosing Polish wording](#choosing-polish-wording)
- [WhatsApp chat means czat](#whatsapp-chat-means-czat)
- [Chat access protection and contact blocking](#chat-access-protection-and-contact-blocking)
- [Other reviewed terms](#other-reviewed-terms)
- [Device linking and channel roles](#device-linking-and-channel-roles)
- [Events, writing help and noise reduction](#events-writing-help-and-noise-reduction)
- [Review of 22 November 2026](#review-of-22-november-2026)
- [Meaning, sources and spoken wording](#meaning-sources-and-spoken-wording)
- [Language review and technical references](#language-review-and-technical-references)

## Choosing Polish wording

1. Check this glossary for the concept, including its exceptions.
2. Search the Polish PO for reviewed wording, especially strings from the same
   feature. Do not copy a nearby unreviewed synonym over a reviewed term.
3. Check related labels, states, settings, shortcut descriptions and help.
4. If the meaning is unclear, trace the key's call sites and actual behavior.
   Check all uses when one key serves several operations.
5. Translate the meaning into natural Polish, then review technical correctness
   and terminology separately.

Preserve good existing translations and prior native-speaker corrections.
Change them for a concrete reason: incorrect meaning, unnatural wording,
inconsistency or changed behavior. Do not rewrite them merely for variety.
If a new concept is not covered here, establish its context instead of inventing
a blanket substitution rule.

## WhatsApp chat means czat

For a WhatsApp message thread, use **czat**, including when the English source
calls it a `conversation`. Inflect it normally in Polish.

| Meaning | Polish |
| --- | --- |
| chat / conversation thread | czat |
| chats / conversation threads | czaty |
| chat list | lista czatów |
| new chat | nowy czat |
| open this conversation | otwórz ten czat |
| this conversation's messages | wiadomości tego czatu |
| chat data | dane czatu |
| private chats, in descriptive prose | czaty prywatne |
| Personal, the category name | Osobiste |

Do not use `rozmowa`, `rozmowy`, `konwersacja` or their inflected forms to name
a WhatsApp message thread. An English key beginning with `conversation_` does
not make `rozmowa` the right Polish term. The `Chats` navigation button is
`Czaty`.

Do **not** ban `rozmowa` globally or run a global search/replace. It remains
valid for a spoken conversation, voice/video call or AI conversation. Examples:

- `ai_error_limit`: `Ta sesja osiągnęła limit liczby żądań lub długości rozmowy.`
  describes an AI session; keep `rozmowy`.
- `chat_encryption_notice`: `Wiadomości i rozmowy są w pełni zaszyfrowane.`
  includes calls. Keep `rozmowy` in this documented phone-app wording (see
  [review provenance](#review-provenance)). The full reviewed notice is:
  `Wiadomości i rozmowy są w pełni zaszyfrowane. Tylko osoby w tym czacie mogą je odczytywać, odsłuchiwać i udostępniać.`
- `bulk_transcription_open_hint`: `otwórz ten czat` refers to opening the
  message thread; `otwórz tę rozmowę` is inappropriate here.

For automatic-processing settings, describe the action being enabled for chats.
Reviewed examples include `Czaty objęte automatycznym przetwarzaniem:`,
`Wszystkie czaty oprócz tych, w których wyłączę tę funkcję` and
`Tylko czaty, w których włączę tę funkcję`. Avoid wording that suggests the
chats themselves are switched on or off.

## Chat access protection and contact blocking

Keep chat access protection distinct from blocking a contact, encryption and
closing the local protected-chat vault.

| Meaning | Polish |
| --- | --- |
| lock chat: enable access protection | Zabezpiecz dostęp do czatu |
| unlock chat: disable access protection | Wyłącz zabezpieczenie dostępu do czatu |
| locked chats | Zabezpieczone czaty |
| locked chat, in descriptive prose | zabezpieczony czat / czat z zabezpieczeniem dostępu |
| chats without chat lock | czaty bez zabezpieczenia dostępu |
| block a contact | Zablokuj |

Do not call access-protected chats `zablokowane czaty`. Keep `Zablokuj` for
contact blocking. Check the code when `lock` refers to a vault or another
mechanism; do not replace all occurrences of that English word alike.

Reviewed explanation:
`Dostęp do tego czatu jest zabezpieczony w WhatsAppie na telefonie. Możesz to wyłączyć tylko tam.`

## Other reviewed terms

Apply these to the indicated meaning, with grammatical inflection and
capitalization appropriate to the UI location.

| Meaning / UI role | Polish |
| --- | --- |
| forward a message | Przekaż |
| forwarded message marker | Przekazano |
| last seen | Widziano |
| who can see when I am online | Kto widzi, że jestem online |
| online visibility: same as last seen | Tak samo jak w ustawieniu „Widziano” |
| group participants / members | Członkowie |
| group size | Liczba członków |
| Media tab | Media |
| frequent emoji category | Często używane |
| caption | Podpis |
| add a caption | Dodaj podpis... |
| playback device | urządzenie odtwarzające |
| mute chat | Wycisz |
| unmute chat menu action (`unmute_chat`) | Włącz powiadomienia |
| unarchive chat action | Przywróć |
| show the window from the system tray | Pokaż |
| read-receipt status | Wyświetlono |
| announce, as a behavior setting | Ogłaszaj |

The menu label `Włącz powiadomienia` and the descriptive shortcut wording
`wycisz/wyłącz wyciszenie czatu` (`shortcut_alt_shift_s_label`) serve different
UI roles. Do not prescribe the shortcut wording as a standalone menu label.

`Osobiste` is a category name; do not mechanically turn descriptive
`czaty prywatne` into `czaty osobiste`. Likewise, the `Media` tab does not
require replacing every descriptive use of `multimedia`.

Keep the reviewed phone-linking label `Połącz, korzystając z &numeru telefonu`;
the ampersand is a mnemonic. Preserve established reply terminology where it
matches the same action; check context before borrowing a term from another app.

## Device linking and channel roles

The following additions were reviewed by @djgraco on 9 November 2026 after
comparing WinZapp's behavior with the Polish strings in a supplied WhatsApp APK.
That is the source of the phone-app labels below; it is not a claim that every
APK string is displayed in every account or platform variant.

| Meaning / UI role | Polish |
| --- | --- |
| keep chats archived | Pozostaw czaty zarchiwizowane |
| invite channel administrators | Zaproś administratorów... |
| invite a follower to become a channel administrator | Zaproś do roli administratora kanału... |
| remove a channel administrator's role | Odwołaj z roli administratora... |
| leave the channel administrator role | Zrezygnuj z roli administratora... |
| cancel a channel administrator invitation | Odwołaj zaproszenie do roli administratora... |
| transfer channel ownership | Przenieś prawa własności... |
| News, the channel directory category | Wiadomości |
| Linked devices, the WhatsApp phone menu | Połączone urządzenia |
| Link a device, the WhatsApp phone action | Połącz urządzenie |
| phone-linking code dialog title | Wprowadź kod na telefonie |
| code label in that dialog | Kod: |
| cancel device linking | Anuluj łączenie |

For channels, use `zaproszenie do roli administratora` consistently in results,
errors and pending states. Inviting does not grant the role immediately: the
recipient becomes an administrator only after accepting. Removing that role
does not remove the person from the channel. Do not apply these channel labels
to community administration without checking the different behavior.

Prefer `łączenie` and a contextual `kod` in the reviewed login flow rather
than technical `parowanie` / `kod parowania`. Distinguish the phone-number
route from a QR image: `qrcode_updated` announces a phone-linking code, whereas
`qrcode_image_updated` announces the QR image. The phone-linking code can
contain letters; do not describe it as an eight-digit number.

Use the exact phone labels in the guide, including
`Zamiast tego połącz, korzystając z numeru telefonu`. Preserve WinZapp's actual
recovery instructions: a terminally expired code requires cancelling and
trying again, and a rate limit requires waiting. Do not invent a
`Wygeneruj nowy kod` button in WinZapp merely because WhatsApp has that label.

These are contextual decisions, not a global replacement of every occurrence
of `parowanie`, `Informacje`, `administrator` or `kod`. Keep the mnemonic
`Pozost&aw czaty zarchiwizowane`, and preserve ellipses where another dialog
opens. Apply the reviewed wording to quoted labels in the Polish guide and
changelog as well as to the PO.

## Events, writing help and noise reduction

The following wording was reviewed and accepted by @djgraco on 16 November
2026, together with the Polish UI, guide and changelog review. The three event
response labels were checked in the supplied Polish WhatsApp APK
(`assets/i18n_pl/strings_pl.pack`: IDs 4474/7398, 4476/7501 and 4477/7510).
This records verification of that supplied resource, not a claim about every
WhatsApp version or a public APK download. Related plural group labels are
Polish adaptations of the same participation terminology.

| Meaning / UI role | Polish |
| --- | --- |
| event response: going | Wezmę udział |
| event response: maybe | Może wezmę udział |
| event response: not going | Nie wezmę udziału |
| event response groups | Wezmą udział / Może wezmą udział / Nie wezmą udziału |
| rewrite a message draft | Przeredaguj wiadomość |
| make a message draft more professional | Nadaj wiadomości bardziej profesjonalny ton |
| make a message draft encouraging | Nadaj wiadomości bardziej wspierający ton |
| noise-reduction device selector | Urządzenie używane do redukcji szumów |
| channel reach | Liczba kont, do których dotarły aktualizacje |
| channel unfollows, a statistics counter | Rezygnacje z obserwowania |

Describe participation in an event rather than physical travel (`Idę`): the
event may also be online. Match the response, own-response state and response
group terminology, preserving each control's mnemonic.

The writing-help labels describe WinZapp's own draft actions and prompts;
they are reviewed Polish choices, not quotations from WhatsApp's writing-help
interface. Applying a result replaces a draft only after the user's explicit
choice; it does not send the chat message.

Use `urządzenie` as the umbrella term for CPU, NVIDIA GPU and Core ML in
noise-reduction settings. Keep `procesor` for the CPU option and other places
where it really means CPU. cuBLAS is a library; CUDA 12 can share it with local
transcription, whereas CUDA 13 has separate components. Do not make a blanket
claim that every downloaded component is shared.

Apply these names to quoted labels in the guide and changelog too. When the
English source is stale, verify the actual behavior in code and explain the
Polish clarification in the PR rather than silently treating the source as
correct or changing other locales without review.

## Review of 22 November 2026

The following decisions were accepted by @djgraco on 22 November 2026 after
reviewing the Polish UI, guide and changelog at commit
`36ddb25044cf70d7700036e7f42e9fe2f36c876d`, including a second comparison with
the supplied WhatsApp APK. They extend, rather than reset, the earlier reviews.
The supplied `split_i18n_pl.apk` has SHA-256
`d272f6bab7aa8278539c57d79d4af7f17ee18cfe2c6cb43475dfe3cbf205ec01`.
Its `assets/i18n_pl/strings_pl.pack` contains ordinary entries and plural groups;
their numeric IDs are resource identifiers within that pack, not line numbers.

| Meaning / UI role | Reviewed Polish |
| --- | --- |
| directly promote a group member | Mianuj administratorem grupy |
| directly demote a group administrator | Odwołaj jako administratora grupy |
| promote a community administrator | Mianuj administratorem społeczności... |
| demote a community administrator | Odwołaj jako administratora społeczności... |
| community announcement group | grupa z ogłoszeniami |
| approve a person's call-link join request | Zatwierdź |
| decline a person's call-link join request | Odrzuć |
| status-list section with unopened updates | Najnowsze aktualizacje |
| status-list section with opened updates | Wyświetlone aktualizacje |
| poll option permitting multiple answers | Zezwól na wiele odpowiedzi |
| noise-reduction method selector | Rodzaj redukcji szumów |
| neural noise-reduction method | Neuronowa |
| voice-message processing timing | Moment przetwarzania wiadomości głosowych |
| failure policy for neural processing | Gdy neuronowa redukcja szumów się nie powiedzie |
| edit of the user's own message saved | Zapisano zmiany w Twojej wiadomości |
| contact Enter-action selector | Działanie klawisza Enter na kontakcie |
| reorderable-list shortcut group | Zmiana kolejności elementów na listach |

The APK supports group administrator appointment (ID 10731), dismissal
(6544/6545), community appointment/dismissal (4953, 6300/6301), the announcement
group (1909/4995), status sections (15986/21252), and the poll option (18049).
The call waiting-room family has `Zatwierdź` (21858), `Odrzuć` (21859), a
waiting-room timeout (21863), and plural group 847 about approving participants'
joining. Copy terms only for the corresponding operation. A group/community
promotion directly changes a role; a channel administrator invitation still
requires acceptance and retains the separately reviewed invitation wording.
An approval is permission to join, not a guarantee that the person has already
connected. Waiting for the organizer's decision must not promise acceptance.

The following are deliberate native-speaker decisions, even where the supplied
APK uses a different label:

- Keep `Online`, `Kto widzi, że jestem online`, and the corresponding shortcut
  descriptions. The APK's `aktywny(-a)` and privacy question (5713, 17756)
  were reviewed but not adopted. Keep `Widziano` and its matching privacy choice.
- Prefer the established linking wording in the initial QR/phone-code flow,
  but do **not** ban `parowanie` or mechanically replace it in account and
  recovery messages. Keep it where another `połączenie` would be repetitive or
  blur the difference between a network connection and linking the device.
  In particular, retain `Połączyć można tylko konta oczekujące na parowanie.`;
  `Połączyć można tylko konta oczekujące na połączenie.` was explicitly rejected.
- Keep the `Osobiste` filter name and descriptive `czaty prywatne` in their
  respective roles. The supplied pack does not prove a matching standalone
  `Osobiste` filter label; do not invent APK verification for it.
- Keep `Podpis` for an attachment caption; the user also associates this term
  with Unigram. A missing matching phrase in the supplied pack is not a reason
  to rename a clear, reviewed feature.
- Keep `Wyświetlana nazwa` for a profile display name, and `nazwy głosujących`
  rather than narrowing arbitrary contact names to first names. Retain the
  distinctions between profile About (`Informacje`) and status updates, between
  contact blocking and chat access protection, and between calls and chat threads.
- Keep WinZapp's own AI, transcription, audio-device, storage and accessibility
  terminology where its functions differ from the phone application. The APK
  contains both `aktualizacje` and `aktualności` for channels and both `rozmowa`
  and `połączenie` for calls; none justifies a global word replacement.

Use the reviewed labels in current guide instructions and matching shortcuts.
Do not rewrite quoted old UI names throughout historical changelog entries
merely because the current interface now has a new label.

For edited-message options, distinguish the global notification labels from
the shorter labels in a chat's own settings. Describe background/foreground
as the state of WinZapp, not of the edit. Marking arrivals as read is not
speech synthesis: it requires the visible chat and an active window, with the
five-second gate explained in the guide. Allowing background speech does not
override the other muting and announcement preferences.

For media cleanup, verify the actual manual-download path before repeating an
old warning: `restore_cleaned=True` permits one explicit confirmed download;
automatic downloads remain blocked. This distinction applies to both the
cleanup confirmation and the message explaining why a file was not restored.
If English also has a stale description, explain the Polish correction in the
PR without changing the source catalogs or application behavior.

## Meaning, sources and spoken wording

Prefer natural Polish over a word-for-word rendering. Reorder, shorten or
clarify a sentence when that preserves the full meaning. Do not invent behavior,
recovery advice or restrictions that the application does not implement.

Use Polish WhatsApp / WhatsApp Web terminology for corresponding WhatsApp
features. Use Unigram / Telegram for genuinely comparable general messaging
actions, and Windows / Microsoft terminology for system functions. Resolve
conflicts in this order: actual meaning, natural Polish, terminology of the
application whose feature is represented, then consistency within WinZapp.
Verify an official label when possible; do not present a guessed phrase as a
quotation from WhatsApp.

Prefer Polish quotation marks `„…”` in prose while respecting technical syntax.

Make text understandable when spoken by NVDA, JAWS or Narrator without visual
context. Put information in a clear order, keep short messages unambiguous and
remove redundant wording. Match the role: a button can say `Otwórz`, a setting
can say `Ogłaszaj`, and a completed action can say `Połączono`. Do not apply
the grammar of an NVDA script description mechanically to WinZapp shortcuts.

Describe the real shortcut behavior, including state-dependent actions. Keep
key combinations intact and verify referenced settings paths in code. Use the
same UI names in help; do not create a second name to avoid repetition.

Do not translate proper names such as WinZapp, WhatsApp, WPPConnect, Meta AI,
NVDA or Windows. Natural inflection is allowed, e.g. `w WhatsAppie` and
`za pomocą Whispera`.

## Language review and technical references

Before finishing, inspect the changed Polish `msgstr` values and related
feature strings for terminology regressions. Searching stems such as `rozmow`,
`konwersac` and `zablokowan` helps find candidates; each match needs a semantic
review and can be legitimate. Do not treat matches in keys or English sources
as Polish wording errors. Fix chat-thread wording while retaining AI, call and
contact-blocking meanings.

Passing technical checks does not replace language review. Keep the process
instructions in their owning documents:

- [The i18n UI string skill](../../.claude/skills/i18n-ui-string/SKILL.md) covers
  adding strings, placeholders, mnemonics and required checks.
- [The gettext workflow](gettext-migration.md) covers catalog synchronization,
  fuzzy review, compilation, metadata and the supported catalog format.
- [The guide maintenance process](../user-guide/PROCESS.md) covers regenerating
  dependent guides, including shortcut tables, and adding generated outputs.
