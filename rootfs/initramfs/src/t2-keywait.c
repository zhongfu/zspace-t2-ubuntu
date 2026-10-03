// ZSpace T2 flash-mode press counter.
//
// The installer initramfs has no systemd-logind (nothing else owns the power
// button while it runs), but it shares the same contract as the rootfs
// installer: a disk write is confirmed by an exact number of KEY_POWER
// presses, and the counting process must hold the input device exclusively so
// no other consumer can act on the presses.  evtest --grab did that on the
// rootfs; the initramfs has no evtest, so this is the minimal replacement.
//
// Usage: t2-keywait WINDOW_SECONDS COUNT KEYCODE "device name" [STATUS_FILE]
//
//   * finds the /dev/input/event* whose EVIOCGNAME equals the name (an empty
//     name selects the first device that reports KEY_POWER);
//   * EVIOCGRABs it, so the kernel routes its events only to us;
//   * counts presses (EV_KEY, the configured code, value 1) and **returns the
//     instant COUNT is reached**, so the caller can start work immediately
//     instead of waiting out the window (measured 2026-10-02: the installer sat
//     for a full 60 s after the 10th press because the counter kept listening to
//     see whether an 11th would arrive).  COUNT presses inside the window is the
//     confirmation; fewer than COUNT by the time the window expires is a
//     failure - one press too few cannot satisfy the gate by accident;
//   * with an optional STATUS_FILE, rewrites that file with the running count
//     (decimal, newline, fsync) after *every* counted press, so the caller can
//     drive LED feedback from the count while we still hold the device;
//   * exits nonzero on timeout, on no matching device, and on a failed grab.
//
// Exit codes: 0 = COUNT reached, 1 = fewer than COUNT when the window expired /
//             no device, 2 = bad usage, 3 = no matching device, 4 = grab failed.
//
// Build (static, aarch64; see the header of rootfs/initramfs/init):
//   LD_LIBRARY_PATH=<cross>/usr/lib/x86_64-linux-gnu \
//   <cross>/usr/bin/aarch64-linux-gnu-gcc \
//       -isystem /usr/aarch64-linux-gnu/include \
//       -B/usr/aarch64-linux-gnu/lib -L/usr/aarch64-linux-gnu/lib \
//       -static -O2 -s -o bin/t2-keywait src/t2-keywait.c
// where <cross> = tools/cross/root (needs libc6-dev-arm64-cross).

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define NAME_LEN 256
#define EV_BATCH 16

static long long now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

// Rewrite `path` with the running press count.  Flushed (fsync) so the caller
// can read it back while we still hold the input device.  Optional: a NULL or
// empty path means "do not report".
static void write_status(const char *path, long count)
{
	char buf[32];
	int n;
	int fd;
	ssize_t wrote;

	if (path == NULL || path[0] == '\0')
		return;
	fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (fd < 0)
		return;
	n = snprintf(buf, sizeof(buf), "%ld\n", count);
	if (n > 0) {
		wrote = write(fd, buf, (size_t)n);
		(void)wrote;
	}
	fsync(fd);
	close(fd);
}

static int device_name_matches(int fd, const char *want)
{
	char buf[NAME_LEN];

	if (ioctl(fd, EVIOCGNAME(sizeof(buf)), buf) < 0)
		return 0;
	return strcmp(buf, want) == 0;
}

static int device_has_key(int fd, int code)
{
	unsigned long bits[(KEY_MAX / (8 * sizeof(unsigned long))) + 1];

	memset(bits, 0, sizeof(bits));
	if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(bits)), bits) < 0)
		return 0;
	return (bits[code / (8 * sizeof(unsigned long))] >>
		(code % (8 * sizeof(unsigned long)))) & 1UL;
}

// Return an opened /dev/input/event* matching `want`.  An empty `want` picks
// the first device that can emit `code`.
static int open_matching(const char *want, int code)
{
	DIR *d;
	struct dirent *e;
	int fd = -1;

	d = opendir("/dev/input");
	if (d == NULL)
		return -1;

	while ((e = readdir(d)) != NULL) {
		char path[320];
		int f;

		if (strncmp(e->d_name, "event", 5) != 0)
			continue;
		snprintf(path, sizeof(path), "/dev/input/%s", e->d_name);
		f = open(path, O_RDONLY | O_NONBLOCK);
		if (f < 0)
			continue;
		if (want[0] != '\0') {
			if (device_name_matches(f, want)) {
				fd = f;
				break;
			}
		} else if (device_has_key(f, code)) {
			fd = f;
			break;
		}
		close(f);
	}
	closedir(d);
	return fd;
}

int main(int argc, char **argv)
{
	long window;
	long want_count;
	int code;
	const char *name;
	const char *status;
	int fd;
	long long deadline;
	long count = 0;

	if (argc != 5 && argc != 6) {
		fprintf(stderr,
			"usage: t2-keywait WINDOW_SECONDS COUNT KEYCODE \"device name\" [STATUS_FILE]\n");
		return 2;
	}

	window = strtol(argv[1], NULL, 10);
	want_count = strtol(argv[2], NULL, 10);
	code = (int)strtol(argv[3], NULL, 10);
	name = argv[4];
	status = (argc == 6) ? argv[5] : NULL;

	if (window < 0 || want_count <= 0 || code <= 0) {
		fprintf(stderr, "t2-keywait: bad window/count/keycode\n");
		return 2;
	}

	fd = open_matching(name, code);
	if (fd < 0) {
		fprintf(stderr, "t2-keywait: no input device named '%s'\n",
			name[0] ? name : "(any with keycode)");
		return 3;
	}
	if (ioctl(fd, EVIOCGRAB, (void *)1) < 0) {
		fprintf(stderr, "t2-keywait: EVIOCGRAB failed on the device: %s\n",
			strerror(errno));
		close(fd);
		return 4;
	}

	deadline = now_ms() + window * 1000;
	while (1) {
		struct input_event ev[EV_BATCH];
		ssize_t got;
		size_t i;
		long long left = deadline - now_ms();
		struct pollfd pfd;

		if (left <= 0)
			break;
		pfd.fd = fd;
		pfd.events = POLLIN;
		pfd.revents = 0;
		if (poll(&pfd, 1, (int)left) <= 0)
			continue;
		got = read(fd, ev, sizeof(ev));
		if (got < 0) {
			if (errno == EAGAIN || errno == EINTR)
				continue;
			break;
		}
		for (i = 0; i < (size_t)got / sizeof(ev[0]); i++) {
			if (ev[i].type == EV_KEY && ev[i].code == code &&
			    ev[i].value == 1) {
				count++;
				write_status(status, count);
				if (count >= want_count)
					goto reached;
			}
		}
	}

reached:
	ioctl(fd, EVIOCGRAB, (void *)0);
	close(fd);

	if (count != want_count) {
		fprintf(stderr, "t2-keywait: counted %ld of %ld press(es)\n",
			count, want_count);
		return 1;
	}
	return 0;
}
