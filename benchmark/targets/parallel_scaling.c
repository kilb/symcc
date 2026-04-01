/*
 * 并行 Scaling 专用 Benchmark
 *
 * 设计：通过 switch 语句显式分派到不同模块。
 * SymCC 的约束求解可以翻转 switch 条件，生成进入其他 case 的输入。
 * 每个 case 内有独立的 magic number 检查 + 多层嵌套分支。
 *
 * 单次 SymCC 执行从种子进入 1 个 case，求解生成进入其他 case 的变体。
 * N 个 worker 并行处理这些变体，覆盖率 ∝ 已探索的 case 数。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>

static volatile int sink = 0;

/* 每个模块有独立的 magic + 多层分支 */
#define MODULE(id, magic_val)                                        \
    case id:                                                         \
        if (len > 5 && *(uint32_t*)(buf+2) == (uint32_t)(magic_val)) { \
            sink += id * 100;                                        \
            if (len > 6 && buf[6] > 128) {                          \
                sink += id * 10;                                     \
                if (len > 7 && buf[7] == (uint8_t)(id ^ 0xAA)) {    \
                    sink += id;                                      \
                }                                                    \
            } else if (len > 6) {                                    \
                sink -= id * 10;                                     \
                if (len > 7 && buf[7] == (uint8_t)(~id & 0xFF)) {   \
                    sink -= id;                                      \
                }                                                    \
            }                                                        \
        }                                                            \
        break;

int main(int argc, char **argv) {
    if (argc < 2) return 1;

    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) return 1;
    uint8_t buf[256];
    ssize_t len = read(fd, buf, sizeof(buf));
    close(fd);
    if (len < 2) return 0;

    /* 第 1 字节 = 模块 selector（显式 switch，SymCC 可以求解） */
    switch (buf[0]) {
        MODULE(0,   0xDEADBEEF)
        MODULE(1,   0xCAFEBABE)
        MODULE(2,   0x8BADF00D)
        MODULE(3,   0xFEEDFACE)
        MODULE(4,   0x0D15EA5E)
        MODULE(5,   0xC0FFEE00)
        MODULE(6,   0xBAAAAAAD)
        MODULE(7,   0x1BADB002)
        MODULE(8,   0xABADCAFE)
        MODULE(9,   0xDEFEC8ED)
        MODULE(10,  0xFACEFEED)
        MODULE(11,  0xD15EA5ED)
        MODULE(12,  0xDABBAD00)
        MODULE(13,  0xDEADC0DE)
        MODULE(14,  0xBADDCAFE)
        MODULE(15,  0x8BADF00E)
        MODULE(16,  0xCAFED00D)
        MODULE(17,  0xFEE1DEAD)
        MODULE(18,  0xDEADFA11)
        MODULE(19,  0xFACEB00C)
        MODULE(20,  0xB16B00B5)
        MODULE(21,  0x0B00B135)
        MODULE(22,  0xBAADF00D)
        MODULE(23,  0xDEADBEAD)
        MODULE(24,  0xC0DED00D)
        MODULE(25,  0xCAFEF00D)
        MODULE(26,  0xBEEFCACE)
        MODULE(27,  0xFEEDC0DE)
        MODULE(28,  0xDEAD10CC)
        MODULE(29,  0xFACECAFE)
        MODULE(30,  0xBADC0FFE)
        MODULE(31,  0x0DEFACED)
        MODULE(32,  0xD0D0CACA)
        MODULE(33,  0xCAFEBEEF)
        MODULE(34,  0xFEEDF00D)
        MODULE(35,  0xBAADFACE)
        MODULE(36,  0xDECAFBAD)
        MODULE(37,  0xC0CAC01A)
        MODULE(38,  0xABCDEF01)
        MODULE(39,  0x12345678)
        MODULE(40,  0x9ABCDEF0)
        MODULE(41,  0xFEDCBA98)
        MODULE(42,  0x76543210)
        MODULE(43,  0x01234567)
        MODULE(44,  0x89ABCDEF)
        MODULE(45,  0xDEADDEAD)
        MODULE(46,  0xBEEFBEEF)
        MODULE(47,  0xCAFECAFE)
        MODULE(48,  0xF00DF00D)
        MODULE(49,  0xBABEBABE)
        MODULE(50,  0xFACEFACE)
        MODULE(51,  0xC0DEC0DE)
        MODULE(52,  0xFEEDFEED)
        MODULE(53,  0xD00DD00D)
        MODULE(54,  0xB00BB00B)
        MODULE(55,  0xACEDACED)
        MODULE(56,  0xDADADADA)
        MODULE(57,  0xABBABAAB)
        MODULE(58,  0xEFFEC7ED)
        MODULE(59,  0xC0DEDBAD)
        MODULE(60,  0xFADEDBAD)
        MODULE(61,  0xADDEDBEE)
        MODULE(62,  0xB0BACAFE)
        MODULE(63,  0xFEEBDAED)
        default:
            break;
    }

    return 0;
}
