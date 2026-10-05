#ifndef WINZAPP_LANG_H
#define WINZAPP_LANG_H

/* Shared by installer.c and uninstaller.c so the two can never disagree about
 * the language the user sees. The app ships seven languages
 * (client/languages/language_map.json) and so do both stubs; the language
 * follows the Windows display language:
 *
 *   Portuguese, Brazilian (and any Portuguese sublanguage but Portugal) -> pt-BR
 *   Portuguese, Portugal                                                -> pt-PT
 *   Spanish (every sublanguage)                                         -> es-ES
 *   Polish -> pl, Romanian -> ro, Turkish -> tr-TR
 *   anything else                                                       -> en-US
 *
 * Each binary keeps its own string tables (the uninstaller needs far fewer
 * strings) and picks one with this result. Adding an app language means a new
 * enumerator here, a case in winzapp_lang_from_langid() and a table in both
 * .c files (tests/test_uninstaller_language.py fails until all three agree).
 * Needs <windows.h> included first. */

typedef enum {
    WINZAPP_LANG_EN = 0,
    WINZAPP_LANG_PT_BR,
    WINZAPP_LANG_PT_PT,
    WINZAPP_LANG_ES,
    WINZAPP_LANG_PL,
    WINZAPP_LANG_RO,
    WINZAPP_LANG_TR
} WinzappLang;

/* Pure: no Windows call, so tests/c/lang_cli.c can feed it any LANGID. */
static inline WinzappLang winzapp_lang_from_langid(LANGID id)
{
    switch (PRIMARYLANGID(id)) {
    case LANG_PORTUGUESE:
        return SUBLANGID(id) == SUBLANG_PORTUGUESE ? WINZAPP_LANG_PT_PT
                                                   : WINZAPP_LANG_PT_BR;
    case LANG_SPANISH: return WINZAPP_LANG_ES;
    case LANG_POLISH:  return WINZAPP_LANG_PL;
    case LANG_ROMANIAN: return WINZAPP_LANG_RO;
    case LANG_TURKISH: return WINZAPP_LANG_TR;
    default:           return WINZAPP_LANG_EN;
    }
}

static inline WinzappLang winzapp_ui_lang(void)
{
    return winzapp_lang_from_langid(GetUserDefaultUILanguage());
}

#endif
