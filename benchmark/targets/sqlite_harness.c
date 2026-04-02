/* SQLite file-based harness for SymCC/AFL */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include "sqlite3.h"

static int exec_callback(void *cnt, int argc, char **argv, char **names) {
    int *c = (int*)cnt;
    if ((*c)-- <= 0) return 1;
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) return 1;

    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) return 1;
    char buf[8192];
    ssize_t len = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (len < 3) return 0;
    buf[len] = '\0';

    sqlite3 *db;
    if (sqlite3_open(":memory:", &db) != SQLITE_OK) return 0;

    /* 限制执行时间 */
    sqlite3_progress_handler(db, 1000, NULL, NULL);

    int cnt = 100;
    char *err = NULL;
    sqlite3_exec(db, (const char*)buf, exec_callback, &cnt, &err);
    sqlite3_free(err);
    sqlite3_close(db);
    return 0;
}
