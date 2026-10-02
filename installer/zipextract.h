#ifndef WINZAPP_ZIPEXTRACT_H
#define WINZAPP_ZIPEXTRACT_H

/* ZIP reader/extractor for the installer payload. GUI-free on purpose: the
 * installer calls it from its worker thread, and tests/c/zip_extract_cli.c
 * calls the same code from a console program.
 *
 * Understands what build.py writes (stored and DEFLATE entries, ZIP64 central
 * directory records and extra fields) behind an arbitrary prefix, because the
 * payload is appended to the installer stub. Sizes and CRC-32 come from the
 * central directory only, so entries written with a data descriptor
 * (general-purpose flag bit 3) extract the same way. Every error string is
 * ASCII technical English, meant to be appended to the installer's own
 * localised "extraction failed" text. */

#include <windows.h>
#include <stddef.h>
#include <stdint.h>

#define ZIPX_OK         0
#define ZIPX_CANCELLED  1
#define ZIPX_ERROR      2

typedef struct ZipxArchive ZipxArchive;

/* Called when an entry starts (file_started = 1, done unchanged) and again
 * after every chunk written. done/total are UNCOMPRESSED bytes of all file
 * entries, so the ratio is the real fraction of the install. name is the
 * entry's relative path, backslash-separated. */
typedef void (*ZipxProgressFn)(void *user, const wchar_t *name, int file_started,
                               uint64_t done, uint64_t total);

/* Called once per file after it was written and verified (not for
 * directories) with its full destination path. */
typedef void (*ZipxFileFn)(void *user, const wchar_t *dest_path);

/* Opens the ZIP at the end of `path` (the file may start with any prefix).
 * Returns NULL and fills err on failure. */
ZipxArchive *zipx_open(const wchar_t *path, char *err, size_t err_cap);

uint64_t zipx_entry_count(const ZipxArchive *za);
uint64_t zipx_total_bytes(const ZipxArchive *za);   /* uncompressed, files only */

#define ZIPX_SKIP_SPACE_CHECK  0x1

/* Extracts every entry under dest_dir (absolute path, created by the caller;
 * '/' separators and trailing backslashes are tolerated). Stops at the first
 * problem: ZIPX_ERROR (err filled; the entry being written is deleted), or
 * ZIPX_CANCELLED when *cancel becomes nonzero (cancel may be NULL). progress
 * and on_file may be NULL.
 *
 * Nothing is written through a junction or symlink found below dest_dir: an
 * existing reparse point on an entry's path is an error, not followed. The
 * free space of dest_dir's volume is checked against the uncompressed total
 * first unless flags has ZIPX_SKIP_SPACE_CHECK. */
int zipx_extract_all(ZipxArchive *za, const wchar_t *dest_dir,
                     ZipxProgressFn progress, ZipxFileFn on_file, void *user,
                     volatile const int *cancel, unsigned flags,
                     char *err, size_t err_cap);

/* Backslash-separated, no trailing backslash (except a drive root "C:\").
 * out may be the same buffer as in. */
void zipx_normalize_dir(const wchar_t *in, wchar_t *out, size_t out_cap);

/* Nonzero when free_bytes covers needed_bytes plus a small margin; otherwise
 * fills err ("not enough free disk space: need N MB ..."). */
int zipx_space_ok(uint64_t free_bytes, uint64_t needed_bytes,
                  char *err, size_t err_cap);

void zipx_close(ZipxArchive *za);

/* Zip-slip guard, exposed for tests: nonzero when a backslash-normalised
 * entry name stays inside the destination (no absolute or UNC path, drive
 * letter, ':' (alternate data stream), ".." segment, empty segment or
 * character Windows forbids in a file name). */
int zipx_name_is_safe(const wchar_t *name);

#endif
