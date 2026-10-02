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

/* ── ZIP structures (no compression — ZIP_STORED only) ───────────────── */

#define ZIP_LOCAL_SIG  0x04034b50UL
#define ZIP_CD_SIG     0x02014b50UL
#define ZIP_EOCD_SIG   0x06054b50UL

#pragma pack(push, 1)
typedef struct {
    uint32_t sig;
    uint16_t ver_needed;
    uint16_t flags;
    uint16_t compression;
    uint16_t mod_time;
    uint16_t mod_date;
    uint32_t crc32;
    uint32_t comp_size;
    uint32_t uncomp_size;
    uint16_t fname_len;
    uint16_t extra_len;
} ZipLocal;

typedef struct {
    uint32_t sig;
    uint16_t ver_made;
    uint16_t ver_needed;
    uint16_t flags;
    uint16_t compression;
    uint16_t mod_time;
    uint16_t mod_date;
    uint32_t crc32;
    uint32_t comp_size;
    uint32_t uncomp_size;
    uint16_t fname_len;
    uint16_t extra_len;
    uint16_t comment_len;
    uint16_t disk_start;
    uint16_t int_attr;
    uint32_t ext_attr;
    uint32_t local_off;
} ZipCentral;

typedef struct {
    uint32_t sig;
    uint16_t disk_num;
    uint16_t cd_disk;
    uint16_t cd_entries_disk;
    uint16_t cd_entries_total;
    uint32_t cd_size;
    uint32_t cd_off;
    uint16_t comment_len;
} ZipEOCD;

/* ZIP64 variants of the EOCD, used whenever the classic 16/32-bit fields
 * above can't hold the real value (more than 65535 entries, or a central
 * directory 4GiB or larger) — routine once client/api/'s node_modules
 * (WPPConnect + Puppeteer's bundled Chromium) is packed into the payload.
 * When that happens the classic EOCD's cd_entries_total/cd_off/cd_size are
 * written as the sentinel 0xFFFF/0xFFFFFFFF, and the real values live here
 * instead — a reader that only understands the classic record used to take
 * those sentinels (or whatever they wrapped around to) at face value and
 * silently stop after however many files that implied, while still
 * reporting a successful install. */
#define ZIP64_EOCD_LOCATOR_SIG 0x07064b50UL
#define ZIP64_EOCD_SIG         0x06064b50UL

typedef struct {
    uint32_t sig;
    uint32_t eocd_disk;
    uint64_t zip64_eocd_offset;   /* unused — the record is located by
                                      scanning instead, see find_zip_info() */
    uint32_t total_disks;
} Zip64EocdLocator;

typedef struct {
    uint32_t sig;
    uint64_t record_size;         /* size of the rest of this record, i.e.
                                      excluding this field and sig */
    uint16_t ver_made;
    uint16_t ver_needed;
    uint32_t disk_num;
    uint32_t cd_disk;
    uint64_t cd_entries_disk;
    uint64_t cd_entries_total;
    uint64_t cd_size;
    uint64_t cd_off;
} Zip64Eocd;
#pragma pack(pop)

/* What extract_all() actually needs, resolved from either the classic EOCD
 * or its ZIP64 counterpart — the rest of the extraction code never has to
 * know which one the payload actually used. */
typedef struct {
    uint64_t zip_start;      /* physical file offset where the ZIP's own
                                 byte 0 lives (payload is appended after the
                                 installer stub, so this is never 0) */
    uint64_t cd_off;         /* zip-relative */
    uint64_t cd_size;
    uint64_t total_entries;
} ZipInfo;

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

#define WM_INSTALL_PROGRESS  (WM_USER + 1)   /* wParam=done, lParam=total */
#define WM_INSTALL_DONE      (WM_USER + 2)
#define WM_INSTALL_ERROR     (WM_USER + 3)   /* lParam=wchar_t* (heap, caller frees) */

/* ── Globals ──────────────────────────────────────────────────────────── */

static HWND  g_hDlg      = NULL;
static BOOL  g_cancelled = FALSE;

typedef struct {
    wchar_t  install_dir[MAX_PATH];
    BOOL     desktop_sc;
    BOOL     startmenu_sc;
} InstallParams;

/* ── ZIP helpers ──────────────────────────────────────────────────────── */

static BOOL read_at(HANDLE hf, uint64_t offset, void *buf, DWORD len)
{
    LARGE_INTEGER li;
    li.QuadPart = (LONGLONG)offset;
    if (!SetFilePointerEx(hf, li, NULL, FILE_BEGIN)) return FALSE;
    DWORD did = 0;
    return ReadFile(hf, buf, len, &did, NULL) && did == len;
}

static BOOL find_zip_info(HANDLE hf, ZipInfo *out)
{
    LARGE_INTEGER fs_li;
    if (!GetFileSizeEx(hf, &fs_li)) return FALSE;
    uint64_t fsize = (uint64_t)fs_li.QuadPart;

    uint64_t scan_size = 65536 + sizeof(ZipEOCD) + 65535;
    if (scan_size > fsize) scan_size = fsize;
    uint64_t scan_start = fsize - scan_size;

    uint8_t *buf = (uint8_t *)malloc((size_t)scan_size);
    if (!buf) return FALSE;

    if (!read_at(hf, scan_start, buf, (DWORD)scan_size)) { free(buf); return FALSE; }

    ZipEOCD eocd = {0};
    int64_t eocd_i = -1;
    for (int64_t i = (int64_t)(scan_size - sizeof(ZipEOCD)); i >= 0; i--) {
        uint32_t sig;
        memcpy(&sig, buf + i, 4);
        if (sig == ZIP_EOCD_SIG) {
            memcpy(&eocd, buf + i, sizeof(ZipEOCD));
            eocd_i = i;
            break;
        }
    }
    if (eocd_i < 0) { free(buf); return FALSE; }
    uint64_t eocd_abs = scan_start + (uint64_t)eocd_i;

    BOOL needs_zip64 = (eocd.cd_entries_total == 0xFFFF) ||
                        (eocd.cd_off == 0xFFFFFFFFUL) ||
                        (eocd.cd_size == 0xFFFFFFFFUL);

    if (!needs_zip64) {
        free(buf);
        out->zip_start     = eocd_abs - (uint64_t)eocd.cd_size - (uint64_t)eocd.cd_off;
        out->cd_off        = eocd.cd_off;
        out->cd_size       = eocd.cd_size;
        out->total_entries = eocd.cd_entries_total;
        return TRUE;
    }

    /* ZIP64 end of central directory locator: a fixed 20 bytes, always
     * immediately before the classic EOCD record — a spec-mandated
     * position, not something to search for. */
    int64_t locator_i = eocd_i - (int64_t)sizeof(Zip64EocdLocator);
    if (locator_i < 0) { free(buf); return FALSE; }
    Zip64EocdLocator loc = {0};
    memcpy(&loc, buf + locator_i, sizeof(loc));
    if (loc.sig != ZIP64_EOCD_LOCATOR_SIG) { free(buf); return FALSE; }

    /* The ZIP64 end of central directory record itself precedes the
     * locator. Its declared size can vary (an optional extensible data
     * sector may follow the fixed fields read into Zip64Eocd), so its
     * start is found by scanning for the signature rather than assuming
     * every archive this installer is ever handed writes the fixed
     * 56-byte minimum with none — which is what build.py's own call into
     * Python's zipfile module happens to do, but nothing here should rely
     * on that holding forever. */
    int64_t z64_i = -1;
    Zip64Eocd z64 = {0};
    int64_t earliest = locator_i - (int64_t)sizeof(Zip64Eocd) - 65536;
    if (earliest < 0) earliest = 0;
    for (int64_t i = locator_i - (int64_t)sizeof(Zip64Eocd); i >= earliest; i--) {
        uint32_t sig;
        memcpy(&sig, buf + i, 4);
        if (sig == ZIP64_EOCD_SIG) {
            memcpy(&z64, buf + i, sizeof(z64));
            z64_i = i;
            break;
        }
    }
    free(buf);
    if (z64_i < 0) return FALSE;

    uint64_t z64_abs = scan_start + (uint64_t)z64_i;
    out->zip_start     = z64_abs - z64.cd_size - z64.cd_off;
    out->cd_off        = z64.cd_off;
    out->cd_size       = z64.cd_size;
    out->total_entries = z64.cd_entries_total;
    return TRUE;
}

/* Long-path buffer size. Node's node_modules/ trees routinely nest deep
 * enough that dest_dir + relative path exceeds the classic MAX_PATH (260)
 * limit once the install dir itself is a few directories deep. */
#define WZ_MAX_PATH 32768

/* Build an extended-length (\\?\) form of an absolute path so
 * CreateFileW/CreateDirectoryW are not limited to MAX_PATH. \\?\ paths must
 * be absolute with backslash separators, which dest_dir/dest_path already
 * are here, so a plain drive-letter check is enough to decide whether to
 * prefix. */
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

/* Create all intermediate directories for a file path */
static void ensure_dirs(const wchar_t *path)
{
    wchar_t tmp[WZ_MAX_PATH];
    wcsncpy(tmp, path, WZ_MAX_PATH - 1);
    tmp[WZ_MAX_PATH - 1] = L'\0';
    wchar_t *p = tmp;
    if (p[1] == L':') p += 3;
    for (; *p; p++) {
        if (*p == L'\\' || *p == L'/') {
            *p = L'\0';
            wchar_t ext[WZ_MAX_PATH];
            to_extended_path(ext, WZ_MAX_PATH, tmp);
            CreateDirectoryW(ext, NULL);
            *p = L'\\';
        }
    }
}

/* Reject a ZIP entry name that could resolve outside dest_dir once joined to
 * it — an absolute path (drive letter, UNC, or a leading separator) or a
 * ".." segment walking back out. Mirrors updater.py's _safe_extract_zip(),
 * which already guards the same thing on the self-update path; this side
 * never had the equivalent check. Defence in depth: build.py (the only
 * source this installer is ever handed) is trusted, but there is no reason
 * for the extractor itself to trust every byte of a payload wholesale.
 * Called after separator normalisation, so only backslash needs checking. */
static BOOL is_path_within_dest(const wchar_t *name)
{
    if (name[0] == L'\0') return FALSE;
    if (name[0] == L'\\') return FALSE;
    if (((name[0] >= L'A' && name[0] <= L'Z') || (name[0] >= L'a' && name[0] <= L'z'))
        && name[1] == L':') return FALSE;

    for (const wchar_t *p = name; *p; p++) {
        if (p[0] == L'.' && p[1] == L'.' &&
            (p == name || p[-1] == L'\\') &&
            (p[2] == L'\0' || p[2] == L'\\')) {
            return FALSE;
        }
    }
    return TRUE;
}

/* ── Extract all files from ZIP payload ───────────────────────────────── */

static BOOL extract_all(HWND hDlg, const wchar_t *dest_dir,
                        wchar_t ***out_files, int *out_count)
{
    wchar_t exe_path[MAX_PATH];
    GetModuleFileNameW(NULL, exe_path, MAX_PATH);

    HANDLE hf = CreateFileW(exe_path, GENERIC_READ, FILE_SHARE_READ,
                            NULL, OPEN_EXISTING, 0, NULL);
    if (hf == INVALID_HANDLE_VALUE) return FALSE;

    ZipInfo info = {0};
    if (!find_zip_info(hf, &info)) { CloseHandle(hf); return FALSE; }

    /* Sanity cap, not a realistic limit: guards the malloc below against a
     * corrupted/truncated EOCD rather than any build this project could
     * ever actually produce. */
    if (info.total_entries > 5000000) { CloseHandle(hf); return FALSE; }
    int total = (int)info.total_entries;
    uint64_t zip_start = info.zip_start;
    uint64_t cd_pos = zip_start + info.cd_off;

    wchar_t **files = (wchar_t **)malloc((size_t)total * sizeof(wchar_t *));
    int file_count = 0;
    int failed_count = 0;

    for (int i = 0; i < total && !g_cancelled; i++) {
        ZipCentral cd = {0};
        if (!read_at(hf, cd_pos, &cd, sizeof(ZipCentral))) break;
        if (cd.sig != ZIP_CD_SIG) break;

        /* Read filename (UTF-8 in ZIP) */
        char fname_utf8[512] = {0};
        int name_len = cd.fname_len < 511 ? cd.fname_len : 511;
        read_at(hf, cd_pos + sizeof(ZipCentral), fname_utf8, name_len);
        fname_utf8[name_len] = '\0';

        cd_pos += sizeof(ZipCentral) + cd.fname_len + cd.extra_len + cd.comment_len;

        /* Skip directory entries */
        if (name_len > 0 &&
            (fname_utf8[name_len - 1] == '/' || fname_utf8[name_len - 1] == '\\'))
            continue;

        /* Convert filename to wide and normalise separators */
        wchar_t fname_w[512];
        MultiByteToWideChar(CP_UTF8, 0, fname_utf8, -1, fname_w, 512);
        for (wchar_t *pw = fname_w; *pw; pw++)
            if (*pw == L'/') *pw = L'\\';

        if (!is_path_within_dest(fname_w)) { failed_count++; continue; }

        wchar_t dest_path[WZ_MAX_PATH];
        swprintf(dest_path, WZ_MAX_PATH, L"%s\\%s", dest_dir, fname_w);

        ensure_dirs(dest_path);

        /* Read local file header to find data offset */
        ZipLocal local = {0};
        uint64_t local_off = zip_start + cd.local_off;
        if (!read_at(hf, local_off, &local, sizeof(ZipLocal))) { failed_count++; continue; }
        if (local.sig != ZIP_LOCAL_SIG) { failed_count++; continue; }

        uint64_t data_off = local_off + sizeof(ZipLocal)
                          + local.fname_len + local.extra_len;

        /* Extract file. A failure here (permission denied, disk full, path
         * still rejected even in extended form, ...) used to be silently
         * skipped via `continue` while the function still reported overall
         * success — the installer would then claim "installed successfully"
         * with files actually missing. Track it and fail the whole install
         * instead, so the user gets the existing extract-failed error dialog
         * rather than a broken, partially-installed app. */
        wchar_t dest_path_ext[WZ_MAX_PATH];
        to_extended_path(dest_path_ext, WZ_MAX_PATH, dest_path);
        HANDLE hout = CreateFileW(dest_path_ext, GENERIC_WRITE, 0, NULL,
                                  CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (hout == INVALID_HANDLE_VALUE) { failed_count++; continue; }

        uint32_t remaining = cd.comp_size;
        uint8_t  chunk[65536];
        uint64_t read_pos = data_off;
        while (remaining > 0) {
            DWORD to_read = remaining < sizeof(chunk) ? remaining : sizeof(chunk);
            DWORD did_read = 0;
            LARGE_INTEGER li; li.QuadPart = (LONGLONG)read_pos;
            SetFilePointerEx(hf, li, NULL, FILE_BEGIN);
            if (!ReadFile(hf, chunk, to_read, &did_read, NULL) || did_read == 0) break;
            DWORD written = 0;
            WriteFile(hout, chunk, did_read, &written, NULL);
            read_pos  += did_read;
            remaining -= did_read;
        }
        CloseHandle(hout);
        if (remaining > 0) failed_count++;  /* short read: file truncated */

        /* Add to file list */
        files[file_count] = (wchar_t *)malloc((wcslen(dest_path) + 1) * sizeof(wchar_t));
        if (files[file_count]) {
            wcscpy(files[file_count], dest_path);
            file_count++;
        }

        /* Update progress bar and status label */
        SendMessage(hDlg, WM_INSTALL_PROGRESS, (WPARAM)(i + 1), (LPARAM)total);
        wchar_t *base = wcsrchr(fname_w, L'\\');
        SetDlgItemTextW(hDlg, IDC_STATUS, base ? base + 1 : fname_w);
    }

    CloseHandle(hf);
    *out_files  = files;
    *out_count  = file_count;
    return !g_cancelled && failed_count == 0;
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
    BOOL ok = extract_all(g_hDlg, p->install_dir, &files, &file_count);

    if (!ok || g_cancelled) {
        if (files) {
            for (int i = 0; i < file_count; i++) free(files[i]);
            free(files);
        }
        free(p);
        if (!g_cancelled)
            SendMessage(g_hDlg, WM_INSTALL_ERROR, 0,
                        (LPARAM)_wcsdup(g_str->extract_failed));
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
