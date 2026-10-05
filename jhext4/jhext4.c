/*
 * jhext4 — an ext4 drive as a Windows folder, using Linux's own ext4 code.
 *
 *   jhext4.exe --disk <image or \\.\PhysicalDriveN> --mount M [--read-write]
 *
 * The Linux kernel's ext4 runs inside this program through LKL, and Dokany
 * presents its files to Windows. Nothing here reimplements ext4: the journal,
 * its replay after a drive is pulled, and every on-disk format decision are
 * the kernel's own, which is the whole reason for this design.
 *
 * Read-only unless --read-write is given. Step 2 of the plan is this program
 * mounting read-only and surviving a pull; writing arrives in step 3, once the
 * 0b yank harness is measuring it.
 *
 * Three settings matter and are not defaults:
 *
 *   the disk says it has a write cache (VIRTIO_BLK_F_FLUSH), so the kernel
 *   sends flushes at all. Stock LKL says it has none, and every fsync was
 *   silently dropped -- see docs/0c-step1-flush-proof.md;
 *
 *   the device is opened write-through and unbuffered, so Windows does not
 *   hold data of its own after a flush;
 *
 *   the mount is private to the session that started it, and single-threaded,
 *   because LKL's ext4 is one kernel and this program is its only caller.
 *
 * GPL-2.0, with LKL.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <windows.h>
#include <dokan.h>
#include <lkl.h>
#include <lkl_host.h>
#include "virtio.h"

#define VIRTIO_BLK_F_FLUSH 9
#define JH_SECTOR 512

static struct lkl_disk disk;
static char mount_in_lkl[64];
static int read_write;
static volatile LONG flush_count;
static struct lkl_dev_blk_ops counting_ops;

/* ---------------------------------------------------------------- plumbing */

/* Count the flushes that reach the host, so `--selftest` can state a number
 * rather than an intention, and pass every request to LKL's own handler. */
static int counting_request(struct lkl_disk d, struct lkl_blk_req *req)
{
	if (req->type == LKL_DEV_BLK_TYPE_FLUSH || req->type == LKL_DEV_BLK_TYPE_FLUSH_OUT)
		InterlockedIncrement(&flush_count);
	return lkl_dev_blk_ops.request(d, req);
}

/* Windows hands us "\dir\file" in UTF-16; ext4 wants "/mnt/dir/file" in UTF-8.
 * Returns 0, or -1 if the name cannot be represented or would not fit. */
static int to_lkl_path(LPCWSTR name, char *out, size_t size)
{
	int n = snprintf(out, size, "%s", mount_in_lkl);
	if (n < 0 || (size_t)n >= size)
		return -1;
	if (!name || !name[0] || (name[0] == L'\\' && !name[1]))
		return 0;
	int wrote = WideCharToMultiByte(CP_UTF8, 0, name, -1, out + n, (int)(size - n), NULL, NULL);
	if (wrote <= 0)
		return -1;
	for (char *p = out + n; *p; p++)
		if (*p == '\\')
			*p = '/';
	return 0;
}

static NTSTATUS from_lkl(long err)
{
	switch (-err) {
	case 0:            return STATUS_SUCCESS;
	case LKL_ENOENT:   return STATUS_OBJECT_NAME_NOT_FOUND;
	case LKL_ENOTDIR:  return STATUS_OBJECT_PATH_NOT_FOUND;
	case LKL_EISDIR:   return STATUS_FILE_IS_A_DIRECTORY;
	case LKL_EACCES:
	case LKL_EPERM:    return STATUS_ACCESS_DENIED;
	case LKL_EEXIST:   return STATUS_OBJECT_NAME_COLLISION;
	case LKL_ENOTEMPTY:return STATUS_DIRECTORY_NOT_EMPTY;
	case LKL_ENOSPC:   return STATUS_DISK_FULL;
	case LKL_EROFS:    return STATUS_MEDIA_WRITE_PROTECTED;
	case LKL_EIO:      return STATUS_IO_DEVICE_ERROR;
	case LKL_ENAMETOOLONG: return STATUS_NAME_TOO_LONG;
	default:           return STATUS_UNSUCCESSFUL;
	}
}

/* Unix seconds to Windows' 100ns-since-1601. */
static void to_filetime(long long seconds, long nanoseconds, FILETIME *out)
{
	long long ticks = (seconds + 11644473600LL) * 10000000LL + nanoseconds / 100;
	out->dwLowDateTime = (DWORD)ticks;
	out->dwHighDateTime = (DWORD)(ticks >> 32);
}

static DWORD attributes_of(const struct lkl_stat *st)
{
	DWORD attributes = 0;
	if (LKL_S_ISDIR(st->st_mode))
		attributes |= FILE_ATTRIBUTE_DIRECTORY;
	/* An owner's files are their own business; nothing here is hidden from
	 * them because of a leading dot. Windows has no notion of a Unix mode,
	 * and mapping one onto READONLY would make a file the agent must write
	 * look unwritable to Explorer. */
	if (read_write == 0)
		attributes |= FILE_ATTRIBUTE_READONLY;
	return attributes ? attributes : FILE_ATTRIBUTE_NORMAL;
}

/* ------------------------------------------------------------- Dokany calls */

static NTSTATUS DOKAN_CALLBACK jh_create(LPCWSTR name, PDOKAN_IO_SECURITY_CONTEXT context,
					 ACCESS_MASK access, ULONG attributes, ULONG share,
					 ULONG disposition, ULONG options, PDOKAN_FILE_INFO info)
{
	char path[LKL_PATH_MAX];
	struct lkl_stat st;
	long ret;
	(void)context; (void)attributes; (void)share;

	if (to_lkl_path(name, path, sizeof(path)) < 0)
		return STATUS_OBJECT_NAME_INVALID;

	if (!read_write && (disposition == FILE_CREATE || disposition == FILE_OVERWRITE ||
			    disposition == FILE_OVERWRITE_IF || disposition == FILE_SUPERSEDE ||
			    (access & (FILE_WRITE_DATA | FILE_APPEND_DATA | DELETE))))
		return STATUS_MEDIA_WRITE_PROTECTED;

	ret = lkl_sys_stat(path, &st);
	if (ret < 0) {
		if (disposition == FILE_OPEN || disposition == FILE_OVERWRITE)
			return from_lkl(ret);
		return from_lkl(ret);   /* creating arrives with step 3 */
	}

	if (LKL_S_ISDIR(st.st_mode)) {
		if (options & FILE_NON_DIRECTORY_FILE)
			return STATUS_FILE_IS_A_DIRECTORY;
		info->IsDirectory = TRUE;
		return STATUS_SUCCESS;
	}
	if (options & FILE_DIRECTORY_FILE)
		return STATUS_NOT_A_DIRECTORY;

	/* The open file descriptor is the context; ReadFile uses it. */
	ret = lkl_sys_open(path, LKL_O_RDONLY, 0);
	if (ret < 0)
		return from_lkl(ret);
	info->Context = (ULONG64)ret;
	return STATUS_SUCCESS;
}

static void DOKAN_CALLBACK jh_close(LPCWSTR name, PDOKAN_FILE_INFO info)
{
	(void)name;
	if (info->Context) {
		lkl_sys_close((int)info->Context);
		info->Context = 0;
	}
}

static NTSTATUS DOKAN_CALLBACK jh_read(LPCWSTR name, LPVOID buffer, DWORD length,
				       LPDWORD read, LONGLONG offset, PDOKAN_FILE_INFO info)
{
	char path[LKL_PATH_MAX];
	int fd = (int)info->Context, opened = 0;
	long ret;

	*read = 0;
	if (!fd) {
		/* Windows can read through a handle we never opened, during
		 * paging in particular. */
		if (to_lkl_path(name, path, sizeof(path)) < 0)
			return STATUS_OBJECT_NAME_INVALID;
		ret = lkl_sys_open(path, LKL_O_RDONLY, 0);
		if (ret < 0)
			return from_lkl(ret);
		fd = (int)ret;
		opened = 1;
	}
	ret = lkl_sys_pread64(fd, buffer, length, offset);
	if (ret >= 0)
		*read = (DWORD)ret;
	if (opened)
		lkl_sys_close(fd);
	return ret < 0 ? from_lkl(ret) : STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_get_info(LPCWSTR name, LPBY_HANDLE_FILE_INFORMATION out,
					   PDOKAN_FILE_INFO info)
{
	char path[LKL_PATH_MAX];
	struct lkl_stat st;
	long ret;
	(void)info;

	if (to_lkl_path(name, path, sizeof(path)) < 0)
		return STATUS_OBJECT_NAME_INVALID;
	ret = lkl_sys_stat(path, &st);
	if (ret < 0)
		return from_lkl(ret);

	memset(out, 0, sizeof(*out));
	out->dwFileAttributes = attributes_of(&st);
	out->nFileSizeLow = (DWORD)st.st_size;
	out->nFileSizeHigh = (DWORD)(st.st_size >> 32);
	out->nNumberOfLinks = (DWORD)st.st_nlink;
	out->nFileIndexLow = (DWORD)st.st_ino;
	out->nFileIndexHigh = (DWORD)(st.st_ino >> 32);
	to_filetime(st.lkl_st_atime, st.st_atime_nsec, &out->ftLastAccessTime);
	to_filetime(st.lkl_st_mtime, st.st_mtime_nsec, &out->ftLastWriteTime);
	to_filetime(st.lkl_st_ctime, st.st_ctime_nsec, &out->ftCreationTime);
	return STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_find(LPCWSTR name, PFillFindData fill, PDOKAN_FILE_INFO info)
{
	char path[LKL_PATH_MAX], child[LKL_PATH_MAX];
	struct lkl_dir *dir;
	struct lkl_linux_dirent64 *entry;
	WIN32_FIND_DATAW found;
	struct lkl_stat st;
	int error = 0;

	if (to_lkl_path(name, path, sizeof(path)) < 0)
		return STATUS_OBJECT_NAME_INVALID;
	dir = lkl_opendir(path, &error);
	if (!dir)
		return from_lkl(-error);

	while ((entry = lkl_readdir(dir))) {
		if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, ".."))
			continue;
		memset(&found, 0, sizeof(found));
		if (MultiByteToWideChar(CP_UTF8, 0, entry->d_name, -1, found.cFileName,
					sizeof(found.cFileName) / sizeof(WCHAR)) <= 0)
			continue;   /* a name Windows cannot spell; listed by nothing, not a failure */
		if (snprintf(child, sizeof(child), "%s/%s", path, entry->d_name) >= (int)sizeof(child))
			continue;
		if (lkl_sys_stat(child, &st) < 0)
			continue;
		found.dwFileAttributes = attributes_of(&st);
		found.nFileSizeLow = (DWORD)st.st_size;
		found.nFileSizeHigh = (DWORD)(st.st_size >> 32);
		to_filetime(st.lkl_st_atime, st.st_atime_nsec, &found.ftLastAccessTime);
		to_filetime(st.lkl_st_mtime, st.st_mtime_nsec, &found.ftLastWriteTime);
		to_filetime(st.lkl_st_ctime, st.st_ctime_nsec, &found.ftCreationTime);
		fill(&found, info);
	}
	lkl_closedir(dir);
	return STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_free_space(PULONGLONG available, PULONGLONG total,
					     PULONGLONG free_bytes, PDOKAN_FILE_INFO info)
{
	struct lkl_statfs fs;
	(void)info;
	if (lkl_sys_statfs(mount_in_lkl, &fs) < 0)
		return STATUS_IO_DEVICE_ERROR;
	*total = (ULONGLONG)fs.f_blocks * fs.f_bsize;
	*free_bytes = (ULONGLONG)fs.f_bfree * fs.f_bsize;
	*available = (ULONGLONG)fs.f_bavail * fs.f_bsize;
	return STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_volume_info(LPWSTR name, DWORD name_size, LPDWORD serial,
					      LPDWORD component, LPDWORD flags,
					      LPWSTR fs_name, DWORD fs_name_size,
					      PDOKAN_FILE_INFO info)
{
	(void)info;
	wcsncpy(name, L"ext4", name_size / sizeof(WCHAR));
	/* The serial is derived from the ext4 UUID by the caller, so that a
	 * process can be told which drive it is running from -- the Windows
	 * answer to the st_dev rule Linux uses (see 0.4.85). */
	*serial = (DWORD)(info && info->DokanOptions ? info->DokanOptions->GlobalContext : 0);
	*component = 255;
	*flags = FILE_CASE_PRESERVED_NAMES | FILE_CASE_SENSITIVE_SEARCH | FILE_UNICODE_ON_DISK;
	if (!read_write)
		*flags |= FILE_READ_ONLY_VOLUME;
	wcsncpy(fs_name, L"ext4", fs_name_size / sizeof(WCHAR));
	return STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_mounted(LPCWSTR actual, PDOKAN_FILE_INFO info)
{
	(void)info;
	fprintf(stderr, "mounted at %ls\n", actual);
	fflush(stderr);
	return STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_unmounted(PDOKAN_FILE_INFO info)
{
	(void)info;
	fprintf(stderr, "unmounted (flushes that reached the disk: %ld)\n", flush_count);
	fflush(stderr);
	return STATUS_SUCCESS;
}

static DOKAN_OPERATIONS operations = {
	.ZwCreateFile = jh_create,
	.CloseFile = jh_close,
	.ReadFile = jh_read,
	.GetFileInformation = jh_get_info,
	.FindFiles = jh_find,
	.GetDiskFreeSpace = jh_free_space,
	.GetVolumeInformation = jh_volume_info,
	.Mounted = jh_mounted,
	.Unmounted = jh_unmounted,
};

/* ------------------------------------------------------------------- start */

static void usage(void)
{
	fprintf(stderr,
		"usage: jhext4 --disk <image|\\\\.\\PhysicalDriveN> --mount <letter or path>\n"
		"              [--part N] [--read-write] [--serial N] [--debug]\n");
}

int main(int argc, char **argv)
{
	const char *disk_path = NULL, *mount = NULL;
	unsigned part = 0, serial = 0, debug = 0;
	DOKAN_OPTIONS options;
	wchar_t mount_w[64];
	long ret;
	int id, status;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--disk") && i + 1 < argc) disk_path = argv[++i];
		else if (!strcmp(argv[i], "--mount") && i + 1 < argc) mount = argv[++i];
		else if (!strcmp(argv[i], "--part") && i + 1 < argc) part = (unsigned)atoi(argv[++i]);
		else if (!strcmp(argv[i], "--serial") && i + 1 < argc) serial = (unsigned)strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--read-write")) read_write = 1;
		else if (!strcmp(argv[i], "--debug")) debug = 1;
		else { usage(); return 2; }
	}
	if (!disk_path || !mount) { usage(); return 2; }

	setvbuf(stderr, NULL, _IONBF, 0);

	/* Write-through and unbuffered: after a flush, Windows must not be
	 * holding data of its own. This is the second half of what makes a
	 * pulled drive safe; the first is the flush reaching here at all. */
	disk.handle = CreateFileA(disk_path, GENERIC_READ | (read_write ? GENERIC_WRITE : 0),
				  FILE_SHARE_READ, NULL, OPEN_EXISTING,
				  FILE_ATTRIBUTE_NORMAL | FILE_FLAG_WRITE_THROUGH | FILE_FLAG_NO_BUFFERING,
				  NULL);
	if (disk.handle == INVALID_HANDLE_VALUE) {
		fprintf(stderr, "cannot open %s: Windows error %lu\n", disk_path, GetLastError());
		return 1;
	}
	counting_ops = lkl_dev_blk_ops;
	counting_ops.request = counting_request;
	disk.ops = &counting_ops;

	if (lkl_init(&lkl_host_ops) < 0) {
		fprintf(stderr, "the Linux filesystem code did not start\n");
		return 1;
	}
	id = lkl_disk_add(&disk);
	if (id < 0) {
		fprintf(stderr, "the disk could not be attached: %s\n", lkl_strerror(id));
		return 1;
	}
	/* Say the disk has a write cache, so the kernel sends flushes at all.
	 * Without this every fsync is dropped in silence; measured in
	 * docs/0c-step1-flush-proof.md. */
	((struct virtio_dev *)disk.dev)->device_features |= 1ULL << VIRTIO_BLK_F_FLUSH;

	ret = lkl_start_kernel(debug ? "mem=128M loglevel=8" : "mem=128M loglevel=3");
	if (ret < 0) {
		fprintf(stderr, "the Linux filesystem code did not start: %s\n", lkl_strerror(ret));
		return 1;
	}
	/* `commit=1`: at most about a second of unsynced data is at risk when
	 * the drive is pulled, rather than ext4's default five. */
	ret = lkl_mount_dev(id, part, "ext4",
			    read_write ? 0 : LKL_MS_RDONLY,
			    read_write ? "commit=1" : NULL,
			    mount_in_lkl, sizeof(mount_in_lkl));
	if (ret < 0) {
		fprintf(stderr, "this disk could not be mounted as ext4: %s\n", lkl_strerror(ret));
		lkl_sys_halt();
		return 1;
	}
	fprintf(stderr, "ext4 mounted inside: %s (%s)\n", mount_in_lkl,
		read_write ? "read/write" : "read-only");

	MultiByteToWideChar(CP_UTF8, 0, mount, -1, mount_w, sizeof(mount_w) / sizeof(wchar_t));
	memset(&options, 0, sizeof(options));
	options.Version = DOKAN_VERSION;
	options.MountPoint = mount_w;
	options.GlobalContext = serial;
	/* Single-threaded: LKL is one kernel and this program is its only
	 * caller. Removable, so Windows treats a pull as it would any stick.
	 * Current session, so the mount belongs to the owner who started it and
	 * is not offered to other accounts. Case-sensitive, as ext4 is. */
	options.SingleThread = TRUE;
	options.Options = DOKAN_OPTION_REMOVABLE | DOKAN_OPTION_CURRENT_SESSION |
			  DOKAN_OPTION_CASE_SENSITIVE |
			  (read_write ? 0 : DOKAN_OPTION_WRITE_PROTECT) |
			  (debug ? (DOKAN_OPTION_DEBUG | DOKAN_OPTION_STDERR) : 0);
	options.SectorSize = JH_SECTOR;
	options.AllocationUnitSize = 4096;
	options.Timeout = 30000;

	/* Dokany 2.x needs this before a mount and the matching shutdown after.
	 * Without it DokanMain returns success immediately and nothing is
	 * mounted -- a silent no-op, not an error. */
	DokanInit();
	status = DokanMain(&options, &operations);
	if (status != DOKAN_SUCCESS)
		fprintf(stderr, "Dokany refused the mount: %d%s\n", status,
			status == DOKAN_DRIVER_INSTALL_ERROR ? " (the Dokany driver is not installed)" :
			status == DOKAN_DRIVE_LETTER_ERROR ? " (that drive letter is in use)" :
			status == DOKAN_MOUNT_ERROR ? " (the mount point could not be assigned)" :
			status == DOKAN_VERSION_ERROR ? " (the installed driver is a different version)" : "");
	DokanShutdown();

	lkl_umount_dev(id, part, 0, 5000);
	lkl_sys_halt();
	lkl_cleanup();
	CloseHandle(disk.handle);
	return status == DOKAN_SUCCESS ? 0 : 1;
}
