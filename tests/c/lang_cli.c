/* Console harness for installer/lang.h: prints the language name
 * winzapp_lang_from_langid() picks for each LANGID given in hex on the command
 * line. No window, no Windows call besides the macros. Used by
 * tests/test_uninstaller_language.py. */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include "lang.h"

static const char *name_of(WinzappLang l)
{
    switch (l) {
    case WINZAPP_LANG_EN:    return "en-US";
    case WINZAPP_LANG_PT_BR: return "pt-BR";
    case WINZAPP_LANG_PT_PT: return "pt-PT";
    case WINZAPP_LANG_ES:    return "es-ES";
    case WINZAPP_LANG_PL:    return "pl";
    case WINZAPP_LANG_RO:    return "ro";
    case WINZAPP_LANG_TR:    return "tr-TR";
    }
    return "?";
}

int main(int argc, char **argv)
{
    for (int i = 1; i < argc; i++) {
        LANGID id = (LANGID)strtoul(argv[i], NULL, 16);
        printf("%s\n", name_of(winzapp_lang_from_langid(id)));
    }
    return 0;
}
