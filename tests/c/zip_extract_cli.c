/* Console harness around installer/zipextract.c, built and run by
 * tests/test_installer_zip_extract.py. Never creates a window.
 *
 *   zip_extract_cli <archive> <dest_dir> [--cancel-after BYTES]
 *
 * stdout (one line each): OPEN entries=N total=T, FILE <path> per extracted
 * file, then exactly one of DONE bytes=B peak_kb=K / CANCELLED bytes=B /
 * ERROR <reason>. Exit code 0 / 3 / 2 / 1 (cannot open). */

#include <windows.h>
#include <psapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
#include "zipextract.h"

static volatile int g_cancel = 0;
static uint64_t g_cancel_after = 0;   /* 0 = never */
static uint64_t g_last_done = 0;

static void on_progress(void *user, const wchar_t *name, int file_started,
                        uint64_t done, uint64_t total)
{
    (void)user; (void)name; (void)file_started; (void)total;
    g_last_done = done;
    if (g_cancel_after && done >= g_cancel_after) g_cancel = 1;
}

static void on_file(void *user, const wchar_t *path)
{
    (void)user;
    wprintf(L"FILE %ls\n", path);
}

int wmain(int argc, wchar_t **argv)
{
    if (argc < 3) { fprintf(stderr, "usage: zip_extract_cli archive dest\n"); return 1; }
    if (argc >= 5 && wcscmp(argv[3], L"--cancel-after") == 0)
        g_cancel_after = (uint64_t)_wtoi64(argv[4]);

    char err[256] = "";
    ZipxArchive *za = zipx_open(argv[1], err, sizeof(err));
    if (!za) { printf("ERROR %s\n", err); return 1; }
    printf("OPEN entries=%llu total=%llu\n",
           (unsigned long long)zipx_entry_count(za),
           (unsigned long long)zipx_total_bytes(za));

    int rc = zipx_extract_all(za, argv[2], on_progress, on_file, NULL, &g_cancel,
                              err, sizeof(err));
    zipx_close(za);

    PROCESS_MEMORY_COUNTERS pmc = {0};
    GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc));
    if (rc == ZIPX_OK) {
        printf("DONE bytes=%llu peak_kb=%llu\n", (unsigned long long)g_last_done,
               (unsigned long long)(pmc.PeakWorkingSetSize / 1024));
        return 0;
    }
    if (rc == ZIPX_CANCELLED) {
        printf("CANCELLED bytes=%llu\n", (unsigned long long)g_last_done);
        return 3;
    }
    printf("ERROR %s\n", err);
    return 2;
}
