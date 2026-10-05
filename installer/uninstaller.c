#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <stdint.h>
#include <stdio.h>
#include "resource.h"
#include "lang.h"

#define REGKEY_UNINSTALL \
    L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\WinZapp"

/* ── Localised UI strings ─────────────────────────────────────────────────
   Same language rule as the installer (lang.h): the app's seven languages,
   en-US for anything else. Wide strings only — nothing localised ever
   reaches the temporary .bat, see schedule_self_delete().                 */

typedef struct {
    const wchar_t *title;          /* dialog caption                  */
    const wchar_t *prompt;         /* "Are you sure ...?" label       */
    const wchar_t *uninstall;      /* "Uninstall" button              */
    const wchar_t *cancel;         /* "Cancel" button                 */
    const wchar_t *not_found;      /* install dir missing in registry */
    const wchar_t *done_msg;       /* success message                 */
    const wchar_t *done_title;     /* success message-box title       */
} UninstallStrings;

static const UninstallStrings STR_PT_BR = {
    L"Desinstalar WinZapp",
    L"Tem certeza que deseja desinstalar o WinZapp?",
    L"Desinstalar",
    L"Cancelar",
    L"Não foi possível encontrar o diretório de instalação do WinZapp.\n"
    L"O programa pode já ter sido desinstalado.",
    L"WinZapp foi desinstalado com sucesso.",
    L"Desinstalação concluída",
};

static const UninstallStrings STR_PT_PT = {
    L"Desinstalar o WinZapp",
    L"Tem a certeza de que pretende desinstalar o WinZapp?",
    L"Desinstalar",
    L"Cancelar",
    L"Não foi possível encontrar a pasta de instalação do WinZapp.\n"
    L"O programa pode já ter sido desinstalado.",
    L"O WinZapp foi desinstalado com sucesso.",
    L"Desinstalação concluída",
};

static const UninstallStrings STR_PL = {
    L"Odinstaluj WinZapp",
    L"Czy na pewno chcesz odinstalować WinZapp?",
    L"Odinstaluj",
    L"Anuluj",
    L"Nie można znaleźć folderu instalacji WinZapp.\n"
    L"Program mógł zostać już odinstalowany.",
    L"WinZapp został pomyślnie odinstalowany.",
    L"Odinstalowywanie ukończone",
};

static const UninstallStrings STR_RO = {
    L"Dezinstalare WinZapp",
    L"Sigur doriți să dezinstalați WinZapp?",
    L"Dezinstalează",
    L"Anulează",
    L"Folderul de instalare WinZapp nu a fost găsit.\n"
    L"Este posibil ca programul să fi fost deja dezinstalat.",
    L"WinZapp a fost dezinstalat cu succes.",
    L"Dezinstalare finalizată",
};

static const UninstallStrings STR_TR = {
    L"WinZapp'i Kaldır",
    L"WinZapp'i kaldırmak istediğinizden emin misiniz?",
    L"Kaldır",
    L"İptal",
    L"WinZapp kurulum klasörü bulunamadı.\n"
    L"Program zaten kaldırılmış olabilir.",
    L"WinZapp başarıyla kaldırıldı.",
    L"Kaldırma tamamlandı",
};

static const UninstallStrings STR_ES = {
    L"Desinstalar WinZapp",
    L"¿Está seguro de que desea desinstalar WinZapp?",
    L"Desinstalar",
    L"Cancelar",
    L"No se pudo encontrar la carpeta de instalación de WinZapp.\n"
    L"Es posible que el programa ya se haya desinstalado.",
    L"WinZapp se desinstaló correctamente.",
    L"Desinstalación completada",
};

static const UninstallStrings STR_EN = {
    L"Uninstall WinZapp",
    L"Are you sure you want to uninstall WinZapp?",
    L"Uninstall",
    L"Cancel",
    L"The WinZapp installation folder could not be found.\n"
    L"The program may already have been uninstalled.",
    L"WinZapp was uninstalled successfully.",
    L"Uninstall complete",
};

static const UninstallStrings *g_str = &STR_EN;

static void select_language(void)
{
    switch (winzapp_ui_lang()) {
    case WINZAPP_LANG_PT_BR: g_str = &STR_PT_BR; break;
    case WINZAPP_LANG_PT_PT: g_str = &STR_PT_PT; break;
    case WINZAPP_LANG_ES:    g_str = &STR_ES;    break;
    case WINZAPP_LANG_PL:    g_str = &STR_PL;    break;
    case WINZAPP_LANG_RO:    g_str = &STR_RO;    break;
    case WINZAPP_LANG_TR:    g_str = &STR_TR;    break;
    default:                 g_str = &STR_EN;    break;
    }
}

/* ── Read install directory from registry ─────────────────────────────── */

static BOOL get_install_dir(wchar_t *out, DWORD char_count)
{
    /* The installer registers under HKEY_LOCAL_MACHINE when it can (running
     * elevated) and falls back to HKEY_CURRENT_USER otherwise (the default,
     * non-admin install path) — check both, machine-wide first, so this
     * finds the entry regardless of which one the install actually used. */
    const HKEY hives[] = { HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER };
    for (int i = 0; i < 2; i++) {
        HKEY hkey;
        if (RegOpenKeyExW(hives[i], REGKEY_UNINSTALL, 0,
                          KEY_READ, &hkey) != ERROR_SUCCESS)
            continue;
        DWORD type  = REG_SZ;
        DWORD bytes = char_count * sizeof(wchar_t);
        LONG  r = RegQueryValueExW(hkey, L"InstallLocation", NULL, &type,
                                   (BYTE *)out, &bytes);
        RegCloseKey(hkey);
        if (r == ERROR_SUCCESS)
            return TRUE;
    }
    return FALSE;
}

/* Build an extended-length (\\?\) form of an absolute path so DeleteFileW is
 * not limited to MAX_PATH — mirrors installer.c's to_extended_path(), needed
 * because the installer can lay down files deeper than 260 chars under a
 * long install path (deep node_modules/ trees). */
static void to_extended_path(wchar_t *out, size_t out_cap, const wchar_t *in)
{
    if (in[0] == L'\\' && in[1] == L'\\' && in[2] == L'?' && in[3] == L'\\') {
        wcsncpy(out, in, out_cap - 1);
        out[out_cap - 1] = L'\0';
        return;
    }
    if (((in[0] >= L'A' && in[0] <= L'Z') || (in[0] >= L'a' && in[0] <= L'z')) && in[1] == L':') {
        swprintf(out, (int)out_cap, L"\\\\?\\%s", in);
    } else {
        wcsncpy(out, in, out_cap - 1);
        out[out_cap - 1] = L'\0';
    }
}

/* ── Delete files listed in installed_files.dat ──────────────────────── */

static void delete_installed_files(const wchar_t *install_dir)
{
    wchar_t list_path[MAX_PATH];
    swprintf(list_path, MAX_PATH, L"%s\\installed_files.dat", install_dir);

    /* Read the whole file into memory as raw bytes, then interpret as UTF-16LE */
    HANDLE hf = CreateFileW(list_path, GENERIC_READ, FILE_SHARE_READ,
                            NULL, OPEN_EXISTING, 0, NULL);
    if (hf == INVALID_HANDLE_VALUE) return;

    LARGE_INTEGER fsize_li;
    GetFileSizeEx(hf, &fsize_li);
    DWORD fsize = (DWORD)fsize_li.QuadPart;

    BYTE *raw = (BYTE *)malloc(fsize + 4);   /* +4 for null terminator */
    if (!raw) { CloseHandle(hf); return; }

    DWORD read_bytes = 0;
    ReadFile(hf, raw, fsize, &read_bytes, NULL);
    CloseHandle(hf);
    raw[read_bytes]     = 0;
    raw[read_bytes + 1] = 0;
    raw[read_bytes + 2] = 0;
    raw[read_bytes + 3] = 0;

    wchar_t *wbuf = (wchar_t *)raw;
    /* Skip UTF-16LE BOM if present */
    if (*wbuf == 0xFEFF) wbuf++;

    /* Parse one path per line */
    wchar_t *p = wbuf;
    while (*p) {
        wchar_t *end = wcspbrk(p, L"\r\n");
        if (!end) end = p + wcslen(p);
        wchar_t save = *end;
        *end = L'\0';
        if (wcslen(p) > 0) {
            wchar_t p_ext[32768];
            to_extended_path(p_ext, 32768, p);
            SetFileAttributesW(p_ext, FILE_ATTRIBUTE_NORMAL);
            DeleteFileW(p_ext);
        }
        *end = save;
        while (*end == L'\r' || *end == L'\n') end++;
        p = end;
    }
    free(raw);

    /* Delete the list file itself */
    SetFileAttributesW(list_path, FILE_ATTRIBUTE_NORMAL);
    DeleteFileW(list_path);
}

/* ── Remove shortcuts ─────────────────────────────────────────────────── */

static void remove_shortcuts(void)
{
    wchar_t path[MAX_PATH];

    if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_DESKTOPDIRECTORY, NULL, 0, path))) {
        wchar_t link[MAX_PATH];
        swprintf(link, MAX_PATH, L"%s\\WinZapp.lnk", path);
        DeleteFileW(link);
    }

    if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_COMMON_PROGRAMS, NULL, 0, path))) {
        wchar_t link[MAX_PATH];
        swprintf(link, MAX_PATH, L"%s\\WinZapp.lnk", path);
        DeleteFileW(link);
    }
}

/* ── Remove registry entry ────────────────────────────────────────────── */

static void remove_registry_entry(void)
{
    /* Remove from both hives — harmless no-op on whichever one the install
     * didn't use (see get_install_dir()). */
    RegDeleteKeyW(HKEY_LOCAL_MACHINE, REGKEY_UNINSTALL);
    RegDeleteKeyW(HKEY_CURRENT_USER, REGKEY_UNINSTALL);
}

/* ── Schedule self-delete via temp batch file ─────────────────────────── */

static BOOL is_ascii_path(const wchar_t *s)
{
    for (; *s; s++)
        if (*s > 127) return FALSE;
    return TRUE;
}

/* Copy *in* into *out* in a form a batch script can carry safely: its 8.3
 * short alias whenever the long form leaves ASCII.
 *
 * This mirrors _console_safe_path() in client/updater.py (issue #83), and it
 * is the *primary* mechanism, not a nicety: the 8.3 alias is pure ASCII, so
 * the script stops depending on which code page cmd.exe decodes it with at
 * all, instead of merely being right about that code page. GetShortPathNameW
 * only answers for a path that already exists on disk and a volume with 8.3
 * generation still enabled — when either isn't true it leaves the long path
 * in place and the caller falls back to the OEM code page below. Both paths
 * passed here do exist at this point: delete_installed_files() empties the
 * install directory but never removes it, and uninstall.exe is this very
 * running process. */
static void console_safe_path(wchar_t *out, size_t out_cap, const wchar_t *in)
{
    wcsncpy(out, in, out_cap - 1);
    out[out_cap - 1] = L'\0';
    if (is_ascii_path(out)) return;

    wchar_t short_path[MAX_PATH];
    DWORD len = GetShortPathNameW(in, short_path, MAX_PATH);
    if (len > 0 && len < MAX_PATH && is_ascii_path(short_path)) {
        wcsncpy(out, short_path, out_cap - 1);
        out[out_cap - 1] = L'\0';
    }
}

static void schedule_self_delete(const wchar_t *uninstall_exe,
                                  const wchar_t *install_dir)
{
    wchar_t temp_dir[MAX_PATH];
    GetTempPathW(MAX_PATH, temp_dir);
    wchar_t bat_path[MAX_PATH];
    swprintf(bat_path, MAX_PATH, L"%swzuninstall.bat", temp_dir);

    /* Only the script's CONTENT needs a code page; its NAME stays wide.
     * cmd.exe reads a .bat back in the OEM code page (CP852 on a Polish
     * Windows, CP850 on a Portuguese one, ...), which is not CP_ACP for any
     * non-ASCII character — writing ANSI bytes and having cmd decode them as
     * OEM is what turned a path like "C:\Users\Paweł\AppData\Local\WinZapp"
     * into mojibake that does not exist on disk (updater.py hit the identical
     * mismatch on the self-update path, see its _oem_encoding(), issue #83).
     * But bat_path itself comes from GetTempPathW, i.e.
     * C:\Users\<account>\AppData\Local\Temp\ — it carries the very same
     * non-ASCII account name — so handing an OEM-encoded copy of it to a
     * narrow fopen() would have the UCRT decode those bytes back in CP_ACP
     * and open (fail to open) a directory that never existed, skipping the
     * self-delete entirely while the dialog still reports success. Hence
     * CreateFileW here, which is also what every other file open across both
     * stubs uses (see installer.c's write_file_list()); it writes raw bytes,
     * so the \r\n below stay \r\n instead of the \r\r\n that fopen(..,"w")
     * used to produce. */
    wchar_t uninstall_safe[MAX_PATH], install_safe[MAX_PATH];
    console_safe_path(uninstall_safe, MAX_PATH, uninstall_exe);
    console_safe_path(install_safe,   MAX_PATH, install_dir);

    /* Twice MAX_PATH because a DBCS OEM code page (CP932 on a Japanese
     * Windows, CP936 on a Chinese one) spends up to two bytes per character. */
    char uninstall_a[MAX_PATH * 2], install_a[MAX_PATH * 2];
    BOOL lossy = FALSE;

    /* lpUsedDefaultChar is the only honest "cannot represent this" signal:
     * with dwFlags 0, WideCharToMultiByte substitutes '?' for an unmappable
     * character and still returns a non-zero length, so the return value
     * alone catches nothing but a too-small buffer (WC_ERR_INVALID_CHARS
     * doesn't help — it applies to CP_UTF8/CP_GB18030 and to malformed
     * input, not to unmappable input). Letting a '?' through would not delete
     * anything dangerous — cmd rejects a wildcard in a directory component
     * (`del`) or anywhere at all (`rmdir`) with ERROR_INVALID_NAME — but it
     * reproduces this bug instead of fixing it: every line silently targets a
     * path that is not the install, the :loop spins, and the success dialog
     * fires over an install still fully on disk. Refusing is the same strict
     * stance _write_installer_script() takes on the Python side.
     *
     * The two calls must stay separate: lpUsedDefaultChar is *overwritten* by
     * each call rather than accumulated, so one shared `lossy` tested after an
     * && chain would forget a lossy first conversion whenever the second came
     * back clean — precisely the non-ASCII-username case this exists for.
     *
     * Unreachable in practice unless 8.3 generation is off, since
     * console_safe_path() above normally hands us pure ASCII. */
    if (!WideCharToMultiByte(CP_OEMCP, 0, uninstall_safe, -1,
                             uninstall_a, (int)sizeof(uninstall_a), NULL, &lossy) || lossy)
        return;
    if (!WideCharToMultiByte(CP_OEMCP, 0, install_safe, -1,
                             install_a, (int)sizeof(install_a), NULL, &lossy) || lossy)
        return;

    char script[MAX_PATH * 6 + 256];
    int len = snprintf(script, sizeof(script),
                       "@echo off\r\n"
                       "ping -n 2 127.0.0.1 >nul\r\n"
                       ":loop\r\n"
                       "del /f /q \"%s\"\r\n"
                       "if exist \"%s\" goto loop\r\n"
                       "rmdir /s /q \"%s\"\r\n"
                       "del \"%%~f0\"\r\n",
                       uninstall_a, uninstall_a, install_a);
    if (len <= 0 || len >= (int)sizeof(script)) return;

    HANDLE hf = CreateFileW(bat_path, GENERIC_WRITE, 0, NULL,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (hf == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    /* A short write must abort the whole thing, and the partial file has to go.
     * WriteFile returning TRUE with written < len is the documented disk-full
     * behaviour, and a script truncated at a path separator is not merely
     * useless — `rmdir /s /q "C:\Users\<account>\AppData\Local\` (no closing
     * quote, no CRLF) is accepted by cmd, exits 0, and recursively deletes that
     * directory. Leaving the truncated file behind would be just as bad: it
     * stays in %TEMP% where a later double-click still runs it. */
    BOOL wrote = WriteFile(hf, script, (DWORD)len, &written, NULL)
              && written == (DWORD)len;
    CloseHandle(hf);
    if (!wrote) {
        DeleteFileW(bat_path);
        return;
    }

    ShellExecuteW(NULL, L"open", bat_path, NULL, NULL, SW_HIDE);
}

/* ── Dialog procedure ─────────────────────────────────────────────────── */

static wchar_t g_install_dir[MAX_PATH];
static wchar_t g_uninstall_exe[MAX_PATH];

static INT_PTR CALLBACK DlgProc(HWND hDlg, UINT msg, WPARAM wParam, LPARAM lParam)
{
    switch (msg) {
    case WM_INITDIALOG:
        SetWindowTextW(hDlg, g_str->title);
        SetDlgItemTextW(hDlg, IDC_UNINSTALL_PROMPT, g_str->prompt);
        SetDlgItemTextW(hDlg, IDC_INSTALL,          g_str->uninstall);
        SetDlgItemTextW(hDlg, IDC_CANCEL,           g_str->cancel);
        if (!get_install_dir(g_install_dir, MAX_PATH)) {
            MessageBoxW(hDlg, g_str->not_found,
                        g_str->title, MB_OK | MB_ICONWARNING);
            EndDialog(hDlg, IDABORT);
            return TRUE;
        }
        swprintf(g_uninstall_exe, MAX_PATH, L"%s\\uninstall.exe", g_install_dir);
        return TRUE;

    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case IDC_INSTALL: {
            EnableWindow(GetDlgItem(hDlg, IDC_INSTALL), FALSE);
            EnableWindow(GetDlgItem(hDlg, IDC_CANCEL),  FALSE);

            delete_installed_files(g_install_dir);
            remove_shortcuts();
            remove_registry_entry();
            schedule_self_delete(g_uninstall_exe, g_install_dir);

            MessageBoxW(hDlg, g_str->done_msg,
                        g_str->done_title, MB_OK | MB_ICONINFORMATION);
            EndDialog(hDlg, IDOK);
            return TRUE;
        }
        case IDC_CANCEL:
            EndDialog(hDlg, IDCANCEL);
            return TRUE;
        }
        break;

    case WM_CLOSE:
        EndDialog(hDlg, IDCANCEL);
        return TRUE;
    }
    return FALSE;
}

/* ── Entry point ──────────────────────────────────────────────────────── */

int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE hPrev,
                   LPSTR lpCmdLine, int nCmdShow)
{
    (void)hPrev; (void)lpCmdLine; (void)nCmdShow;

    select_language();

    INITCOMMONCONTROLSEX icc = { sizeof(icc), ICC_STANDARD_CLASSES };
    InitCommonControlsEx(&icc);

    DialogBoxW(hInstance, MAKEINTRESOURCEW(IDD_UNINSTALL), NULL, DlgProc);
    return 0;
}
