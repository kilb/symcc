#define _GNU_SOURCE

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

/* Benchmark-only flock bridge. Production code must use a qualified shared FS. */

#define PROXY_MAGIC "SFLK001"
#define PROXY_MAGIC_BYTES 8
#define PROXY_MAX_PATH 4096

struct proxy_header {
  unsigned char magic[PROXY_MAGIC_BYTES];
  uint32_t operation;
  uint32_t path_size;
};

struct proxy_lock {
  int application_fd;
  int socket_fd;
  struct proxy_lock *next;
};

static pthread_mutex_t proxy_mutex = PTHREAD_MUTEX_INITIALIZER;
static struct proxy_lock *proxy_locks;
static int (*real_flock_fn)(int, int);
static int (*real_close_fn)(int);

static void resolve_symbols(void) {
  if (real_flock_fn == NULL)
    real_flock_fn = (int (*)(int, int))dlsym(RTLD_NEXT, "flock");
  if (real_close_fn == NULL)
    real_close_fn = (int (*)(int))dlsym(RTLD_NEXT, "close");
}

static int write_all(int fd, const void *buffer, size_t size) {
  const unsigned char *cursor = buffer;
  while (size > 0) {
    ssize_t written = send(fd, cursor, size, MSG_NOSIGNAL);
    if (written < 0) {
      if (errno == EINTR)
        continue;
      return -1;
    }
    if (written == 0) {
      errno = EIO;
      return -1;
    }
    cursor += (size_t)written;
    size -= (size_t)written;
  }
  return 0;
}

static int read_all(int fd, void *buffer, size_t size) {
  unsigned char *cursor = buffer;
  while (size > 0) {
    ssize_t received = recv(fd, cursor, size, 0);
    if (received < 0) {
      if (errno == EINTR)
        continue;
      return -1;
    }
    if (received == 0) {
      errno = EIO;
      return -1;
    }
    cursor += (size_t)received;
    size -= (size_t)received;
  }
  return 0;
}

static int proxy_target(struct sockaddr_in *address) {
  const char *configured = getenv("SYMCC_FLOCK_PROXY_ADDR");
  if (configured == NULL || configured[0] == '\0')
    return 0;
  const char *separator = strrchr(configured, ':');
  if (separator == NULL || separator == configured || separator[1] == '\0') {
    errno = EINVAL;
    return -1;
  }
  char host[INET_ADDRSTRLEN];
  size_t host_size = (size_t)(separator - configured);
  if (host_size >= sizeof(host)) {
    errno = EINVAL;
    return -1;
  }
  memcpy(host, configured, host_size);
  host[host_size] = '\0';
  char *end = NULL;
  errno = 0;
  long port = strtol(separator + 1, &end, 10);
  if (errno != 0 || end == separator + 1 || *end != '\0' || port < 1 ||
      port > 65535) {
    errno = EINVAL;
    return -1;
  }
  memset(address, 0, sizeof(*address));
  address->sin_family = AF_INET;
  address->sin_port = htons((uint16_t)port);
  if (inet_pton(AF_INET, host, &address->sin_addr) != 1) {
    errno = EINVAL;
    return -1;
  }
  return 1;
}

static int lock_path(int fd, char path[PROXY_MAX_PATH], size_t *path_size) {
  char descriptor_path[64];
  int printed = snprintf(descriptor_path, sizeof(descriptor_path),
                         "/proc/self/fd/%d", fd);
  if (printed < 0 || (size_t)printed >= sizeof(descriptor_path)) {
    errno = EINVAL;
    return -1;
  }
  ssize_t length = readlink(descriptor_path, path, PROXY_MAX_PATH - 1);
  if (length < 0)
    return -1;
  path[length] = '\0';
  *path_size = (size_t)length;
  return 0;
}

static int path_uses_proxy(const char *path, size_t path_size) {
  const char *prefix = getenv("SYMCC_FLOCK_PROXY_PREFIX");
  if (prefix == NULL || prefix[0] != '/') {
    errno = EINVAL;
    return -1;
  }
  size_t prefix_size = strlen(prefix);
  while (prefix_size > 1 && prefix[prefix_size - 1] == '/')
    prefix_size--;
  if (path_size < prefix_size || memcmp(path, prefix, prefix_size) != 0)
    return 0;
  return path_size == prefix_size || path[prefix_size] == '/';
}

static void remove_proxy_lock(int application_fd) {
  resolve_symbols();
  pthread_mutex_lock(&proxy_mutex);
  struct proxy_lock **cursor = &proxy_locks;
  while (*cursor != NULL) {
    if ((*cursor)->application_fd == application_fd) {
      struct proxy_lock *removed = *cursor;
      *cursor = removed->next;
      int socket_fd = removed->socket_fd;
      free(removed);
      pthread_mutex_unlock(&proxy_mutex);
      real_close_fn(socket_fd);
      return;
    }
    cursor = &(*cursor)->next;
  }
  pthread_mutex_unlock(&proxy_mutex);
}

int flock(int fd, int operation) {
  resolve_symbols();
  if (real_flock_fn == NULL || real_close_fn == NULL) {
    errno = ENOSYS;
    return -1;
  }
  struct sockaddr_in address;
  int configured = proxy_target(&address);
  if (configured == 0)
    return real_flock_fn(fd, operation);
  if (configured < 0)
    return -1;

  char path[PROXY_MAX_PATH];
  size_t path_size = 0;
  if (lock_path(fd, path, &path_size) != 0)
    return -1;
  int proxied = path_uses_proxy(path, path_size);
  if (proxied == 0)
    return real_flock_fn(fd, operation);
  if (proxied < 0)
    return -1;

  remove_proxy_lock(fd);
  if ((operation & ~LOCK_NB) == LOCK_UN)
    return 0;
  int base_operation = operation & ~LOCK_NB;
  if (base_operation != LOCK_SH && base_operation != LOCK_EX) {
    errno = EINVAL;
    return -1;
  }

  int socket_fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (socket_fd < 0)
    return -1;
  if (connect(socket_fd, (const struct sockaddr *)&address, sizeof(address)) !=
      0) {
    int saved = errno;
    real_close_fn(socket_fd);
    errno = saved;
    return -1;
  }
  struct proxy_header header;
  memset(&header, 0, sizeof(header));
  memcpy(header.magic, PROXY_MAGIC, sizeof(PROXY_MAGIC));
  header.operation = htonl((uint32_t)operation);
  header.path_size = htonl((uint32_t)path_size);
  uint32_t response = 0;
  if (write_all(socket_fd, &header, sizeof(header)) != 0 ||
      write_all(socket_fd, path, path_size) != 0 ||
      read_all(socket_fd, &response, sizeof(response)) != 0) {
    int saved = errno;
    real_close_fn(socket_fd);
    errno = saved;
    return -1;
  }
  int error_number = (int)ntohl(response);
  if (error_number != 0) {
    real_close_fn(socket_fd);
    errno = error_number;
    return -1;
  }

  struct proxy_lock *entry = malloc(sizeof(*entry));
  if (entry == NULL) {
    real_close_fn(socket_fd);
    errno = ENOMEM;
    return -1;
  }
  entry->application_fd = fd;
  entry->socket_fd = socket_fd;
  pthread_mutex_lock(&proxy_mutex);
  entry->next = proxy_locks;
  proxy_locks = entry;
  pthread_mutex_unlock(&proxy_mutex);
  return 0;
}

int close(int fd) {
  resolve_symbols();
  if (real_close_fn == NULL) {
    errno = ENOSYS;
    return -1;
  }
  remove_proxy_lock(fd);
  return real_close_fn(fd);
}
