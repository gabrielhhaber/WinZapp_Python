# Reuse each locale's own terminology

> Why a new string must use the words the same locale file already uses (the Polish `rozmowa` → `czat` case).
>
> Moved verbatim out of `CLAUDE.md` so it is read when the area is touched, not on every session. Keep it here: this is measured history, not a summary.

Locale-specific reviewed terminology takes precedence over older or unreviewed
wording. Before writing or reviewing Polish `msgstr`, read
[the Polish glossary](i18n-terminology-pl.md).

  **Reuse the terminology each locale catalog already uses — do not introduce your own.** Before writing a value, grep that locale's PO for how it already names the concept (conversation, chat, read/unread, status, attachment…) and use those words. The existing vocabulary in each catalog has been reviewed by native speakers and is the accepted term for that language; a new string that picks a synonym makes the UI inconsistent and quietly undoes their work. The concrete case: a Polish PR (f292049f) moved the Polish catalog from `rozmowa` to `czat` for "conversation" (`"Oznacz wszystkie rozmowy jako przeczytane"` → `"Oznacz wszystkie czaty jako przeczytane"`), so a new Polish string saying `rozmowy` would reintroduce exactly what was removed. The same applies per locale, not across them: en-US says "chats" where pt-BR says "conversas" — follow the catalog you are writing into, never a translation of another locale's word choice. Where a catalog is mixed, match the closest existing strings for the same feature (es-ES now says "chat/chats" throughout; "conversación" no longer appears in its active catalog entries). Changing an established term is a terminology decision for a native speaker, made across the whole catalog, not a side effect of adding one string.
