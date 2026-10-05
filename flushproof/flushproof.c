/*
 * 0c step 1: does a "save now" inside LKL ever reach the disk on Windows?
 *
 *   flushproof.exe <ext4 image> stock      LKL exactly as shipped
 *   flushproof.exe <ext4 image> flush      the disk advertises a write cache
 *
 * ext4 makes its journal durable by asking the block layer for flushes, and
 * PostgreSQL and SQLite make commits durable with fsync, which turns into the
 * same flushes. LKL's Windows host answers a flush with FlushFileBuffers -- but
 * only if the kernel sends one, and the kernel sends one only to a disk that
 * says it has a write cache (VIRTIO_BLK_F_FLUSH). LKL's virtual disk says it
 * has none (device_features = 0), so every flush is dropped before it reaches
 * the host, and on Windows a write can still be in the system cache when the
 * drive is pulled.
 *
 * This counts what actually reaches the host -- writes and flushes -- around an
 * fsync, a sync and an unmount, in both modes. GPL-2.0, like LKL.
 */
#include <stdio.h>
#include <string.h>
#include <windows.h>
#include <lkl.h>
#include <lkl_host.h>
#include "virtio.h"

#define VIRTIO_BLK_F_FLUSH 9

static volatile LONG writes, flushes;
static struct lkl_dev_blk_ops counting_ops;

static int counting_request(struct lkl_disk disk, struct lkl_blk_req *req)
{
	if (req->type == LKL_DEV_BLK_TYPE_FLUSH || req->type == LKL_DEV_BLK_TYPE_FLUSH_OUT)
		InterlockedIncrement(&flushes);
	else if (req->type == LKL_DEV_BLK_TYPE_WRITE)
		InterlockedIncrement(&writes);
	return lkl_dev_blk_ops.request(disk, req);
}

static void phase(const char *name, LONG *w0, LONG *f0)
{
	printf("  \"%s\": {\"writes\": %ld, \"flushes\": %ld},\n", name, writes - *w0, flushes - *f0);
	*w0 = writes;
	*f0 = flushes;
}

static int fail(const char *what, long err)
{
	printf("  \"error\": \"%s: %s\"\n}\n", what, err < 0 ? lkl_strerror(err) : "failed");
	return 1;
}

int main(int argc, char **argv)
{
	static char buffer[64 * 1024];
	char mnt[64], path[128];
	struct lkl_disk disk;
	LONG w0 = 0, f0 = 0;
	long ret;
	int id, fd, advertise;

	if (argc != 3 || (strcmp(argv[2], "stock") && strcmp(argv[2], "flush"))) {
		fprintf(stderr, "usage: %s <ext4 image> stock|flush\n", argv[0]);
		return 2;
	}
	advertise = !strcmp(argv[2], "flush");
	memset(buffer, 'j', sizeof(buffer));
	setvbuf(stdout, NULL, _IONBF, 0);
	setvbuf(stderr, NULL, _IONBF, 0);

	memset(&disk, 0, sizeof(disk));
	disk.handle = CreateFileA(argv[1], GENERIC_READ | GENERIC_WRITE, 0, NULL, OPEN_EXISTING,
				  FILE_ATTRIBUTE_NORMAL, NULL);
	if (disk.handle == INVALID_HANDLE_VALUE) {
		fprintf(stderr, "cannot open %s (error %lu)\n", argv[1], GetLastError());
		return 2;
	}
	counting_ops = lkl_dev_blk_ops;
	counting_ops.request = counting_request;
	disk.ops = &counting_ops;

	printf("{\n  \"mode\": \"%s\",\n", argv[2]);
	fprintf(stderr, "step: lkl_init\n");
	if (lkl_init(&lkl_host_ops) < 0)
		return fail("lkl_init", -1);
	fprintf(stderr, "step: lkl_disk_add\n");
	id = lkl_disk_add(&disk);
	if (id < 0)
		return fail("lkl_disk_add", id);
	/* The guest reads the features when it probes the disk at boot, so this
	 * is the same as LKL with the one-bit change, without rebuilding it. */
	if (advertise)
		((struct virtio_dev *)disk.dev)->device_features |= 1ULL << VIRTIO_BLK_F_FLUSH;

	fprintf(stderr, "step: lkl_start_kernel\n");
	ret = lkl_start_kernel("mem=64M loglevel=3");
	if (ret < 0)
		return fail("lkl_start_kernel", ret);
	fprintf(stderr, "step: lkl_mount_dev\n");
	ret = lkl_mount_dev(id, 0, "ext4", 0, NULL, mnt, sizeof(mnt));
	if (ret < 0)
		return fail("lkl_mount_dev", ret);
	phase("mount", &w0, &f0);

	snprintf(path, sizeof(path), "%s/saved.bin", mnt);
	fd = lkl_sys_open(path, LKL_O_CREAT | LKL_O_WRONLY | LKL_O_TRUNC, 0600);
	if (fd < 0)
		return fail("open", fd);
	if (lkl_sys_write(fd, buffer, sizeof(buffer)) != sizeof(buffer))
		return fail("write", -1);
	ret = lkl_sys_fsync(fd);
	if (ret < 0)
		return fail("fsync", ret);
	phase("write_then_fsync", &w0, &f0);

	if (lkl_sys_write(fd, buffer, sizeof(buffer)) != sizeof(buffer))
		return fail("write", -1);
	lkl_sys_close(fd);
	lkl_sys_sync();
	phase("write_then_sync", &w0, &f0);

	ret = lkl_umount_dev(id, 0, 0, 1000);
	if (ret < 0)
		return fail("umount", ret);
	phase("umount", &w0, &f0);

	lkl_sys_halt();
	lkl_cleanup();
	CloseHandle(disk.handle);
	printf("  \"total\": {\"writes\": %ld, \"flushes\": %ld}\n}\n", writes, flushes);
	return 0;
}
