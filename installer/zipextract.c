#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <zlib.h>
#include "zipextract.h"

/* ── ZIP record layout ───────────────────────────────────────────────────
 * Records are read field by field from byte offsets instead of through
 * packed structs: the central directory lives in one heap buffer, and every
 * read is bounds-checked against it. */

#define ZIP_LOCAL_SIG        0x04034b50UL
#define ZIP_CD_SIG           0x02014b50UL
#define ZIP_EOCD_SIG         0x06054b50UL
#define ZIP64_EOCD_SIG       0x06064b50UL
#define ZIP64_EOCD_LOC_SIG   0x07064b50UL

#define ZIP_LOCAL_SIZE   30
#define ZIP_CD_SIZE      46
#define ZIP_EOCD_SIZE    22
#define ZIP64_LOC_SIZE   20
#define ZIP64_EOCD_SIZE  56

#define ZIP_FLAG_ENCRYPTED  0x0001

/* ZIP64 variants of the EOCD, used whenever the classic 16/32-bit fields
 * can't hold the real value (more than 65535 entries, or a central directory
 * 4GiB or larger) — routine once client/api/'s node_modules (WPPConnect +
 * Puppeteer's bundled Chromium) is packed into the payload. The classic
 * record then carries the sentinel 0xFFFF/0xFFFFFFFF and the real values
 * live in the ZIP64 record; a reader that only understood the classic one
 * used to take the sentinels at face value and silently stop after however
 * many files that implied, while still reporting a successful install. */

/* Sanity caps, not realistic limits: they guard the allocations below
 * against a corrupted EOCD rather than any build this project could make. */
#define ZIPX_MAX_ENTRIES   5000000
#define ZIPX_MAX_CD_BYTES  ((uint64_t)1 << 30)

/* Long-path buffer size. Node's node_modules/ trees routinely nest deep
 * enough that dest_dir + relative path exceeds the classic MAX_PATH (260)
 * limit once the install dir itself is a few directories deep. */
#define WZ_MAX_PATH 32768

/* Streaming chunk sizes: an entry is never held in memory, only these two
 * buffers (allocated once per extraction). */
#define IN_CHUNK   (256 * 1024)
#define OUT_CHUNK  (1024 * 1024)

struct ZipxArchive {
    HANDLE   hf;
    uint64_t file_size;
    uint64_t zip_start;      /* physical offset of the ZIP's own byte 0
                                (payload is appended after the installer
                                stub, so this is never 0 there) */
    uint64_t cd_off;         /* zip-relative */
    uint64_t entries;
    uint8_t *cd;             /* the whole central directory */
    size_t   cd_len;
    uint64_t total_bytes;
};

static uint16_t rd16(const uint8_t *p) { uint16_t v; memcpy(&v, p, 2); return v; }
static uint32_t rd32(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }
static uint64_t rd64(const uint8_t *p) { uint64_t v; memcpy(&v, p, 8); return v; }

static void set_err(char *err, size_t cap, const char *fmt, ...)
{
    if (!err || cap == 0) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, cap, fmt, ap);
    va_end(ap);
    err[cap - 1] = '\0';
}

/* A name made safe for an ASCII error string. */
static void ascii_name(char *out, size_t cap, const wchar_t *name)
{
    size_t i = 0;
    for (; name[i] && i + 1 < cap && i < 100; i++)
        out[i] = (name[i] >= 0x20 && name[i] < 0x7F) ? (char)name[i] : '?';
    out[i] = '\0';
}

static BOOL read_at(HANDLE hf, uint64_t offset, void *buf, DWORD len)
{
    LARGE_INTEGER li;
    li.QuadPart = (LONGLONG)offset;
    if (!SetFilePointerEx(hf, li, NULL, FILE_BEGIN)) return FALSE;
    DWORD did = 0;
    return ReadFile(hf, buf, len, &did, NULL) && did == len;
}

/* Locates the central directory from the classic EOCD or its ZIP64
 * counterpart. */
static BOOL find_zip_info(ZipxArchive *za, char *err, size_t cap)
{
    uint64_t fsize = za->file_size;
    uint64_t scan_size = 65536 + ZIP_EOCD_SIZE + 65535;
    if (scan_size > fsize) scan_size = fsize;
    if (scan_size < ZIP_EOCD_SIZE) {
        set_err(err, cap, "file too small to hold a zip archive");
        return FALSE;
    }
    uint64_t scan_start = fsize - scan_size;

    uint8_t *buf = (uint8_t *)malloc((size_t)scan_size);
    if (!buf) { set_err(err, cap, "out of memory"); return FALSE; }
    if (!read_at(za->hf, scan_start, buf, (DWORD)scan_size)) {
        free(buf);
        set_err(err, cap, "cannot read end of file (error %lu)", GetLastError());
        return FALSE;
    }

    /* The payload may be followed by data the installer did not write (an
     * Authenticode signature appended after signing), so the EOCD is searched
     * for backwards instead of being expected at the very end. */
    int64_t eocd_i = -1;
    for (int64_t i = (int64_t)(scan_size - ZIP_EOCD_SIZE); i >= 0; i--) {
        if (rd32(buf + i) == ZIP_EOCD_SIG &&
            (uint64_t)i + ZIP_EOCD_SIZE + rd16(buf + i + 20) <= scan_size) {
            eocd_i = i;
            break;
        }
    }
    if (eocd_i < 0) {
        free(buf);
        set_err(err, cap, "end of central directory not found (truncated payload)");
        return FALSE;
    }
    uint64_t eocd_abs = scan_start + (uint64_t)eocd_i;
    const uint8_t *e = buf + eocd_i;
    uint64_t entries = rd16(e + 10);
    uint64_t cd_size = rd32(e + 12);
    uint64_t cd_off  = rd32(e + 16);

    BOOL needs_zip64 = (entries == 0xFFFF) || (cd_off == 0xFFFFFFFFUL) ||
                       (cd_size == 0xFFFFFFFFUL);
    uint64_t end_of_cd_abs = eocd_abs;   /* where the CD must end */

    if (needs_zip64) {
        /* ZIP64 end of central directory locator: a fixed 20 bytes, always
         * immediately before the classic EOCD record — a spec-mandated
         * position, not something to search for. */
        int64_t locator_i = eocd_i - ZIP64_LOC_SIZE;
        if (locator_i < 0 || rd32(buf + locator_i) != ZIP64_EOCD_LOC_SIG) {
            free(buf);
            set_err(err, cap, "zip64 end of central directory locator missing");
            return FALSE;
        }
        /* The ZIP64 record itself precedes the locator. Its declared size
         * can vary (an optional extensible data sector may follow the fixed
         * fields), so its start is found by scanning for the signature
         * rather than assuming the fixed 56-byte minimum. */
        int64_t z64_i = -1;
        int64_t earliest = locator_i - ZIP64_EOCD_SIZE - 65536;
        if (earliest < 0) earliest = 0;
        for (int64_t i = locator_i - ZIP64_EOCD_SIZE; i >= earliest; i--) {
            if (rd32(buf + i) == ZIP64_EOCD_SIG) { z64_i = i; break; }
        }
        if (z64_i < 0) {
            free(buf);
            set_err(err, cap, "zip64 end of central directory record not found");
            return FALSE;
        }
        const uint8_t *z = buf + z64_i;
        entries = rd64(z + 32);
        cd_size = rd64(z + 40);
        cd_off  = rd64(z + 48);
        end_of_cd_abs = scan_start + (uint64_t)z64_i;
    }
    free(buf);

    if (cd_size + cd_off > end_of_cd_abs) {
        set_err(err, cap, "central directory does not fit the file (truncated payload)");
        return FALSE;
    }
    if (entries > ZIPX_MAX_ENTRIES || cd_size > ZIPX_MAX_CD_BYTES) {
        set_err(err, cap, "implausible central directory (%llu entries, %llu bytes)",
                (unsigned long long)entries, (unsigned long long)cd_size);
        return FALSE;
    }
    za->zip_start = end_of_cd_abs - cd_size - cd_off;
    za->cd_off    = cd_off;
    za->entries   = entries;
    za->cd_len    = (size_t)cd_size;
    return TRUE;
}

/* One central-directory entry, already resolved to 64-bit sizes. */
typedef struct {
    uint16_t flags;
    uint16_t method;
    uint32_t crc;
    uint64_t comp_size;
    uint64_t uncomp_size;
    uint64_t local_off;      /* zip-relative */
    const uint8_t *name;     /* raw bytes, not terminated */
    uint16_t name_len;
} CdEntry;

/* Parses the entry at *pos and advances it; FALSE on a malformed record. */
static BOOL next_cd_entry(const ZipxArchive *za, size_t *pos, CdEntry *out)
{
    const uint8_t *p = za->cd + *pos;
    size_t left = za->cd_len - *pos;
    if (left < ZIP_CD_SIZE || rd32(p) != ZIP_CD_SIG) return FALSE;

    uint16_t name_len    = rd16(p + 28);
    uint16_t extra_len   = rd16(p + 30);
    uint16_t comment_len = rd16(p + 32);
    size_t total = (size_t)ZIP_CD_SIZE + name_len + extra_len + comment_len;
    if (total > left) return FALSE;

    out->flags       = rd16(p + 8);
    out->method      = rd16(p + 10);
    out->crc         = rd32(p + 16);
    out->comp_size   = rd32(p + 20);
    out->uncomp_size = rd32(p + 24);
    out->local_off   = rd32(p + 42);
    out->name        = p + ZIP_CD_SIZE;
    out->name_len    = name_len;

    /* ZIP64 extra field: carries, in this order and only for the fields
     * whose 32-bit value is the 0xFFFFFFFF sentinel, the real uncompressed
     * size, compressed size and local header offset. */
    const uint8_t *x = p + ZIP_CD_SIZE + name_len;
    const uint8_t *xend = x + extra_len;
    while (x + 4 <= xend) {
        uint16_t id = rd16(x), sz = rd16(x + 2);
        const uint8_t *d = x + 4;
        if (d + sz > xend) return FALSE;
        if (id == 0x0001) {
            size_t used = 0;
            if (out->uncomp_size == 0xFFFFFFFFULL) {
                if (used + 8 > sz) return FALSE;
                out->uncomp_size = rd64(d + used); used += 8;
            }
            if (out->comp_size == 0xFFFFFFFFULL) {
                if (used + 8 > sz) return FALSE;
                out->comp_size = rd64(d + used); used += 8;
            }
            if (out->local_off == 0xFFFFFFFFULL) {
                if (used + 8 > sz) return FALSE;
                out->local_off = rd64(d + used); used += 8;
            }
            break;
        }
        x = d + sz;
    }
    *pos += total;
    return TRUE;
}

static BOOL name_is_dir(const CdEntry *ent)
{
    return ent->name_len > 0 &&
           (ent->name[ent->name_len - 1] == '/' || ent->name[ent->name_len - 1] == '\\');
}

ZipxArchive *zipx_open(const wchar_t *path, char *err, size_t err_cap)
{
    ZipxArchive *za = (ZipxArchive *)calloc(1, sizeof(*za));
    if (!za) { set_err(err, err_cap, "out of memory"); return NULL; }

    za->hf = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                         OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (za->hf == INVALID_HANDLE_VALUE) {
        set_err(err, err_cap, "cannot open payload file (error %lu)", GetLastError());
        free(za);
        return NULL;
    }
    LARGE_INTEGER fs;
    if (!GetFileSizeEx(za->hf, &fs)) {
        set_err(err, err_cap, "cannot get payload file size (error %lu)", GetLastError());
        zipx_close(za);
        return NULL;
    }
    za->file_size = (uint64_t)fs.QuadPart;

    if (!find_zip_info(za, err, err_cap)) { zipx_close(za); return NULL; }

    za->cd = (uint8_t *)malloc(za->cd_len ? za->cd_len : 1);
    if (!za->cd) {
        set_err(err, err_cap, "out of memory reading central directory");
        zipx_close(za);
        return NULL;
    }
    /* DWORD-sized reads: the cap above keeps cd_len under 1 GiB. */
    if (za->cd_len && !read_at(za->hf, za->zip_start + za->cd_off, za->cd, (DWORD)za->cd_len)) {
        set_err(err, err_cap, "cannot read central directory (error %lu)", GetLastError());
        zipx_close(za);
        return NULL;
    }

    /* One pass to validate the directory and total the bytes progress is
     * measured against. */
    size_t pos = 0;
    for (uint64_t i = 0; i < za->entries; i++) {
        CdEntry ent;
        if (!next_cd_entry(za, &pos, &ent)) {
            set_err(err, err_cap, "malformed central directory entry %llu",
                    (unsigned long long)i);
            zipx_close(za);
            return NULL;
        }
        if (!name_is_dir(&ent)) za->total_bytes += ent.uncomp_size;
    }
    return za;
}

uint64_t zipx_entry_count(const ZipxArchive *za) { return za->entries; }
uint64_t zipx_total_bytes(const ZipxArchive *za) { return za->total_bytes; }

void zipx_close(ZipxArchive *za)
{
    if (!za) return;
    if (za->hf != INVALID_HANDLE_VALUE && za->hf != NULL) CloseHandle(za->hf);
    free(za->cd);
    free(za);
}

/* ── Paths ────────────────────────────────────────────────────────────── */

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
        _snwprintf(out, out_cap, L"\\\\?\\%ls", in);
        out[out_cap - 1] = L'\0';
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

/* Reject an entry name that could resolve outside dest_dir once joined to
 * it. Mirrors updater.py's _safe_extract_zip(), which guards the same thing
 * on the self-update path. Defence in depth: build.py (the only source this
 * installer is ever handed) is trusted, but nothing here should trust every
 * byte of a payload wholesale. A ':' anywhere covers drive letters
 * ("C:evil", "C:\evil") and NTFS alternate data streams ("file::$DATA").
 * Called after '/' was turned into '\', so only backslash separates. */
int zipx_name_is_safe(const wchar_t *name)
{
    if (name[0] == L'\0' || name[0] == L'\\') return 0;

    const wchar_t *seg = name;
    for (const wchar_t *p = name;; p++) {
        wchar_t c = *p;
        if (c < 0x20 || c == L':' || c == L'*' || c == L'?' || c == L'"' ||
            c == L'<' || c == L'>' || c == L'|')
            return 0;
        if (c == L'\\' || c == L'\0') {
            size_t n = (size_t)(p - seg);
            if (n == 0) return 0;                                  /* "a\\b" */
            if (n == 2 && seg[0] == L'.' && seg[1] == L'.') return 0;
            if (c == L'\0') break;
            seg = p + 1;
        }
    }
    return 1;
}

/* ── Extraction ───────────────────────────────────────────────────────── */

typedef struct {
    ZipxArchive *za;
    uint8_t *inbuf, *outbuf;
    volatile const int *cancel;
    ZipxProgressFn progress;
    void *user;
    uint64_t done;           /* uncompressed bytes of finished + current data */
    char *err;
    size_t err_cap;
} Ctx;

static BOOL cancelled(const Ctx *c) { return c->cancel && *c->cancel; }

/* Extracts one file entry into hout; returns ZIPX_OK / ZIPX_CANCELLED /
 * ZIPX_ERROR. The caller deletes the partial file on anything but OK. */
static int extract_data(Ctx *c, const CdEntry *ent, const wchar_t *name,
                        HANDLE hout)
{
    ZipxArchive *za = c->za;
    char shown[128];
    ascii_name(shown, sizeof(shown), name);

    if (ent->method != 0 && ent->method != 8) {
        set_err(c->err, c->err_cap, "unsupported compression method %u: %s",
                (unsigned)ent->method, shown);
        return ZIPX_ERROR;
    }
    if (ent->method == 0 && ent->comp_size != ent->uncomp_size) {
        set_err(c->err, c->err_cap, "stored entry with mismatched sizes: %s", shown);
        return ZIPX_ERROR;
    }

    /* The local header's own name/extra lengths locate the data; its sizes
     * and CRC are ignored (a data descriptor may have left them zero). */
    uint64_t local_abs = za->zip_start + ent->local_off;
    uint8_t lh[ZIP_LOCAL_SIZE];
    if (ent->local_off >= za->cd_off ||
        !read_at(za->hf, local_abs, lh, sizeof(lh)) || rd32(lh) != ZIP_LOCAL_SIG) {
        set_err(c->err, c->err_cap, "bad local header (truncated payload?): %s", shown);
        return ZIPX_ERROR;
    }
    uint64_t data_off = local_abs + ZIP_LOCAL_SIZE + rd16(lh + 26) + rd16(lh + 28);
    if (data_off + ent->comp_size > za->zip_start + za->cd_off) {
        set_err(c->err, c->err_cap, "entry data overruns the archive: %s", shown);
        return ZIPX_ERROR;
    }
    LARGE_INTEGER li;
    li.QuadPart = (LONGLONG)data_off;
    if (!SetFilePointerEx(za->hf, li, NULL, FILE_BEGIN)) {
        set_err(c->err, c->err_cap, "seek failed (error %lu): %s", GetLastError(), shown);
        return ZIPX_ERROR;
    }

    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    BOOL inflating = ent->method == 8;
    if (inflating && inflateInit2(&zs, -15) != Z_OK) {
        set_err(c->err, c->err_cap, "inflateInit failed: %s", shown);
        return ZIPX_ERROR;
    }

    uint64_t in_left = ent->comp_size;
    uint64_t out_total = 0;
    uLong crc = crc32(0L, Z_NULL, 0);
    int rc = ZIPX_OK;
    BOOL finished = !inflating && in_left == 0;

    while (!finished) {
        if (cancelled(c)) { rc = ZIPX_CANCELLED; break; }

        const uint8_t *out_data;
        size_t out_len;

        if (!inflating) {
            DWORD n = in_left < IN_CHUNK ? (DWORD)in_left : IN_CHUNK;
            DWORD did = 0;
            if (!ReadFile(za->hf, c->inbuf, n, &did, NULL) || did != n) {
                set_err(c->err, c->err_cap, "read failed or archive truncated: %s", shown);
                rc = ZIPX_ERROR; break;
            }
            in_left -= n;
            out_data = c->inbuf;
            out_len = n;
            finished = in_left == 0;
        } else {
            if (zs.avail_in == 0) {
                if (in_left == 0) {
                    set_err(c->err, c->err_cap, "truncated deflate stream: %s", shown);
                    rc = ZIPX_ERROR; break;
                }
                DWORD n = in_left < IN_CHUNK ? (DWORD)in_left : IN_CHUNK;
                DWORD did = 0;
                if (!ReadFile(za->hf, c->inbuf, n, &did, NULL) || did != n) {
                    set_err(c->err, c->err_cap, "read failed or archive truncated: %s", shown);
                    rc = ZIPX_ERROR; break;
                }
                zs.next_in = c->inbuf;
                zs.avail_in = n;
                in_left -= n;
            }
            zs.next_out = c->outbuf;
            zs.avail_out = OUT_CHUNK;
            int zr = inflate(&zs, Z_NO_FLUSH);
            if (zr != Z_OK && zr != Z_STREAM_END) {
                set_err(c->err, c->err_cap, "inflate error %d (%s): %s", zr,
                        zs.msg ? zs.msg : "corrupt data", shown);
                rc = ZIPX_ERROR; break;
            }
            out_data = c->outbuf;
            out_len = OUT_CHUNK - zs.avail_out;
            finished = zr == Z_STREAM_END;
        }

        if (out_len) {
            out_total += out_len;
            if (out_total > ent->uncomp_size) {
                set_err(c->err, c->err_cap, "entry larger than its declared size: %s", shown);
                rc = ZIPX_ERROR; break;
            }
            DWORD wrote = 0;
            if (!WriteFile(hout, out_data, (DWORD)out_len, &wrote, NULL) || wrote != out_len) {
                set_err(c->err, c->err_cap, "write failed (error %lu, disk full?): %s",
                        GetLastError(), shown);
                rc = ZIPX_ERROR; break;
            }
            crc = crc32(crc, out_data, (uInt)out_len);
            c->done += out_len;
            if (c->progress) c->progress(c->user, name, 0, c->done, za->total_bytes);
        }
    }
    if (inflating) inflateEnd(&zs);
    if (rc != ZIPX_OK) return rc;

    if (out_total != ent->uncomp_size) {
        set_err(c->err, c->err_cap, "size mismatch (%llu of %llu bytes): %s",
                (unsigned long long)out_total, (unsigned long long)ent->uncomp_size, shown);
        return ZIPX_ERROR;
    }
    if ((uint32_t)crc != ent->crc) {
        set_err(c->err, c->err_cap, "CRC-32 mismatch: %s", shown);
        return ZIPX_ERROR;
    }
    return ZIPX_OK;
}

int zipx_extract_all(ZipxArchive *za, const wchar_t *dest_dir,
                     ZipxProgressFn progress, ZipxFileFn on_file, void *user,
                     volatile const int *cancel, char *err, size_t err_cap)
{
    Ctx c;
    memset(&c, 0, sizeof(c));
    c.za = za; c.cancel = cancel; c.progress = progress; c.user = user;
    c.err = err; c.err_cap = err_cap;
    c.inbuf  = (uint8_t *)malloc(IN_CHUNK);
    c.outbuf = (uint8_t *)malloc(OUT_CHUNK);
    wchar_t *name_w = (wchar_t *)malloc(WZ_MAX_PATH * sizeof(wchar_t));
    wchar_t *dest_path = (wchar_t *)malloc(WZ_MAX_PATH * sizeof(wchar_t));
    wchar_t *dest_ext = (wchar_t *)malloc(WZ_MAX_PATH * sizeof(wchar_t));
    int result = ZIPX_ERROR;
    if (!c.inbuf || !c.outbuf || !name_w || !dest_path || !dest_ext) {
        set_err(err, err_cap, "out of memory");
        goto done;
    }

    size_t pos = 0;
    for (uint64_t i = 0; i < za->entries; i++) {
        if (cancelled(&c)) { result = ZIPX_CANCELLED; goto done; }

        CdEntry ent;
        if (!next_cd_entry(za, &pos, &ent)) {
            set_err(err, err_cap, "malformed central directory entry %llu",
                    (unsigned long long)i);
            goto done;
        }
        if (ent.flags & ZIP_FLAG_ENCRYPTED) {
            set_err(err, err_cap, "encrypted entries are not supported");
            goto done;
        }

        BOOL is_dir = name_is_dir(&ent);
        int n = ent.name_len ? MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                    (const char *)ent.name, ent.name_len, name_w, WZ_MAX_PATH - 2) : 0;
        if (n <= 0) {
            set_err(err, err_cap, "entry name is empty, too long or not UTF-8");
            goto done;
        }
        name_w[n] = L'\0';
        for (wchar_t *pw = name_w; *pw; pw++)
            if (*pw == L'/') *pw = L'\\';
        if (is_dir) name_w[n - 1] = L'\0';   /* trailing separator */

        if (!zipx_name_is_safe(name_w)) {
            char shown[128];
            ascii_name(shown, sizeof(shown), name_w);
            set_err(err, err_cap, "unsafe entry name rejected: %s", shown);
            goto done;
        }
        if (wcslen(dest_dir) + 1 + (size_t)n + 2 >= WZ_MAX_PATH) {
            set_err(err, err_cap, "destination path too long");
            goto done;
        }
        _snwprintf(dest_path, WZ_MAX_PATH, L"%ls\\%ls", dest_dir, name_w);
        dest_path[WZ_MAX_PATH - 1] = L'\0';

        if (is_dir) {
            wcscat(dest_path, L"\\");
            ensure_dirs(dest_path);
            continue;
        }

        ensure_dirs(dest_path);
        to_extended_path(dest_ext, WZ_MAX_PATH, dest_path);
        if (progress) progress(user, name_w, 1, c.done, za->total_bytes);

        HANDLE hout = CreateFileW(dest_ext, GENERIC_WRITE, 0, NULL,
                                  CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (hout == INVALID_HANDLE_VALUE) {
            char shown[128];
            ascii_name(shown, sizeof(shown), name_w);
            set_err(err, err_cap, "cannot create file (error %lu): %s",
                    GetLastError(), shown);
            goto done;
        }

        uint64_t done_before = c.done;
        int rc = extract_data(&c, &ent, name_w, hout);
        CloseHandle(hout);
        if (rc != ZIPX_OK) {
            /* A half-written file would pass for the real one next launch. */
            DeleteFileW(dest_ext);
            result = rc;
            goto done;
        }
        c.done = done_before + ent.uncomp_size;
        if (on_file) on_file(user, dest_path);
    }
    result = ZIPX_OK;
    if (progress) progress(user, L"", 0, c.done, za->total_bytes);

done:
    free(c.inbuf); free(c.outbuf);
    free(name_w); free(dest_path); free(dest_ext);
    return result;
}
