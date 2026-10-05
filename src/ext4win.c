/*
 * ext4win — an ext4 drive as a Windows folder, using Linux's own ext4 code.
 *
 *   ext4win.exe --disk <image or \\.\PhysicalDriveN> --mount M [--read-write]
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
#include <winioctl.h>
#include <dokan.h>
#include <lkl.h>
#include <lkl_host.h>
#include "virtio.h"

#define VIRTIO_BLK_F_FLUSH 9
#define JH_SECTOR 512
/* UTIME_OMIT from the kernel's uapi: leave this timestamp as it is. */
#define JH_UTIME_OMIT ((1l << 30) - 2l)

static struct lkl_disk disk;
static char mount_in_lkl[64];
static int read_write;
static volatile LONG flush_count;
static volatile LONG drive_gone;
static volatile LONG stop_requested;
static struct lkl_dev_blk_ops counting_ops;
static HANDLE wake_event, unmounted_event;
static const char *status_path;
static wchar_t mount_w[64];

/*
 * Exit codes, so whatever started this program can tell what happened without
 * reading prose. The supervisor needs "the drive was taken away" to be a
 * different answer from "this program failed", because the first means stop the
 * agent and wait, and the second means something is wrong with the drive's
 * software.
 */
#define JH_EXIT_OK	 0
#define JH_EXIT_FAILED	 1
#define JH_EXIT_REMOVED	10

/* Once the drive is gone, answer every request with the error Windows has for
 * exactly this, rather than letting it down into a filesystem whose disk is no
 * longer there. Programs then get "the device was removed" instead of a
 * puzzling I/O error, and nothing waits on a disk that cannot answer. */
#define JH_IF_GONE() do { if (drive_gone) return STATUS_DEVICE_REMOVED; } while (0)

/* The one line of state anything outside this program can read. It goes on the
 * host, never on the drive: at the moment it matters most, the drive is gone. */
static void write_status(const char *state)
{
	FILE *out;

	if (!status_path)
		return;
	out = fopen(status_path, "w");
	if (!out)
		return;
	fprintf(out, "{\"state\":\"%s\",\"mount\":\"%ls\",\"flushes\":%ld,\"pid\":%lu}\n",
		state, mount_w, (long)flush_count, (unsigned long)GetCurrentProcessId());
	fclose(out);
}

/* ---------------------------------------------------------------- plumbing */

/*
 * Has the drive been taken away, as opposed to one request failing?
 *
 * Windows has a specific set of errors for "the device you were talking to is
 * not there any more". A bad sector or a busy disk is not one of them, and must
 * not be reported as a pull: a drive that is still plugged in should keep
 * working, and ext4 will remount itself read-only if its own writes fail.
 */
static int device_is_gone(void)
{
	GET_LENGTH_INFORMATION length;
	DWORD got, error;

	if (DeviceIoControl(disk.handle, IOCTL_DISK_GET_LENGTH_INFO, NULL, 0,
			    &length, sizeof(length), &got, NULL))
		return 0;
	error = GetLastError();
	switch (error) {
	case ERROR_DEVICE_NOT_CONNECTED:
	case ERROR_NO_SUCH_DEVICE:
	case ERROR_DEV_NOT_EXIST:
	case ERROR_FILE_NOT_FOUND:
	case ERROR_INVALID_HANDLE:
	case ERROR_NOT_READY:
	case ERROR_UNRECOGNIZED_VOLUME:
		return 1;
	default:
		/* An image file answers this call with ERROR_INVALID_FUNCTION,
		 * so a file-backed disk is never mistaken for a vanished one. */
		return 0;
	}
}

/* Say so once, on the host, and ask Windows to drop the drive. Called from the
 * thread that noticed; the mount point is removed by the watcher, because
 * doing it inside a callback can deadlock against Dokany's own dispatch. */
static void note_the_drive_is_gone(void)
{
	if (InterlockedExchange(&drive_gone, 1))
		return;
	fprintf(stderr, "the drive is gone: every further request will fail cleanly\n");
	write_status("removed");
	SetEvent(wake_event);
}

/*
 * Give up the drive letter, either because the drive is gone or because
 * something asked this program to stop.
 *
 * This runs on its own thread because DokanRemoveMountPoint must not be called
 * from inside a callback: it waits on Dokany's own dispatch, which is what the
 * callback is holding. Removing the mount point is what makes DokanMain
 * return, so this is also how the program gets to exit at all.
 */
static DWORD WINAPI watch_for_removal(LPVOID unused)
{
	(void)unused;
	WaitForSingleObject(wake_event, INFINITE);
	if (drive_gone || stop_requested)
		DokanRemoveMountPoint(mount_w);
	return 0;
}

/*
 * Stop cleanly when asked.
 *
 * There has to be a way to put the drive down without killing this program,
 * and `dokanctl /u` is not it: measured, it does not unmount a drive mounted
 * for one session, so before this the only way to stop was the kill -- which
 * is the pull path, not the clean one. A console control event is the same
 * signal Windows supervision already plans to use for the engines
 * (CTRL_BREAK on a process group), so it is the one word this program answers
 * to as well. The flush of everything outstanding happens in the unmount, on
 * the main thread; this only asks for it.
 */
static BOOL WINAPI on_console_signal(DWORD event)
{
	switch (event) {
	case CTRL_C_EVENT:
	case CTRL_BREAK_EVENT:
	case CTRL_CLOSE_EVENT:
	case CTRL_LOGOFF_EVENT:
	case CTRL_SHUTDOWN_EVENT:
		if (!InterlockedExchange(&stop_requested, 1)) {
			fprintf(stderr, "stopping: unmounting the drive\n");
			SetEvent(wake_event);
		}
		/* Windows gives a close or shutdown handler a few seconds before it
		 * kills the process; wait for the unmount so the journal is settled. */
		if (event != CTRL_C_EVENT && event != CTRL_BREAK_EVENT)
			WaitForSingleObject(unmounted_event, 10000);
		return TRUE;
	default:
		return FALSE;
	}
}

/* Count the flushes that reach the host, so `--selftest` can state a number
 * rather than an intention, notice a drive that has been taken away, and pass
 * every request to LKL's own handler. */
static int counting_request(struct lkl_disk d, struct lkl_blk_req *req)
{
	int status;

	if (drive_gone)
		return LKL_DEV_BLK_STATUS_IOERR;
	if (req->type == LKL_DEV_BLK_TYPE_FLUSH || req->type == LKL_DEV_BLK_TYPE_FLUSH_OUT)
		InterlockedIncrement(&flush_count);
	status = lkl_dev_blk_ops.request(d, req);
	/* Every read, write and flush passes through here, so this is the one
	 * place that learns the drive has been pulled -- and it learns it from
	 * the disk itself rather than from Windows telling anyone. */
	if (status != LKL_DEV_BLK_STATUS_OK && device_is_gone())
		note_the_drive_is_gone();
	return status;
}

/* LKL asks the file's size, which a physical disk does not have: Windows
 * answers GetFileSizeEx on \\.\PhysicalDriveN with an error, and the drive
 * would attach as zero bytes. A disk is asked its length instead. */
static int disk_capacity(struct lkl_disk d, unsigned long long *res)
{
	GET_LENGTH_INFORMATION length;
	LARGE_INTEGER size;
	DWORD got;

	if (GetFileSizeEx(d.handle, &size) && size.QuadPart > 0) {
		*res = size.QuadPart;
		return 0;
	}
	if (DeviceIoControl(d.handle, IOCTL_DISK_GET_LENGTH_INFO, NULL, 0,
			    &length, sizeof(length), &got, NULL)) {
		*res = length.Length.QuadPart;
		return 0;
	}
	return -1;
}

/*
 * Make a change to a folder's contents durable before saying it happened:
 * a rename, a delete, a new folder.
 *
 * On Linux a careful program fsyncs the folder after such a change, and only
 * then is it safe from a pull. Windows has no way for a program to fsync a
 * folder, so that step can never arrive from a Windows program; doing it here
 * gives every Windows program the guarantee careful Linux programs give
 * themselves. It costs one journal commit per change, which a single owner's
 * agent never notices. A new file needs nothing here: fsyncing the file
 * commits the journal transaction that created its name.
 */
static long sync_parent(char *path)
{
	char *slash = strrchr(path, '/');
	long dir, ret;

	if (!slash || slash == path)
		return 0;
	*slash = '\0';
	dir = lkl_sys_open(path, LKL_O_RDONLY | LKL_O_DIRECTORY, 0);
	*slash = '/';
	if (dir < 0)
		return dir;
	ret = lkl_sys_fsync((int)dir);
	lkl_sys_close((int)dir);
	return ret;
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

/* An open file's descriptor is kept as fd + 1, so that 0 means "none" --
 * a descriptor can itself be 0. */
#define FD_OF(info)       ((int)(info)->Context - 1)
#define HAS_FD(info)      ((info)->Context != 0)
#define SET_FD(info, fd)  ((info)->Context = (ULONG64)((fd) + 1))

static NTSTATUS DOKAN_CALLBACK jh_create(LPCWSTR name, PDOKAN_IO_SECURITY_CONTEXT context,
					 ACCESS_MASK access, ULONG attributes, ULONG share,
					 ULONG disposition, ULONG options, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	char path[LKL_PATH_MAX];
	struct lkl_stat st;
	ACCESS_MASK generic;
	DWORD creation, flags_attr;
	long ret;
	int exists, flags, wants_write;
	(void)context; (void)share;

	if (to_lkl_path(name, path, sizeof(path)) < 0)
		return STATUS_OBJECT_NAME_INVALID;

	/* Dokany hands over kernel-mode values; turn them into the familiar
	 * CreateFile ones, which is the documented way to read them. */
	DokanMapKernelToUserCreateFileFlags(access, attributes, options, disposition,
					    &generic, &flags_attr, &creation);
	wants_write = (generic & (GENERIC_WRITE | FILE_WRITE_DATA | FILE_APPEND_DATA | DELETE)) != 0 ||
		      creation == CREATE_NEW || creation == CREATE_ALWAYS || creation == TRUNCATE_EXISTING;
	if (!read_write && (wants_write || (creation == OPEN_ALWAYS)))
		if (wants_write || lkl_sys_stat(path, &st) < 0)
			return STATUS_MEDIA_WRITE_PROTECTED;

	ret = lkl_sys_stat(path, &st);
	exists = ret == 0;

	/* Directories. */
	if ((exists && LKL_S_ISDIR(st.st_mode)) || (options & FILE_DIRECTORY_FILE)) {
		if (exists && !LKL_S_ISDIR(st.st_mode))
			return STATUS_NOT_A_DIRECTORY;
		if (exists && (options & FILE_NON_DIRECTORY_FILE))
			return STATUS_FILE_IS_A_DIRECTORY;
		info->IsDirectory = TRUE;
		if (creation == CREATE_NEW || creation == OPEN_ALWAYS) {
			if (exists)
				return creation == CREATE_NEW ? STATUS_OBJECT_NAME_COLLISION : STATUS_SUCCESS;
			ret = lkl_sys_mkdir(path, 0700);
			if (ret == 0)
				ret = sync_parent(path);
			return from_lkl(ret);
		}
		return exists ? STATUS_SUCCESS : STATUS_OBJECT_NAME_NOT_FOUND;
	}

	/* Files. */
	switch (creation) {
	case CREATE_NEW:
		if (exists) return STATUS_OBJECT_NAME_COLLISION;
		flags = LKL_O_CREAT | LKL_O_EXCL;
		break;
	case CREATE_ALWAYS:
		flags = LKL_O_CREAT | LKL_O_TRUNC;
		break;
	case OPEN_ALWAYS:
		flags = LKL_O_CREAT;
		break;
	case TRUNCATE_EXISTING:
		if (!exists) return STATUS_OBJECT_NAME_NOT_FOUND;
		flags = LKL_O_TRUNC;
		break;
	default:	/* OPEN_EXISTING */
		if (!exists) return STATUS_OBJECT_NAME_NOT_FOUND;
		flags = 0;
	}
	/* New files are private (0600), as the drive's own tools make them:
	 * credentials live on this drive. */
	ret = lkl_sys_open(path, flags | (read_write ? LKL_O_RDWR : LKL_O_RDONLY), 0600);
	if (ret < 0 && read_write && exists)	/* a file we may read but not write */
		ret = lkl_sys_open(path, LKL_O_RDONLY, 0);
	if (ret < 0)
		return from_lkl(ret);
	SET_FD(info, ret);

	/* Windows expects "already existed" to be reported for these two. */
	if (exists && (creation == OPEN_ALWAYS || creation == CREATE_ALWAYS))
		return STATUS_OBJECT_NAME_COLLISION;
	return STATUS_SUCCESS;
}

/* Cleanup runs when the last handle closes; deletion happens here, because
 * Windows only decides it on the way out. */
static void DOKAN_CALLBACK jh_cleanup(LPCWSTR name, PDOKAN_FILE_INFO info)
{
	char path[LKL_PATH_MAX];
	if (HAS_FD(info)) {
		lkl_sys_close(FD_OF(info));
		info->Context = 0;
	}
	if (info->DeletePending && read_write && to_lkl_path(name, path, sizeof(path)) == 0) {
		long ret = info->IsDirectory ? lkl_sys_rmdir(path) : lkl_sys_unlink(path);
		/* SQLite in rollback mode commits by deleting its journal, and with
		 * synchronous=EXTRA asks for the folder to be synced after -- which
		 * its Windows code cannot do. Measured on the real test stick: the
		 * deletion was lost to a pull, the journal came back, and SQLite
		 * rolled back a commit it had acknowledged. */
		if (ret == 0)
			sync_parent(path);
	}
}

static void DOKAN_CALLBACK jh_close(LPCWSTR name, PDOKAN_FILE_INFO info)
{
	(void)name;
	if (HAS_FD(info)) {
		lkl_sys_close(FD_OF(info));
		info->Context = 0;
	}
}

static int open_for(LPCWSTR name, int flags, int *opened)
{
	char path[LKL_PATH_MAX];
	long ret;
	*opened = 0;
	if (to_lkl_path(name, path, sizeof(path)) < 0)
		return -LKL_ENOENT;
	ret = lkl_sys_open(path, flags, 0);
	if (ret >= 0)
		*opened = 1;
	return (int)ret;
}

static NTSTATUS DOKAN_CALLBACK jh_read(LPCWSTR name, LPVOID buffer, DWORD length,
				       LPDWORD read, LONGLONG offset, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	int opened = 0, fd = HAS_FD(info) ? FD_OF(info) : open_for(name, LKL_O_RDONLY, &opened);
	long ret;

	*read = 0;
	if (fd < 0)
		return from_lkl(fd);
	ret = lkl_sys_pread64(fd, buffer, length, offset);
	if (ret >= 0)
		*read = (DWORD)ret;
	if (opened)
		lkl_sys_close(fd);
	return ret < 0 ? from_lkl(ret) : STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_write(LPCWSTR name, LPCVOID buffer, DWORD length,
					LPDWORD written, LONGLONG offset, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	int opened = 0, fd = HAS_FD(info) ? FD_OF(info) : open_for(name, LKL_O_WRONLY, &opened);
	struct lkl_stat st;
	long ret;

	*written = 0;
	if (!read_write)
		return STATUS_MEDIA_WRITE_PROTECTED;
	if (fd < 0)
		return from_lkl(fd);
	if (info->WriteToEndOfFile) {
		if (lkl_sys_fstat(fd, &st) < 0) {
			if (opened) lkl_sys_close(fd);
			return STATUS_IO_DEVICE_ERROR;
		}
		offset = st.st_size;
	}
	ret = lkl_sys_pwrite64(fd, buffer, length, offset);
	if (ret >= 0)
		*written = (DWORD)ret;
	if (opened)
		lkl_sys_close(fd);
	return ret < 0 ? from_lkl(ret) : STATUS_SUCCESS;
}

/* FlushFileBuffers from a Windows program -- SQLite's and PostgreSQL's
 * commit, a careful program's save. It becomes fsync inside the kernel,
 * which now reaches the disk (docs/0c-step1-flush-proof.md). If this ever
 * returns success without the fsync, a pulled drive loses "saved" data. */
static NTSTATUS DOKAN_CALLBACK jh_flush(LPCWSTR name, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	int opened = 0, fd = HAS_FD(info) ? FD_OF(info) : open_for(name, LKL_O_RDONLY, &opened);
	long ret;
	if (fd < 0)
		return from_lkl(fd);
	ret = lkl_sys_fsync(fd);
	if (opened)
		lkl_sys_close(fd);
	return from_lkl(ret);
}

static NTSTATUS DOKAN_CALLBACK jh_set_end(LPCWSTR name, LONGLONG length, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	int opened = 0, fd = HAS_FD(info) ? FD_OF(info) : open_for(name, LKL_O_WRONLY, &opened);
	long ret;
	if (!read_write)
		return STATUS_MEDIA_WRITE_PROTECTED;
	if (fd < 0)
		return from_lkl(fd);
	ret = lkl_sys_ftruncate(fd, length);
	if (opened)
		lkl_sys_close(fd);
	return from_lkl(ret);
}

/* Windows preallocates; ext4 does not need it. Only shrinking matters. */
static NTSTATUS DOKAN_CALLBACK jh_set_allocation(LPCWSTR name, LONGLONG length, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	int opened = 0, fd = HAS_FD(info) ? FD_OF(info) : open_for(name, LKL_O_WRONLY, &opened);
	struct lkl_stat st;
	long ret = 0;
	if (!read_write)
		return STATUS_MEDIA_WRITE_PROTECTED;
	if (fd < 0)
		return from_lkl(fd);
	if (lkl_sys_fstat(fd, &st) == 0 && length < st.st_size)
		ret = lkl_sys_ftruncate(fd, length);
	if (opened)
		lkl_sys_close(fd);
	return from_lkl(ret);
}

static NTSTATUS DOKAN_CALLBACK jh_set_attributes(LPCWSTR name, DWORD attributes, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	/* Windows' read-only, hidden and archive bits have no ext4 meaning;
	 * accepting them is what keeps Windows programs from failing a save. */
	(void)name; (void)attributes; (void)info;
	return read_write ? STATUS_SUCCESS : STATUS_MEDIA_WRITE_PROTECTED;
}

static NTSTATUS DOKAN_CALLBACK jh_set_time(LPCWSTR name, CONST FILETIME *created,
					   CONST FILETIME *accessed, CONST FILETIME *written,
					   PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	char path[LKL_PATH_MAX];
	struct __lkl__kernel_timespec times[2];
	(void)created; (void)info;
	if (!read_write)
		return STATUS_MEDIA_WRITE_PROTECTED;
	if (to_lkl_path(name, path, sizeof(path)) < 0)
		return STATUS_OBJECT_NAME_INVALID;
	for (int i = 0; i < 2; i++) {
		const FILETIME *t = i == 0 ? accessed : written;
		if (!t || (t->dwLowDateTime == 0 && t->dwHighDateTime == 0)) {
			times[i].tv_sec = 0;
			times[i].tv_nsec = JH_UTIME_OMIT;
			continue;
		}
		long long ticks = ((long long)t->dwHighDateTime << 32) | t->dwLowDateTime;
		times[i].tv_sec = ticks / 10000000LL - 11644473600LL;
		times[i].tv_nsec = (ticks % 10000000LL) * 100;
	}
	return from_lkl(lkl_sys_utimensat(LKL_AT_FDCWD, path, times, 0));
}

/* Deletion is only checked here; it happens in Cleanup. */
static NTSTATUS DOKAN_CALLBACK jh_delete_file(LPCWSTR name, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	(void)name; (void)info;
	return read_write ? STATUS_SUCCESS : STATUS_MEDIA_WRITE_PROTECTED;
}

static NTSTATUS DOKAN_CALLBACK jh_delete_directory(LPCWSTR name, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	char path[LKL_PATH_MAX];
	struct lkl_dir *dir;
	struct lkl_linux_dirent64 *entry;
	int error = 0, empty = 1;
	(void)info;
	if (!read_write)
		return STATUS_MEDIA_WRITE_PROTECTED;
	if (to_lkl_path(name, path, sizeof(path)) < 0)
		return STATUS_OBJECT_NAME_INVALID;
	dir = lkl_opendir(path, &error);
	if (!dir)
		return from_lkl(-error);
	while ((entry = lkl_readdir(dir)))
		if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, "..")) {
			empty = 0;
			break;
		}
	lkl_closedir(dir);
	return empty ? STATUS_SUCCESS : STATUS_DIRECTORY_NOT_EMPTY;
}

/* Rename. ext4's rename is atomic, which is exactly what the
 * write-temp-then-rename pattern every careful saver uses depends on. */
static NTSTATUS DOKAN_CALLBACK jh_move(LPCWSTR from, LPCWSTR to, BOOL replace, PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
	char a[LKL_PATH_MAX], b[LKL_PATH_MAX];
	struct lkl_stat st;
	(void)info;
	if (!read_write)
		return STATUS_MEDIA_WRITE_PROTECTED;
	if (to_lkl_path(from, a, sizeof(a)) < 0 || to_lkl_path(to, b, sizeof(b)) < 0)
		return STATUS_OBJECT_NAME_INVALID;
	if (!replace && lkl_sys_stat(b, &st) == 0)
		return STATUS_OBJECT_NAME_COLLISION;
	long ret = lkl_sys_rename(a, b);
	if (ret < 0)
		return from_lkl(ret);
	/* Measured in the 0b harness: without this an acknowledged replace came
	 * back as the previous version after the drive was pulled. */
	ret = sync_parent(b);
	return ret < 0 ? from_lkl(ret) : STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_get_info(LPCWSTR name, LPBY_HANDLE_FILE_INFORMATION out,
					   PDOKAN_FILE_INFO info)
{
	JH_IF_GONE();
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
	JH_IF_GONE();
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
	JH_IF_GONE();
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
	write_status("mounted");
	return STATUS_SUCCESS;
}

static NTSTATUS DOKAN_CALLBACK jh_unmounted(PDOKAN_FILE_INFO info)
{
	(void)info;
	fprintf(stderr, "unmounted (flushes that reached the disk: %ld)\n", (long)flush_count);
	fflush(stderr);
	return STATUS_SUCCESS;
}

static DOKAN_OPERATIONS operations = {
	.ZwCreateFile = jh_create,
	.Cleanup = jh_cleanup,
	.CloseFile = jh_close,
	.ReadFile = jh_read,
	.WriteFile = jh_write,
	.FlushFileBuffers = jh_flush,
	.SetEndOfFile = jh_set_end,
	.SetAllocationSize = jh_set_allocation,
	.SetFileAttributes = jh_set_attributes,
	.SetFileTime = jh_set_time,
	.DeleteFile = jh_delete_file,
	.DeleteDirectory = jh_delete_directory,
	.MoveFile = jh_move,
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
		"usage: ext4win --disk <image|\\\\.\\PhysicalDriveN> --mount <letter or path>\n"
		"              [--part N] [--read-write] [--serial N] [--mount-manager]\n"
		"              [--status FILE] [--debug]\n"
		"exit: 0 unmounted cleanly, 1 failed, 10 the drive was taken away\n");
}

int main(int argc, char **argv)
{
	const char *disk_path = NULL, *mount = NULL;
	unsigned part = 0, serial = 0, debug = 0, mount_manager = 0;
	DOKAN_OPTIONS options;
	long ret;
	int id, status;
	HANDLE watcher;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--disk") && i + 1 < argc) disk_path = argv[++i];
		else if (!strcmp(argv[i], "--mount") && i + 1 < argc) mount = argv[++i];
		else if (!strcmp(argv[i], "--part") && i + 1 < argc) part = (unsigned)atoi(argv[++i]);
		else if (!strcmp(argv[i], "--serial") && i + 1 < argc) serial = (unsigned)strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--read-write")) read_write = 1;
		/* Register the drive letter with Windows' mount manager rather than
		 * assigning it ourselves. Without it the drive works for ordinary file
		 * calls but Windows cannot map the letter back to a volume, so
		 * GetFinalPathNameByHandle fails -- and OpenClaw reads that failure as
		 * "this database is on network storage" and drops SQLite to
		 * journal_mode=DELETE. Measured in docs/0c-step4-ownership-and-paths.md.
		 * It costs the per-session privacy CURRENT_SESSION gives, so it is a
		 * flag until Project 7 settles which the product wants. */
		else if (!strcmp(argv[i], "--mount-manager")) mount_manager = 1;
		/* Where to write what the drive is doing, for whatever is
		 * supervising it. On the host: when it matters, the drive is gone. */
		else if (!strcmp(argv[i], "--status") && i + 1 < argc) status_path = argv[++i];
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
		fprintf(stderr, "cannot open %s: Windows error %lu\n", disk_path,
			(unsigned long)GetLastError());
		return JH_EXIT_FAILED;
	}
	counting_ops = lkl_dev_blk_ops;
	counting_ops.request = counting_request;
	counting_ops.get_capacity = disk_capacity;
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
	options.Options = DOKAN_OPTION_REMOVABLE |
			  (mount_manager ? DOKAN_OPTION_MOUNT_MANAGER : DOKAN_OPTION_CURRENT_SESSION) |
			  DOKAN_OPTION_CASE_SENSITIVE |
			  (read_write ? 0 : DOKAN_OPTION_WRITE_PROTECT) |
			  (debug ? (DOKAN_OPTION_DEBUG | DOKAN_OPTION_STDERR) : 0);
	options.SectorSize = JH_SECTOR;
	options.AllocationUnitSize = 4096;
	options.Timeout = 30000;

	wake_event = CreateEventW(NULL, TRUE, FALSE, NULL);
	unmounted_event = CreateEventW(NULL, TRUE, FALSE, NULL);
	if (!wake_event || !unmounted_event) {
		fprintf(stderr, "could not prepare the drive watch\n");
		return JH_EXIT_FAILED;
	}
	watcher = CreateThread(NULL, 0, watch_for_removal, NULL, 0, NULL);
	if (!watcher) {
		fprintf(stderr, "could not start the drive watch\n");
		return JH_EXIT_FAILED;
	}
	SetConsoleCtrlHandler(on_console_signal, TRUE);

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
	SetEvent(wake_event);			/* release the watcher either way */
	WaitForSingleObject(watcher, 5000);
	CloseHandle(watcher);

	/* Unmounting a drive that is not there would ask ext4 to write its
	 * journal to a disk that cannot take it. Nothing is lost by skipping it:
	 * what was flushed is on the drive, and the journal replays on re-plug. */
	if (!drive_gone) {
		lkl_umount_dev(id, part, 0, 5000);
		write_status("unmounted");
	}
	SetEvent(unmounted_event);		/* a close handler may be waiting on this */
	lkl_sys_halt();
	lkl_cleanup();
	CloseHandle(disk.handle);
	if (drive_gone)
		return JH_EXIT_REMOVED;
	return status == DOKAN_SUCCESS ? JH_EXIT_OK : JH_EXIT_FAILED;
}
