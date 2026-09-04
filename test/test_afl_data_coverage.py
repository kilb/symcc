# RUN: python3 %s

import ctypes
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAP_SIZE = 65536


class AflDataCoverageRuntimeTests(unittest.TestCase):
    def _build_runtime(self, tmp_path: Path) -> tuple[str, Path]:
        cc = shutil.which("cc") or shutil.which("clang")
        if not cc:
            self.skipTest("no C compiler available")
        so_path = tmp_path / "libafl_data_coverage_rt.so"
        subprocess.run(
            [
                cc, "-O2", "-shared", "-fPIC",
                str(ROOT / "util" / "afl_data_coverage_rt.c"),
                "-o", str(so_path), "-ldl", "-pthread",
            ],
            check=True,
        )
        return cc, so_path

    def test_concurrent_first_use_is_deadlock_free_and_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cc, so_path = self._build_runtime(tmp_path)
            target_path = tmp_path / "thread-target"
            target_src = tmp_path / "thread-target.c"
            target_src.write_text(
                r'''
#include <dlfcn.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define THREADS 32
unsigned char map[65536];
unsigned char *__afl_area_ptr = map;
unsigned char *__symcc_afl_area_ptr = map;
static pthread_barrier_t start_barrier;
static int (*comparison)(const void *, const void *, size_t);

static void *run(void *unused) {
  (void)unused;
  if (pthread_barrier_wait(&start_barrier) == PTHREAD_BARRIER_SERIAL_THREAD) {}
  if (comparison("ABCD", "ABCD", 4) != 0)
    abort();
  return NULL;
}

int main(int argc, char **argv) {
  if (argc != 2) return 2;
  pthread_t threads[THREADS];
  if (pthread_barrier_init(&start_barrier, NULL, THREADS + 1) != 0) return 3;
  for (unsigned i = 0; i < THREADS; ++i)
    if (pthread_create(&threads[i], NULL, run, NULL) != 0) return 4;
  void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
  if (!library) return 5;
  comparison = (int (*)(const void *, const void *, size_t))
      dlsym(library, "memcmp");
  if (!comparison) return 6;
  if (pthread_barrier_wait(&start_barrier) == PTHREAD_BARRIER_SERIAL_THREAD) {}
  for (unsigned i = 0; i < THREADS; ++i)
    if (pthread_join(threads[i], NULL) != 0) return 7;
  unsigned total = 0;
  for (unsigned i = 0; i < sizeof(map); ++i)
    total += map[i];
  printf("%u\n", total);
  return 0;
}
''',
                encoding="ascii",
            )
            subprocess.run(
                [
                    cc, "-O2", "-rdynamic", "-Wl,-E",
                    str(target_src), "-o", str(target_path),
                    "-ldl", "-pthread",
                ],
                check=True,
            )
            env = os.environ.copy()
            env["SYMCC_AFL_DATA_COVERAGE"] = "1"
            # A full four-byte match emits five prefix counters per thread.
            for _ in range(10):
                output = subprocess.check_output(
                    [str(target_path), str(so_path)],
                    env=env,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(int(output.strip()), 32 * 5)

    def test_preload_writes_prefix_progress_to_exported_afl_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cc, so_path = self._build_runtime(tmp_path)
            target_path = tmp_path / "target"
            target_src = tmp_path / "target.c"
            target_src.write_text(
                r'''
#include <stdio.h>
#include <string.h>
unsigned char map[65536];
unsigned char *__afl_area_ptr = map;
unsigned char *__symcc_afl_area_ptr = map;
int main(int argc, char **argv) {
  if (argc < 2) return 2;
  int (*volatile cmpfn)(const void *, const void *, size_t) = memcmp;
  (void)cmpfn(argv[1], "ABCD", 4);
  unsigned count = 0;
  for (unsigned i = 0; i < sizeof(map); ++i)
    if (map[i]) count++;
  printf("%u\n", count);
  return 0;
}
''',
                encoding="utf-8",
            )
            subprocess.run(
                [
                    cc, "-O0", "-fno-builtin", "-rdynamic", "-Wl,-E",
                    str(target_src), "-o", str(target_path),
                ],
                check=True,
            )
            env = os.environ.copy()
            env["LD_PRELOAD"] = str(so_path)
            env["SYMCC_AFL_DATA_COVERAGE"] = "1"
            partial = int(subprocess.check_output(
                [str(target_path), "ABxx"], env=env, text=True).strip())
            full = int(subprocess.check_output(
                [str(target_path), "ABCD"], env=env, text=True).strip())
            self.assertGreater(partial, 0)
            self.assertGreater(full, partial)

    def test_preload_can_attach_afl_shm_without_exported_symbol(self):
        libc = ctypes.CDLL(None, use_errno=True)
        shmget = libc.shmget
        shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        shmget.restype = ctypes.c_int
        shmat = libc.shmat
        shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        shmat.restype = ctypes.c_void_p
        shmdt = libc.shmdt
        shmdt.argtypes = [ctypes.c_void_p]
        shmctl = libc.shmctl
        shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

        shm_id = shmget(0, MAP_SIZE, 0o1000 | 0o600)
        if shm_id < 0:
            self.skipTest("SysV shmget unavailable")
        addr = shmat(shm_id, None, 0)
        if addr == ctypes.c_void_p(-1).value:
            shmctl(shm_id, 0, None)
            self.skipTest("SysV shmat unavailable")

        try:
            shm_buf = (ctypes.c_ubyte * MAP_SIZE).from_address(addr)
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                cc, so_path = self._build_runtime(tmp_path)
                target_path = tmp_path / "target"
                target_src = tmp_path / "target.c"
                target_src.write_text(
                    r'''
#include <string.h>
int main(int argc, char **argv) {
  if (argc < 2) return 2;
  int (*volatile cmpfn)(const void *, const void *, size_t) = memcmp;
  return cmpfn(argv[1], "ABCD", 4) == 0 ? 0 : 1;
}
''',
                    encoding="utf-8",
                )
                subprocess.run(
                    [
                        cc, "-O0", "-fno-builtin",
                        str(target_src), "-o", str(target_path),
                    ],
                    check=True,
                )
                env = os.environ.copy()
                env["LD_PRELOAD"] = str(so_path)
                env["__AFL_SHM_ID"] = str(shm_id)
                env["SYMCC_AFL_DATA_COVERAGE"] = "1"
                ctypes.memset(addr, 0, MAP_SIZE)
                subprocess.run([str(target_path), "ABxx"], env=env, check=False)
                partial = sum(1 for byte in shm_buf if byte)
                ctypes.memset(addr, 0, MAP_SIZE)
                subprocess.run([str(target_path), "ABCD"], env=env, check=True)
                full = sum(1 for byte in shm_buf if byte)
                self.assertGreater(partial, 0)
                self.assertGreater(full, partial)
        finally:
            shmdt(ctypes.c_void_p(addr))
            shmctl(shm_id, 0, None)

    def test_pie_site_identity_is_stable_across_aslr_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cc, so_path = self._build_runtime(tmp_path)
            target_path = tmp_path / "pie-target"
            target_src = tmp_path / "pie-target.c"
            target_src.write_text(
                r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
unsigned char map[65536];
unsigned char *__afl_area_ptr = map;
unsigned char *__symcc_afl_area_ptr = map;
int main(int argc, char **argv) {
  if (argc < 2) return 2;
  int (*volatile cmpfn)(const void *, const void *, size_t) = memcmp;
  (void)cmpfn(argv[1], "ABCD", 4);
  uint64_t digest = 1469598103934665603ULL;
  for (unsigned i = 0; i < sizeof(map); ++i) {
    if (!map[i]) continue;
    digest ^= i;
    digest *= 1099511628211ULL;
    digest ^= map[i];
    digest *= 1099511628211ULL;
  }
  printf("%016llx\n", (unsigned long long)digest);
  return 0;
}
''',
                encoding="utf-8",
            )
            subprocess.run(
                [
                    cc, "-O0", "-fno-builtin", "-rdynamic", "-Wl,-E",
                    "-fPIE", "-pie", str(target_src), "-o", str(target_path),
                ],
                check=True,
            )
            env = os.environ.copy()
            env["LD_PRELOAD"] = str(so_path)
            env["SYMCC_AFL_DATA_COVERAGE"] = "1"
            fingerprints = {
                subprocess.check_output(
                    [str(target_path), "ABxx"], env=env, text=True).strip()
                for _ in range(8)
            }
            self.assertEqual(len(fingerprints), 1)

    def test_afl_showmap_streaming_observes_reserved_data_namespace(self):
        afl_cc = shutil.which("afl-clang-fast")
        showmap = shutil.which("afl-showmap")
        if not afl_cc or not showmap:
            self.skipTest("AFL++ compiler and afl-showmap are required")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, so_path = self._build_runtime(tmp_path)
            target_path = tmp_path / "afl-target"
            target_src = tmp_path / "afl-target.c"
            target_src.write_text(
                r'''
#include <stdio.h>
#include <string.h>
static volatile int sink;
int main(int argc, char **argv) {
  if (argc < 2) return 2;
  FILE *stream = fopen(argv[1], "rb");
  if (!stream) return 3;
  unsigned char byte = 0;
  (void)fread(&byte, 1, 1, stream);
  fclose(stream);
  int (*volatile cmpfn)(const void *, const void *, size_t) = memcmp;
  sink = cmpfn(&byte, "B", 1);
  if (byte == 'B') sink++;
  return 0;
}
''',
                encoding="utf-8",
            )
            subprocess.run(
                [
                    afl_cc, "-O0", "-fno-builtin",
                    str(target_src), "-o", str(target_path),
                ],
                check=True,
            )

            def observe(preload: bool) -> tuple[tuple[int, int], ...]:
                env = os.environ.copy()
                env["AFL_QUIET"] = "1"
                env["AFL_PRELOAD"] = str(so_path)
                if preload:
                    env["SYMCC_AFL_DATA_COVERAGE"] = "1"
                    env["AFL_DATA_COVERAGE"] = "1"
                else:
                    env.pop("LD_PRELOAD", None)
                    env["SYMCC_AFL_DATA_COVERAGE"] = "0"
                    env["AFL_DATA_COVERAGE"] = "0"
                process = subprocess.Popen(
                    [
                        showmap, "-S", "-e", "-q", "--",
                        str(target_path), "@@",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                try:
                    assert process.stdin is not None
                    assert process.stdout is not None
                    process.stdin.write(struct.pack("<I", 1) + b"B")
                    process.stdin.flush()
                    status = struct.unpack("<H", process.stdout.read(2))[0]
                    count = struct.unpack("<I", process.stdout.read(4))[0]
                    raw = process.stdout.read(5 * count)
                    for _ in range(2):
                        length = struct.unpack(
                            "<I", process.stdout.read(4))[0]
                        process.stdout.read(length)
                    self.assertEqual(status & 0x3, 0)
                    return tuple(struct.iter_unpack("<IB", raw))
                finally:
                    process.terminate()
                    process.communicate(timeout=5)

            edge = observe(False)
            combined = observe(True)
            self.assertEqual(combined, observe(True))
            self.assertTrue(set(edge).issubset(set(combined)))
            self.assertGreater(len(combined), len(edge))
            self.assertTrue(any(identifier < MAP_SIZE
                                for identifier, _ in combined
                                if (identifier, 1) not in set(edge)))


if __name__ == "__main__":
    unittest.main()
