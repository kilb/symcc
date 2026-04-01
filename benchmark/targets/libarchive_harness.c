#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include "archive.h"
#include "archive_entry.h"

int main(int argc, char **argv) {
    if (argc < 2) return 1;

    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) return 1;
    char buf[65536];
    ssize_t len = read(fd, buf, sizeof(buf));
    close(fd);
    if (len <= 0) return 0;

    struct archive *a = archive_read_new();
    archive_read_support_filter_all(a);
    archive_read_support_format_all(a);

    if (archive_read_open_memory(a, buf, len) == ARCHIVE_OK) {
        struct archive_entry *entry;
        while (archive_read_next_header(a, &entry) == ARCHIVE_OK) {
            /* 读取条目数据以触发更深的解码路径 */
            char data_buf[4096];
            while (archive_read_data(a, data_buf, sizeof(data_buf)) > 0)
                ;
        }
    }
    archive_read_free(a);
    return 0;
}
