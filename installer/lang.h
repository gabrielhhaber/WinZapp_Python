#ifndef WINZAPP_LANG_H
#define WINZAPP_LANG_H

/* Shared by installer.c and uninstaller.c so the two can never disagree about
 * the language the user sees: Portuguese -> pt-BR, Spanish -> es-ES, anything
 * else -> English. Each binary keeps its own string tables (the uninstaller
 * needs far fewer strings) and indexes them with this result. Needs
 * <windows.h> included first. */

typedef enum {
    WINZAPP_LANG_EN = 0,
    WINZAPP_LANG_PT,
    WINZAPP_LANG_ES
} WinzappLang;

static inline WinzappLang winzapp_ui_lang(void)
{
    switch (PRIMARYLANGID(GetUserDefaultUILanguage())) {
    case LANG_PORTUGUESE: return WINZAPP_LANG_PT;
    case LANG_SPANISH:    return WINZAPP_LANG_ES;
    default:              return WINZAPP_LANG_EN;
    }
}

#endif
