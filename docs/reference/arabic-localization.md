# Arabic localization

Arabic (`ar`, displayed as `العربية`) is an initial community translation of
the interface and the Windows installer/uninstaller. Keep the product name
**WinZapp** in English in Arabic text, including inflected sentences. The PO
at `translations/ar/LC_MESSAGES/winzapp.po` is authoritative.

## Translation and review status

The initial translation was produced with AI assistance through OpenCode.
Additional source updates and obvious mixed-language wording were reviewed
while preparing the contribution. The contributor tried the portable Arabic
preview and reported that the translation looked good and was provisionally
acceptable. This is initial user acceptance, not a comprehensive linguistic,
right-to-left layout, or screen-reader certification. Further Arabic-speaking
review and accessibility feedback are welcome.

The complete usage guide is translated in `docs/user-guide/secoes/ar/`, with the
page text in `docs/user-guide/i18n/ar.json`. It was written with AI assistance
from the English sections and has not had a native-speaker review yet. Every
interface label it quotes comes from the PO, and the guide checks in
`docs/user-guide/PROCESSO.md` cover it like the other seven guides. Further
Arabic-speaking review is welcome here too.

## Terms the guide follows

The PO decides, and these are the words it uses most. Where the PO is mixed the
guide picks one and keeps it:

| English | Arabic |
|---|---|
| chat | دردشة (محادثة only in the strings that say conversation) |
| WhatsApp / Windows | واتساب / ويندوز (WinZapp stays in English) |
| transcription | تفريغ نصي, never تسجيل (#638) |
| recording | تسجيل |
| pairing | الاقتران |
| system tray | منطقة الإشعارات |
| locked chats vault / PIN | الخزنة / رقم التعريف |
| starred | مميزة |
| reaction | رد فعل |
| bookmark | علامة (the PO also says إشارة, which it uses for mentions) |
| provider | الموفّر |

## Updating the translation

Run `translations-update` after changes to the English catalog, review new and
fuzzy Arabic entries, then run `translations-compile` and `translations-check`.
Preserve format fields, markup, keyboard mnemonics and existing terminology.
Do not mark changed wording reviewed solely because an automated check passes.
No automatic AI translation service, credentials, or scheduled workflow is
introduced by this contribution.

The existing singular-key translation API does not support gettext plural
entries. Adding Arabic plural handling or globally mirroring native wx controls
needs a separate tested change; this translation does not claim either feature.
