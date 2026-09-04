#!/usr/bin/env python3
# RUN: python3 %s

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "util"))

try:
    from mpi_fuzzing_helper import _read_tace_density  # noqa: E402
except ModuleNotFoundError:
    _read_tace_density = None


@unittest.skipIf(_read_tace_density is None, "mpi4py is unavailable")
class TaceProfileTests(unittest.TestCase):
    def test_density_parser_is_bounded_deduplicated_and_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "density"
            path.write_text(
                "# offset density\n8 2\n2 1\n8 4\n3 0\n-1 9\n100 1\nbad\n"
            )
            self.assertEqual(_read_tace_density(str(path), 16), [2, 8])

    def test_missing_density_falls_back_to_full_symbolization(self):
        self.assertEqual(_read_tace_density("/missing", 10), [])


if __name__ == "__main__":
    unittest.main()
