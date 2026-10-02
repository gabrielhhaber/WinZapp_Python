#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <shlobj.h>
#include <shlwapi.h>
#include <objbase.h>
#include <shobjidl.h>
#include <stdint.h>
#include <stdio.h>
#include "resource.h"
#include "lang.h"
#include "zipextract.h"

/* ── Localised UI strings ─────────────────────────────────────────────────
   The installer language follows the Windows display language:
   Portuguese → pt-BR, Spanish → es-ES, anything else → en-US.            */

typedef struct {
    const wchar_t *title;          /* dialog caption                  */
    const wchar_t *path_label;     /* "Installation folder:"          */
    const wchar_t *browse;         /* "Browse..." button              */
    const wchar_t *desktop_sc;     /* desktop-shortcut checkbox       */
    const wchar_t *startmenu_sc;   /* start-menu-shortcut checkbox    */
    const wchar_t *install;        /* "Install" button                */
    const wchar_t *cancel;         /* "Cancel" button                 */
    const wchar_t *browse_title;   /* folder-picker title             */
    const wchar_t *err_no_folder;  /* empty-path warning              */
    const wchar_t *extract_failed; /* extraction-failure message      */
    const wchar_t *done_msg;       /* success message                 */
    const wchar_t *done_title;     /* success message-box title       */
    const wchar_t *err_fmt;        /* error format string (%s)        */
    const wchar_t *err_title;      /* error message-box title         */
} UiStrings;

static const UiStrings STR_PT = {
    L"Instalador do WinZapp",
    L"Pasta de instalação:",
    L"Procurar...",
    L"Criar atalho na área de trabalho",
    L"Criar atalho no menu Iniciar",
    L"Instalar",
    L"Cancelar",
    L"Selecione a pasta de instalação",
    L"Por favor, selecione uma pasta de instalação.",
    L"Extração falhou.",
    L"WinZapp foi instalado com sucesso!",
    L"Instalação concluída",
    L"Ocorreu um erro durante a instalação:\n%s",
    L"Erro de instalação",
};

static const UiStrings STR_ES = {
    L"Instalador de WinZapp",
    L"Carpeta de instalación:",
    L"Examinar...",
    L"Crear acceso directo en el escritorio",
    L"Crear acceso directo en el menú Inicio",
    L"Instalar",
    L"Cancelar",
    L"Seleccione la carpeta de instalación",
    L"Por favor, seleccione una carpeta de instalación.",
    L"La extracción falló.",
    L"¡WinZapp se instaló correctamente!",
    L"Instalación completada",
    L"Se produjo un error durante la instalación:\n%s",
    L"Error de instalación",
};

static const UiStrings STR_EN = {
    L"WinZapp Installer",
    L"Installation folder:",
    L"Browse...",
    L"Create desktop shortcut",
    L"Create Start menu shortcut",
    L"Install",
    L"Cancel",
    L"Select the installation folder",
    L"Please select an installation folder.",
    L"Extraction failed.",
    L"WinZapp was installed successfully!",
    L"Installation complete",
    L"An error occurred during installation:\n%s",
    L"Installation error",
};

static const UiStrings *g_str = &STR_EN;

static void select_language(void)
{
    switch (winzapp_ui_lang()) {
    case WINZAPP_LANG_PT: g_str = &STR_PT; break;
    case WINZAPP_LANG_ES: g_str = &STR_ES; break;
    default:              g_str = &STR_EN; break;
    }
}

/* ── Custom window messages ───────────────────────────────────────────── */

#define WM_INSTALL_PROGRESS  (WM_USER + 1)   /* wParam=done, lParam=total (progress units) */
#define WM_INSTALL_DONE      (WM_USER + 2)
#define WM_INSTALL_ERROR     (WM_USER + 3)   /* lParam=wchar_t* (heap, caller frees) */

/* ── Globals ──────────────────────────────────────────────────────────── */

static HWND  g_hDlg      = NULL;
static volatile BOOL g_cancelled = FALSE;

typedef struct {
    wchar_t  install_dir[MAX_PATH];
    BOOL     desktop_sc;
    BOOL     startmenu_sc;
} InstallParams;

/* ── Extract all files from the ZIP payload ───────────────────────────────
   The ZIP parsing, inflating and path checks live in zipextract.c (shared
   with tests/c/zip_extract_cli.c); this is only the glue to the dialog.   */

#define PROGRESS_UNITS 10000   /* the bar's range, whatever the payload size */

typedef struct {
    HWND      hDlg;
    wchar_t **files;
    int       file_count;
    int       file_cap;
    int       last_units;
} ExtractUi;

/* done/total are uncompressed bytes and may exceed 2 GiB, which the 32-bit
 * int the dialog procedure reads cannot hold: the bar is driven in fixed
 * units instead. */
static void on_extract_progress(void *user, const wchar_t *name, int file_started,
                                uint64_t done, uint64_t total)
{
    ExtractUi *ui = (ExtractUi *)user;
    int units = total ? (int)(done * PROGRESS_UNITS / total) : 0;
    if (units != ui->last_units) {
        ui->last_units = units;
        SendMessage(ui->hDlg, WM_INSTALL_PROGRESS, (WPARAM)units, (LPARAM)PROGRESS_UNITS);
    }
    if (file_started) {
        const wchar_t *base = wcsrchr(name, L'\\');
        SetDlgItemTextW(ui->hDlg, IDC_STATUS, base ? base + 1 : name);
    }
}

static void on_extracted_file(void *user, const wchar_t *dest_path)
{
    ExtractUi *ui = (ExtractUi *)user;
    if (ui->file_count >= ui->file_cap) return;
    ui->files[ui->file_count] = _wcsdup(dest_path);
    if (ui->files[ui->file_count]) ui->file_count++;
}

/* err receives an ASCII technical reason when this returns FALSE without a
 * user cancel. */
static BOOL extract_all(HWND hDlg, const wchar_t *dest_dir,
                        wchar_t ***out_files, int *out_count,
                        char *err, size_t err_cap)
{
    *out_files = NULL;
    *out_count = 0;

    wchar_t exe_path[MAX_PATH];
    GetModuleFileNameW(NULL, exe_path, MAX_PATH);

    ZipxArchive *za = zipx_open(exe_path, err, err_cap);
    if (!za) return FALSE;

    ExtractUi ui = { hDlg, NULL, 0, 0, -1 };
    ui.file_cap = (int)zipx_entry_count(za);
    ui.files = (wchar_t **)malloc((size_t)(ui.file_cap ? ui.file_cap : 1) * sizeof(wchar_t *));
    if (!ui.files) {
        snprintf(err, err_cap, "out of memory");
        zipx_close(za);
        return FALSE;
    }

    int rc = zipx_extract_all(za, dest_dir, on_extract_progress, on_extracted_file,
                              &ui, &g_cancelled, err, err_cap);
    zipx_close(za);

    *out_files = ui.files;
    *out_count = ui.file_count;
    return rc == ZIPX_OK;
}

/* ── Shortcut creation ────────────────────────────────────────────────── */

static void create_shortcut(const wchar_t *target, const wchar_t *link_path,
                            const wchar_t *working_dir)
{
    CoInitialize(NULL);
    IShellLinkW *psl = NULL;
    HRESULT hr = CoCreateInstance(&CLSID_ShellLink, NULL, CLSCTX_INPROC_SERVER,
                                  &IID_IShellLinkW, (void **)&psl);
    if (FAILED(hr)) { CoUninitialize(); return; }

    IShellLinkW_SetPath(psl, target);
    IShellLinkW_SetWorkingDirectory(psl, working_dir);

    IPersistFile *ppf = NULL;
    if (SUCCEEDED(IShellLinkW_QueryInterface(psl, &IID_IPersistFile, (void **)&ppf))) {
        IPersistFile_Save(ppf, link_path, TRUE);
        IPersistFile_Release(ppf);
    }
    IShellLinkW_Release(psl);
    CoUninitialize();
}

/* ── Registry ─────────────────────────────────────────────────────────── */

/* Version string embedded by build.py at compile time from
 * client/version.py's __version__ (-DWINZAPP_VERSION=L"..."), so
 * "Add or Remove Programs" shows the version that was actually installed
 * instead of a permanently-stale placeholder. */
#ifndef WINZAPP_VERSION
#define WINZAPP_VERSION L"0.0.0"
#endif

static void register_uninstall(const wchar_t *install_dir,
                                const wchar_t *uninstall_exe)
{
    HKEY hkey;
    const wchar_t *key_path =
        L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\WinZapp";

    /* The default install location (%LOCALAPPDATA%\WinZapp) needs no
     * elevation, so this process is usually NOT admin — writing straight to
     * HKEY_LOCAL_MACHINE then fails with ERROR_ACCESS_DENIED and silently
     * skips the whole uninstall registration, leaving the app installed with
     * no entry in "Add or Remove Programs" and no way for uninstall.exe to
     * find itself later. Try the machine-wide hive first (works when the
     * install dir did require elevation, e.g. Program Files), and fall back
     * to the per-user hive — which every installer process can always write
     * to, elevated or not, and which Windows reads "Add or Remove Programs"
     * entries from exactly the same way. */
    LSTATUS status = RegCreateKeyExW(HKEY_LOCAL_MACHINE, key_path, 0, NULL,
                        REG_OPTION_NON_VOLATILE, KEY_WRITE, NULL, &hkey, NULL);
    if (status != ERROR_SUCCESS) {
        status = RegCreateKeyExW(HKEY_CURRENT_USER, key_path, 0, NULL,
                        REG_OPTION_NON_VOLATILE, KEY_WRITE, NULL, &hkey, NULL);
    }
    if (status != ERROR_SUCCESS)
        return;

    RegSetValueExW(hkey, L"DisplayName", 0, REG_SZ,
                   (BYTE *)L"WinZapp", sizeof(L"WinZapp"));
    RegSetValueExW(hkey, L"UninstallString", 0, REG_SZ,
                   (BYTE *)uninstall_exe,
                   (DWORD)((wcslen(uninstall_exe) + 1) * sizeof(wchar_t)));
    RegSetValueExW(hkey, L"InstallLocation", 0, REG_SZ,
                   (BYTE *)install_dir,
                   (DWORD)((wcslen(install_dir) + 1) * sizeof(wchar_t)));
    RegSetValueExW(hkey, L"DisplayVersion", 0, REG_SZ,
                   (BYTE *)WINZAPP_VERSION, (DWORD)sizeof(WINZAPP_VERSION));
    RegSetValueExW(hkey, L"Publisher", 0, REG_SZ,
                   (BYTE *)L"WinZapp", sizeof(L"WinZapp"));
    DWORD one = 1;
    RegSetValueExW(hkey, L"NoModify", 0, REG_DWORD, (BYTE *)&one, sizeof(DWORD));
    RegSetValueExW(hkey, L"NoRepair", 0, REG_DWORD, (BYTE *)&one, sizeof(DWORD));
    RegCloseKey(hkey);
}

/* ── Write installed-files manifest (UTF-16LE) ────────────────────────── */

static void write_file_list(const wchar_t *install_dir,
                             wchar_t **files, int count)
{
    wchar_t list_path[MAX_PATH];
    swprintf(list_path, MAX_PATH, L"%s\\installed_files.dat", install_dir);

    HANDLE hf = CreateFileW(list_path, GENERIC_WRITE, 0, NULL,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (hf == INVALID_HANDLE_VALUE) return;

    /* UTF-16LE BOM */
    WORD bom = 0xFEFF;
    DWORD written;
    WriteFile(hf, &bom, sizeof(bom), &written, NULL);

    for (int i = 0; i < count; i++) {
        WriteFile(hf, files[i],
                  (DWORD)(wcslen(files[i]) * sizeof(wchar_t)), &written, NULL);
        WriteFile(hf, L"\r\n", 4, &written, NULL);   /* 4 bytes = \r\n in UTF-16LE */
    }
    CloseHandle(hf);
}

/* ── Install thread ───────────────────────────────────────────────────── */

static DWORD WINAPI install_thread(LPVOID param)
{
    InstallParams *p = (InstallParams *)param;

    SHCreateDirectoryExW(NULL, p->install_dir, NULL);

    wchar_t **files    = NULL;
    int       file_count = 0;
    char err[200] = "";
    BOOL ok = extract_all(g_hDlg, p->install_dir, &files, &file_count,
                          err, sizeof(err));

    if (!ok || g_cancelled) {
        if (files) {
            for (int i = 0; i < file_count; i++) free(files[i]);
            free(files);
        }
        free(p);
        if (!g_cancelled) {
            /* The localised sentence, then the ASCII technical reason: the
             * dialog's own "%s" format wraps the whole of it. */
            wchar_t *msg = (wchar_t *)calloc(400, sizeof(wchar_t));
            if (msg) {
                wcsncpy(msg, g_str->extract_failed, 100);
                size_t used = wcslen(msg);
                msg[used++] = L' ';
                MultiByteToWideChar(CP_ACP, 0, err, -1, msg + used, (int)(399 - used));
            }
            SendMessage(g_hDlg, WM_INSTALL_ERROR, 0, (LPARAM)msg);
        }
        return 0;
    }

    /* WinZapp.exe is at install_dir root (single onefile exe) */
    wchar_t exe_path[MAX_PATH];
    swprintf(exe_path, MAX_PATH, L"%s\\WinZapp.exe", p->install_dir);

    if (p->desktop_sc) {
        wchar_t desktop[MAX_PATH], link[MAX_PATH];
        SHGetFolderPathW(NULL, CSIDL_DESKTOPDIRECTORY, NULL, 0, desktop);
        swprintf(link, MAX_PATH, L"%s\\WinZapp.lnk", desktop);
        create_shortcut(exe_path, link, p->install_dir);
    }

    if (p->startmenu_sc) {
        wchar_t programs[MAX_PATH], link[MAX_PATH];
        SHGetFolderPathW(NULL, CSIDL_COMMON_PROGRAMS, NULL, 0, programs);
        swprintf(link, MAX_PATH, L"%s\\WinZapp.lnk", programs);
        create_shortcut(exe_path, link, p->install_dir);
    }

    wchar_t uninstall_exe[MAX_PATH];
    swprintf(uninstall_exe, MAX_PATH, L"%s\\uninstall.exe", p->install_dir);

    register_uninstall(p->install_dir, uninstall_exe);
    write_file_list(p->install_dir, files, file_count);

    for (int i = 0; i < file_count; i++) free(files[i]);
    free(files);
    free(p);

    SendMessage(g_hDlg, WM_INSTALL_DONE, 0, 0);
    return 0;
}

/* ── Dialog procedure ─────────────────────────────────────────────────── */

static INT_PTR CALLBACK DlgProc(HWND hDlg, UINT msg, WPARAM wParam, LPARAM lParam)
{
    switch (msg) {
    case WM_INITDIALOG: {
        g_hDlg = hDlg;

        /* Apply localised strings (language follows the Windows UI language) */
        SetWindowTextW(hDlg, g_str->title);
        SetDlgItemTextW(hDlg, IDC_PATH_LABEL,   g_str->path_label);
        SetDlgItemTextW(hDlg, IDC_BROWSE,       g_str->browse);
        SetDlgItemTextW(hDlg, IDC_DESKTOP_SC,   g_str->desktop_sc);
        SetDlgItemTextW(hDlg, IDC_STARTMENU_SC, g_str->startmenu_sc);
        SetDlgItemTextW(hDlg, IDC_INSTALL,      g_str->install);
        SetDlgItemTextW(hDlg, IDC_CANCEL,       g_str->cancel);

        wchar_t local_app[MAX_PATH];
        if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, 0, local_app))) {
            wchar_t def_path[MAX_PATH];
            swprintf(def_path, MAX_PATH, L"%s\\WinZapp", local_app);
            SetDlgItemTextW(hDlg, IDC_INSTALL_PATH, def_path);
        }

        CheckDlgButton(hDlg, IDC_DESKTOP_SC,   BST_CHECKED);
        CheckDlgButton(hDlg, IDC_STARTMENU_SC, BST_CHECKED);

        SendDlgItemMessage(hDlg, IDC_PROGRESS, PBM_SETRANGE32, 0, 100);
        return TRUE;
    }

    case WM_COMMAND:
        switch (LOWORD(wParam)) {
        case IDC_BROWSE: {
            BROWSEINFOW bi = {0};
            bi.hwndOwner = hDlg;
            bi.lpszTitle = g_str->browse_title;
            bi.ulFlags   = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE;
            LPITEMIDLIST pidl = SHBrowseForFolderW(&bi);
            if (pidl) {
                wchar_t path[MAX_PATH];
                if (SHGetPathFromIDListW(pidl, path))
                    SetDlgItemTextW(hDlg, IDC_INSTALL_PATH, path);
                CoTaskMemFree(pidl);
            }
            return TRUE;
        }

        case IDC_INSTALL: {
            wchar_t install_dir[MAX_PATH];
            GetDlgItemTextW(hDlg, IDC_INSTALL_PATH, install_dir, MAX_PATH);
            if (!install_dir[0]) {
                MessageBoxW(hDlg, g_str->err_no_folder,
                            L"WinZapp", MB_OK | MB_ICONWARNING);
                return TRUE;
            }

            EnableWindow(GetDlgItem(hDlg, IDC_INSTALL), FALSE);
            EnableWindow(GetDlgItem(hDlg, IDC_CANCEL),  FALSE);
            EnableWindow(GetDlgItem(hDlg, IDC_BROWSE),  FALSE);

            InstallParams *params = (InstallParams *)malloc(sizeof(InstallParams));
            wcsncpy(params->install_dir, install_dir, MAX_PATH - 1);
            params->install_dir[MAX_PATH - 1] = L'\0';
            params->desktop_sc   = IsDlgButtonChecked(hDlg, IDC_DESKTOP_SC)   == BST_CHECKED;
            params->startmenu_sc = IsDlgButtonChecked(hDlg, IDC_STARTMENU_SC) == BST_CHECKED;

            HANDLE hThread = CreateThread(NULL, 0, install_thread, params, 0, NULL);
            if (hThread) CloseHandle(hThread);
            return TRUE;
        }

        case IDC_CANCEL:
            g_cancelled = TRUE;
            EndDialog(hDlg, IDCANCEL);
            return TRUE;
        }
        break;

    case WM_INSTALL_PROGRESS: {
        int done  = (int)wParam;
        int total = (int)lParam;
        if (total > 0) {
            SendDlgItemMessage(hDlg, IDC_PROGRESS, PBM_SETRANGE32, 0, total);
            SendDlgItemMessage(hDlg, IDC_PROGRESS, PBM_SETPOS,     done, 0);
        }
        return TRUE;
    }

    case WM_INSTALL_DONE:
        MessageBoxW(hDlg, g_str->done_msg,
                    g_str->done_title, MB_OK | MB_ICONINFORMATION);
        EndDialog(hDlg, IDOK);
        return TRUE;

    case WM_INSTALL_ERROR: {
        wchar_t *err = (wchar_t *)lParam;
        wchar_t buf[512];
        swprintf(buf, 512, g_str->err_fmt, err ? err : L"");
        free(err);
        MessageBoxW(hDlg, buf, g_str->err_title, MB_OK | MB_ICONERROR);
        EndDialog(hDlg, IDABORT);
        return TRUE;
    }

    case WM_CLOSE:
        g_cancelled = TRUE;
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

    INITCOMMONCONTROLSEX icc = { sizeof(icc), ICC_PROGRESS_CLASS };
    InitCommonControlsEx(&icc);

    DialogBoxW(hInstance, MAKEINTRESOURCEW(IDD_INSTALL), NULL, DlgProc);
    return 0;
}
