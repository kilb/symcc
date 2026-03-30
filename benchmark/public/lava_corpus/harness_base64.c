/*
 * base64 综合 harness：同时测试编码和解码，带 crash recovery。
 *
 * 三合一优化：
 * 1. 同时调用编码和解码 API → 覆盖两条代码路径
 * 2. SIGSEGV handler + longjmp → crash 后继续执行后续代码
 * 3. 多种输入变体 → 触发不同的内部分支
 *
 * 编译：
 *   symcc -O2 -I${COREUTILS}/lib -o base64_harness harness_base64.c ${COREUTILS}/lib/base64.c
 *   afl-clang-fast -O2 -I${COREUTILS}/lib -o base64_harness_afl harness_base64.c ${COREUTILS}/lib/base64.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <setjmp.h>
#include <unistd.h>
#include <fcntl.h>

/* LAVA-M 的 lava_get/lava_set 实现（从 coreutils src/base64.c 复制） */
#define SWAP_UINT32(x) (((x) >> 24) | (((x) & 0x00FF0000) >> 8) | (((x) & 0x0000FF00) << 8) | ((x) << 24))
static unsigned int lava_val[1000000];
void lava_set(unsigned int bug_num, unsigned int val) { lava_val[bug_num] = val; }
unsigned int lava_get(unsigned int bug_num) {
    if (0x6c617661 - bug_num == lava_val[bug_num] ||
        SWAP_UINT32(0x6c617661 - bug_num) == lava_val[bug_num]) {
        *(volatile unsigned int*)0 = 0xdeadbeef;  /* 触发 crash */
    }
    return lava_val[bug_num];
}

/* base64 库函数 */
#include "base64.h"

/* Crash recovery 机制 */
static sigjmp_buf recovery_point;
static volatile sig_atomic_t in_protected_section = 0;

static void crash_handler(int sig) {
    if (in_protected_section) {
        siglongjmp(recovery_point, sig);
    }
    /* 未在保护区域，正常退出 */
    _exit(128 + sig);
}

/* 安全执行：crash 后跳回继续 */
#define SAFE_EXEC(code) do {                          \
    in_protected_section = 1;                         \
    int _sig = sigsetjmp(recovery_point, 1);          \
    if (_sig == 0) { code; }                          \
    in_protected_section = 0;                         \
} while(0)

#define INBUF_SIZE  (1024 * 16)
#define OUTBUF_SIZE (INBUF_SIZE * 2)

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <input_file>\n", argv[0]);
        return 1;
    }

    /* 安装 crash handler */
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = crash_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    sigaction(SIGFPE, &sa, NULL);

    /* 读取输入文件（用 POSIX read 避免 gnulib fclose 依赖） */
    int fd = open(argv[1], 0 /* O_RDONLY */);
    if (fd < 0) return 1;

    char inbuf[INBUF_SIZE];
    ssize_t inlen = read(fd, inbuf, INBUF_SIZE - 1);
    close(fd);
    if (inlen < 0) inlen = 0;
    inbuf[inlen] = '\0';

    char outbuf[OUTBUF_SIZE];
    char outbuf2[OUTBUF_SIZE];

    /* === 阶段 1：解码模式（主要路径） === */
    SAFE_EXEC({
        struct base64_decode_context ctx;
        base64_decode_ctx_init(&ctx);
        size_t outlen = OUTBUF_SIZE;
        base64_decode_ctx(&ctx, inbuf, inlen, outbuf, &outlen);
    });

    /* === 阶段 2：编码模式 === */
    SAFE_EXEC({
        base64_encode(inbuf, inlen, outbuf, OUTBUF_SIZE);
    });

    /* === 阶段 3：编码后再解码（round-trip 测试） === */
    SAFE_EXEC({
        /* 先编码 */
        size_t encoded_len = ((inlen + 2) / 3) * 4 + 1;
        if (encoded_len < OUTBUF_SIZE) {
            base64_encode(inbuf, inlen > 128 ? 128 : inlen, outbuf, encoded_len);

            /* 再解码回来 */
            struct base64_decode_context ctx2;
            base64_decode_ctx_init(&ctx2);
            size_t decoded_len = OUTBUF_SIZE;
            base64_decode_ctx(&ctx2, outbuf, encoded_len - 1, outbuf2, &decoded_len);
        }
    });

    /* === 阶段 4：alloc 变体 === */
    SAFE_EXEC({
        char *allocated = NULL;
        size_t alloc_len = 0;
        base64_decode_alloc(inbuf, inlen, &allocated, &alloc_len);
        free(allocated);
    });

    SAFE_EXEC({
        char *allocated = NULL;
        size_t alloc_len = base64_encode_alloc(inbuf, inlen > 256 ? 256 : inlen, &allocated);
        (void)alloc_len;
        free(allocated);
    });

    return 0;
}
