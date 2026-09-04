/*
 * Functional AFL edge/data-map target for F247 evidence plumbing.
 *
 * This is intentionally a one-byte micro target matching the F245 synthetic
 * Query IR witnesses. It is not a performance benchmark.
 */

#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

static volatile uint32_t sink;

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  int fd = open(argv[1], O_RDONLY);
  if (fd < 0)
    return 2;
  uint8_t value = 0;
  ssize_t length = read(fd, &value, 1);
  close(fd);
  if (length != 1)
    return 0;

  /* 0xbd is one F246 SAT candidate, so the data map has measurable gain. */
  uint8_t target = 0xbd;
  int (*volatile compare)(const void *, const void *, size_t) = memcmp;
  if (compare(&value, &target, 1) == 0)
    sink += 1;
  if (value < 0x10)
    sink += 2;
  if (value > 0xf0)
    sink += 4;
  if ((int8_t)value < 0)
    sink += 8;
  switch (value & 3U) {
  case 0:
    sink += 16;
    break;
  case 1:
    sink += 32;
    break;
  case 2:
    sink += 64;
    break;
  default:
    sink += 128;
    break;
  }
  return 0;
}
