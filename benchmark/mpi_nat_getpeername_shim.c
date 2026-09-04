/*
 * Test-only IPv4 peer-address normalization for MPI over an SSH/NAT tunnel.
 *
 * Load this library only for an explicitly documented multi-node experiment:
 *
 *   SYMCC_MPI_NAT_SOURCE_MAP="127.0.0.1=203.0.113.1" \
 *   LD_PRELOAD=./mpi_nat_getpeername_shim.so command
 *
 * Each comma-separated rule rewrites the source address returned by
 * getpeername(2). Ports and non-IPv4 sockets are left untouched. The shim is
 * deliberately fail-closed: malformed rules disable all rewriting.
 */

#define _GNU_SOURCE

#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <pthread.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/ioctl.h>

#define SYMCC_MPI_NAT_MAX_RULES 16

struct address_rule {
  struct in_addr observed;
  struct in_addr advertised;
};

struct interface_rule {
  struct in_addr observed;
  struct in_addr advertised;
  struct in_addr advertised_mask;
  int local;
};

static int (*real_getpeername)(int, struct sockaddr *, socklen_t *);
static int (*real_getsockname)(int, struct sockaddr *, socklen_t *);
static int (*real_getifaddrs)(struct ifaddrs **);
static void (*real_freeifaddrs)(struct ifaddrs *);
static int (*real_bind)(int, const struct sockaddr *, socklen_t);
static int (*real_connect)(int, const struct sockaddr *, socklen_t);
static int (*real_ioctl)(int, unsigned long, ...);
static struct address_rule rules[SYMCC_MPI_NAT_MAX_RULES];
static size_t rule_count;
static struct interface_rule interface_rules[SYMCC_MPI_NAT_MAX_RULES];
static size_t interface_rule_count;
static struct in_addr local_connect_source;
static int local_connect_source_configured;
static pthread_once_t initialize_once = PTHREAD_ONCE_INIT;

static int parse_prefix(char *text, struct in_addr *address,
                        struct in_addr *mask) {
  char *slash = strchr(text, '/');
  if (slash == NULL)
    return 0;
  *slash = '\0';
  char *end = NULL;
  long prefix = strtol(slash + 1, &end, 10);
  if (end == slash + 1 || *end != '\0' || prefix < 0 || prefix > 32 ||
      inet_pton(AF_INET, text, address) != 1)
    return 0;
  uint32_t host_mask = prefix == 0 ? 0 : UINT32_MAX << (32 - prefix);
  mask->s_addr = htonl(host_mask);
  return 1;
}

static void parse_interface_rules(void) {
  const char *mapping = getenv("SYMCC_MPI_NAT_INTERFACE_MAP");
  if (mapping == NULL || mapping[0] == '\0')
    return;
  char *copy = strdup(mapping);
  if (copy == NULL)
    return;

  char *save = NULL;
  for (char *item = strtok_r(copy, ",", &save); item != NULL;
       item = strtok_r(NULL, ",", &save)) {
    char *separator = strchr(item, '=');
    struct in_addr ignored_mask;
    if (separator == NULL ||
        interface_rule_count == SYMCC_MPI_NAT_MAX_RULES) {
      interface_rule_count = 0;
      break;
    }
    *separator = '\0';
    if (!parse_prefix(item, &interface_rules[interface_rule_count].observed,
                      &ignored_mask) ||
        !parse_prefix(separator + 1,
                      &interface_rules[interface_rule_count].advertised,
                      &interface_rules[interface_rule_count].advertised_mask)) {
      interface_rule_count = 0;
      break;
    }
    ++interface_rule_count;
  }
  free(copy);
}

static void detect_local_interface_rules(void) {
  struct ifaddrs *interfaces = NULL;
  if (real_getifaddrs == NULL || real_freeifaddrs == NULL ||
      real_getifaddrs(&interfaces) != 0)
    return;
  for (struct ifaddrs *item = interfaces; item != NULL;
       item = item->ifa_next) {
    if (item->ifa_addr == NULL || item->ifa_addr->sa_family != AF_INET)
      continue;
    const struct in_addr address =
        ((const struct sockaddr_in *)item->ifa_addr)->sin_addr;
    for (size_t index = 0; index < interface_rule_count; ++index) {
      if (address.s_addr == interface_rules[index].observed.s_addr)
        interface_rules[index].local = 1;
    }
  }
  real_freeifaddrs(interfaces);
}

static void initialize_shim(void) {
  const char *mapping = getenv("SYMCC_MPI_NAT_SOURCE_MAP");
  const char *local_source = getenv("SYMCC_MPI_NAT_LOCAL_SOURCE");
  real_getpeername = dlsym(RTLD_NEXT, "getpeername");
  real_getsockname = dlsym(RTLD_NEXT, "getsockname");
  real_getifaddrs = dlsym(RTLD_NEXT, "getifaddrs");
  real_freeifaddrs = dlsym(RTLD_NEXT, "freeifaddrs");
  real_bind = dlsym(RTLD_NEXT, "bind");
  real_connect = dlsym(RTLD_NEXT, "connect");
  real_ioctl = dlsym(RTLD_NEXT, "ioctl");
  parse_interface_rules();
  detect_local_interface_rules();
  if (local_source != NULL && local_source[0] != '\0' &&
      inet_pton(AF_INET, local_source, &local_connect_source) == 1)
    local_connect_source_configured = 1;
  if (mapping == NULL || mapping[0] == '\0')
    return;

  char *copy = strdup(mapping);
  if (copy == NULL)
    return;

  char *save = NULL;
  for (char *item = strtok_r(copy, ",", &save); item != NULL;
       item = strtok_r(NULL, ",", &save)) {
    char *separator = strchr(item, '=');
    if (separator == NULL || rule_count == SYMCC_MPI_NAT_MAX_RULES) {
      rule_count = 0;
      break;
    }
    *separator = '\0';
    if (inet_pton(AF_INET, item, &rules[rule_count].observed) != 1 ||
        inet_pton(AF_INET, separator + 1,
                  &rules[rule_count].advertised) != 1) {
      rule_count = 0;
      break;
    }
    ++rule_count;
  }
  free(copy);
}

int ioctl(int descriptor, unsigned long request, ...) {
  pthread_once(&initialize_once, initialize_shim);
  if (real_ioctl == NULL) {
    errno = ENOSYS;
    return -1;
  }

  va_list arguments;
  va_start(arguments, request);
  void *argument = va_arg(arguments, void *);
  va_end(arguments);
  int result = real_ioctl(descriptor, request, argument);
  if (result != 0 || argument == NULL)
    return result;

  if (request == SIOCGIFADDR) {
    struct ifreq *interface = argument;
    if (interface->ifr_addr.sa_family != AF_INET)
      return result;
    struct sockaddr_in *address = (struct sockaddr_in *)&interface->ifr_addr;
    for (size_t index = 0; index < interface_rule_count; ++index) {
      if (address->sin_addr.s_addr ==
          interface_rules[index].observed.s_addr) {
        address->sin_addr = interface_rules[index].advertised;
        break;
      }
    }
  } else if (request == SIOCGIFNETMASK) {
    struct ifreq *interface = argument;
    struct ifreq probe;
    memset(&probe, 0, sizeof(probe));
    memcpy(probe.ifr_name, interface->ifr_name, IFNAMSIZ - 1);
    if (real_ioctl(descriptor, SIOCGIFADDR, &probe) != 0 ||
        probe.ifr_addr.sa_family != AF_INET)
      return result;
    struct in_addr observed =
        ((struct sockaddr_in *)&probe.ifr_addr)->sin_addr;
    for (size_t index = 0; index < interface_rule_count; ++index) {
      if (observed.s_addr == interface_rules[index].observed.s_addr) {
        ((struct sockaddr_in *)&interface->ifr_addr)->sin_addr =
            interface_rules[index].advertised_mask;
        break;
      }
    }
  }
  return result;
}

int getifaddrs(struct ifaddrs **interfaces) {
  pthread_once(&initialize_once, initialize_shim);
  if (real_getifaddrs == NULL) {
    errno = ENOSYS;
    return -1;
  }
  int result = real_getifaddrs(interfaces);
  if (result != 0 || interfaces == NULL)
    return result;

  for (struct ifaddrs *item = *interfaces; item != NULL;
       item = item->ifa_next) {
    if (item->ifa_addr == NULL || item->ifa_addr->sa_family != AF_INET)
      continue;
    struct sockaddr_in *address = (struct sockaddr_in *)item->ifa_addr;
    for (size_t index = 0; index < interface_rule_count; ++index) {
      if (address->sin_addr.s_addr !=
          interface_rules[index].observed.s_addr)
        continue;
      address->sin_addr = interface_rules[index].advertised;
      if (item->ifa_netmask != NULL &&
          item->ifa_netmask->sa_family == AF_INET) {
        ((struct sockaddr_in *)item->ifa_netmask)->sin_addr =
            interface_rules[index].advertised_mask;
      }
      break;
    }
  }
  return result;
}

int bind(int socket, const struct sockaddr *address, socklen_t address_length) {
  pthread_once(&initialize_once, initialize_shim);
  if (real_bind == NULL) {
    errno = ENOSYS;
    return -1;
  }
  if (address == NULL || address->sa_family != AF_INET ||
      address_length < sizeof(struct sockaddr_in))
    return real_bind(socket, address, address_length);

  struct sockaddr_in actual = *(const struct sockaddr_in *)address;
  for (size_t index = 0; index < interface_rule_count; ++index) {
    if (interface_rules[index].local &&
        actual.sin_addr.s_addr ==
        interface_rules[index].advertised.s_addr) {
      actual.sin_addr = interface_rules[index].observed;
      break;
    }
  }
  return real_bind(socket, (const struct sockaddr *)&actual, address_length);
}

int connect(int socket, const struct sockaddr *address,
            socklen_t address_length) {
  pthread_once(&initialize_once, initialize_shim);
  if (real_connect == NULL) {
    errno = ENOSYS;
    return -1;
  }
  if (address == NULL || address->sa_family != AF_INET ||
      address_length < sizeof(struct sockaddr_in))
    return real_connect(socket, address, address_length);

  struct sockaddr_in actual = *(const struct sockaddr_in *)address;
  for (size_t index = 0; index < interface_rule_count; ++index) {
    if (interface_rules[index].local &&
        (actual.sin_addr.s_addr ==
             interface_rules[index].advertised.s_addr ||
         actual.sin_addr.s_addr == interface_rules[index].observed.s_addr)) {
      if (local_connect_source_configured && real_getsockname != NULL) {
        struct sockaddr_in current;
        socklen_t current_length = sizeof(current);
        memset(&current, 0, sizeof(current));
        if (real_getsockname(socket, (struct sockaddr *)&current,
                             &current_length) == 0 &&
            current_length >= sizeof(current) && current.sin_family == AF_INET &&
            current.sin_port == 0 && current.sin_addr.s_addr == INADDR_ANY) {
          struct sockaddr_in source;
          memset(&source, 0, sizeof(source));
          source.sin_family = AF_INET;
          source.sin_addr = local_connect_source;
          if (real_bind(socket, (const struct sockaddr *)&source,
                        sizeof(source)) != 0)
            return -1;
        }
      }
      if (actual.sin_addr.s_addr ==
          interface_rules[index].advertised.s_addr)
        actual.sin_addr = interface_rules[index].observed;
      break;
    }
  }
  return real_connect(socket, (const struct sockaddr *)&actual, address_length);
}

int getpeername(int socket, struct sockaddr *address,
                socklen_t *address_length) {
  pthread_once(&initialize_once, initialize_shim);
  if (real_getpeername == NULL) {
    errno = ENOSYS;
    return -1;
  }

  int result = real_getpeername(socket, address, address_length);
  if (result != 0 || address == NULL || address_length == NULL ||
      *address_length < sizeof(struct sockaddr_in) ||
      address->sa_family != AF_INET)
    return result;

  struct sockaddr_in *ipv4 = (struct sockaddr_in *)address;
  for (size_t index = 0; index < rule_count; ++index) {
    if (ipv4->sin_addr.s_addr == rules[index].observed.s_addr) {
      ipv4->sin_addr = rules[index].advertised;
      break;
    }
  }
  return result;
}
