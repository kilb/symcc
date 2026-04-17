#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#define PCRE2_CODE_UNIT_WIDTH 8
#include "pcre2.h"

int main(int argc, char **argv) {
    if (argc < 2) return 1;
    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) return 1;
    char buf[4096];
    ssize_t len = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (len < 1) return 0;
    buf[len] = '\0';

    int errcode;
    PCRE2_SIZE erroffset;
    pcre2_code *re = pcre2_compile((PCRE2_SPTR)buf, len, 0, &errcode, &erroffset, NULL);
    if (re) {
        pcre2_match_data *match = pcre2_match_data_create_from_pattern(re, NULL);
        pcre2_match(re, (PCRE2_SPTR)buf, len, 0, 0, match, NULL);
        pcre2_match_data_free(match);
        pcre2_code_free(re);
    }
    return 0;
}
