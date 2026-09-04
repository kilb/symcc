#!/usr/bin/env python3
# RUN: python3 %s

import importlib.util
import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parent.parent / "util" / "ucsan_seed.py"
SPEC = importlib.util.spec_from_file_location("ucsan_seed", MODULE_PATH)
assert SPEC and SPEC.loader
ucsan_seed = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ucsan_seed
SPEC.loader.exec_module(ucsan_seed)


class UCSanSeedTests(unittest.TestCase):
    def test_round_trip_preserves_root_object_and_negative_bound(self):
        entries = [
            ucsan_seed.SeedEntry(ucsan_seed.ROOT_ENTRY, 0, 0, (0,), b"\x01\x00"),
            ucsan_seed.SeedEntry(
                ucsan_seed.OBJECT_ENTRY,
                17,
                -4,
                (0, ucsan_seed.POINTEE, 8),
                b"node",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.ucsan"
            ucsan_seed.write_seed(path, entries)
            self.assertEqual(ucsan_seed.read_seed(path), entries)

    def test_object_path_uses_pointee_marker(self):
        path, lower = ucsan_seed.parse_object_path("3/8/-4@-16")
        self.assertEqual(path, (3, ucsan_seed.POINTEE, 8, -4))
        self.assertEqual(lower, -16)

    def test_alias_encodes_shared_and_cyclic_object_graph(self):
        path, lower, object_id = ucsan_seed.parse_alias("0/8@-4=17")
        self.assertEqual(path, (0, ucsan_seed.POINTEE, 8))
        self.assertEqual(lower, -4)
        self.assertEqual(object_id, 17)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "graph.ucsan"
            args = argparse.Namespace(
                output=output,
                root=[],
                object=["0=01020304"],
                alias=["1=1", "0/8=1"],
            )
            ucsan_seed.create(args)
            entries = ucsan_seed.read_seed(output)
            self.assertEqual(entries[0].object_id, 1)
            self.assertEqual(entries[1].object_id, 1)
            self.assertEqual(entries[2].object_id, 1)
            self.assertEqual(entries[1].data, b"")
            self.assertEqual(entries[2].path, (0, ucsan_seed.POINTEE, 8))

    def test_alias_rejects_zero_object_id(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            ucsan_seed.parse_alias("0=0")

    def test_canonicalize_preserves_aliases_with_deterministic_ids(self):
        root_path = (0, ucsan_seed.POINTEE)
        child_path = (0, ucsan_seed.POINTEE, 8)
        entries = [
            ucsan_seed.SeedEntry(ucsan_seed.OBJECT_ENTRY, 91, 0, child_path, b""),
            ucsan_seed.SeedEntry(ucsan_seed.OBJECT_ENTRY, 91, -4, root_path, b"object"),
        ]
        normalized = ucsan_seed.canonicalize(entries)
        objects = [
            entry for entry in normalized if entry.flags & ucsan_seed.OBJECT_ENTRY
        ]
        self.assertEqual({entry.object_id for entry in objects}, {1})
        self.assertEqual({entry.path for entry in objects}, {root_path, child_path})
        self.assertEqual(sum(bool(entry.data) for entry in objects), 1)

    def test_learns_majority_alias_cycle_and_drops_rare_paths(self):
        root_path = (0, ucsan_seed.POINTEE)
        cycle_path = (0, ucsan_seed.POINTEE, 8)
        other_path = (1, ucsan_seed.POINTEE)
        rare_path = (2, ucsan_seed.POINTEE)

        def graph(root_object, cycle_object, payload, include_rare=False):
            entries = [
                ucsan_seed.SeedEntry(ucsan_seed.ROOT_ENTRY, 0, 0, (0,), b"\x01"),
                ucsan_seed.SeedEntry(
                    ucsan_seed.OBJECT_ENTRY, root_object, -4, root_path, payload
                ),
                ucsan_seed.SeedEntry(
                    ucsan_seed.OBJECT_ENTRY, cycle_object, 0, cycle_path, b""
                ),
                ucsan_seed.SeedEntry(
                    ucsan_seed.OBJECT_ENTRY, root_object + 100, 0, other_path, b"other"
                ),
            ]
            if include_rare:
                entries.append(
                    ucsan_seed.SeedEntry(
                        ucsan_seed.OBJECT_ENTRY, 999, 0, rare_path, b"rare"
                    )
                )
            return entries

        learned = ucsan_seed.learn_object_graph(
            [
                graph(7, 7, b"node"),
                graph(31, 31, b"node"),
                graph(50, 51, b"outlier", include_rare=True),
            ],
            min_support=2,
        )
        by_path = {
            entry.path: entry
            for entry in learned
            if entry.flags & ucsan_seed.OBJECT_ENTRY
        }
        self.assertEqual(
            by_path[root_path].object_id,
            by_path[cycle_path].object_id,
        )
        self.assertNotEqual(
            by_path[root_path].object_id,
            by_path[other_path].object_id,
        )
        self.assertNotIn(rare_path, by_path)
        payloads = [
            entry.data
            for entry in learned
            if entry.object_id == by_path[root_path].object_id and entry.data
        ]
        self.assertEqual(payloads, [b"node"])

    def test_rejects_duplicate_path_and_object_payload(self):
        path = (0, ucsan_seed.POINTEE)
        duplicate_path = [
            ucsan_seed.SeedEntry(ucsan_seed.OBJECT_ENTRY, 1, 0, path, b"one"),
            ucsan_seed.SeedEntry(ucsan_seed.OBJECT_ENTRY, 2, 0, path, b"two"),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate object path"):
            ucsan_seed.serialize_seed(duplicate_path)

        duplicate_payload = [
            ucsan_seed.SeedEntry(ucsan_seed.OBJECT_ENTRY, 7, 0, path, b"one"),
            ucsan_seed.SeedEntry(
                ucsan_seed.OBJECT_ENTRY,
                7,
                0,
                (1, ucsan_seed.POINTEE),
                b"two",
            ),
        ]
        with self.assertRaisesRegex(ValueError, "more than one payload"):
            ucsan_seed.serialize_seed(duplicate_payload)

    def test_rejects_invalid_shape_bounds_and_combined_kind(self):
        invalid = [
            ucsan_seed.SeedEntry(
                ucsan_seed.ROOT_ENTRY | ucsan_seed.OBJECT_ENTRY,
                1,
                0,
                (1,),
                b"",
            )
        ]
        with self.assertRaisesRegex(ValueError, "combined kind"):
            ucsan_seed.serialize_seed(invalid)
        with self.assertRaisesRegex(ValueError, "upper bound"):
            ucsan_seed.serialize_seed(
                [
                    ucsan_seed.SeedEntry(
                        ucsan_seed.OBJECT_ENTRY,
                        1,
                        ucsan_seed.I64_MAX,
                        (0, ucsan_seed.POINTEE),
                        b"x",
                    )
                ]
            )

    def test_canonical_payload_path_and_verification_report(self):
        entries = [
            ucsan_seed.SeedEntry(
                ucsan_seed.OBJECT_ENTRY,
                99,
                -4,
                (2, ucsan_seed.POINTEE, 8),
                b"payload",
            ),
            ucsan_seed.SeedEntry(
                ucsan_seed.OBJECT_ENTRY,
                99,
                0,
                (1, ucsan_seed.POINTEE),
                b"",
            ),
        ]
        canonical = ucsan_seed.canonicalize(entries)
        self.assertEqual(canonical[0].path, (1, ucsan_seed.POINTEE))
        self.assertEqual(canonical[0].data, b"payload")
        self.assertEqual(canonical, ucsan_seed.canonicalize(canonical))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.ucsan"
            ucsan_seed.write_seed(path, canonical)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                ucsan_seed.verify(argparse.Namespace(seed=path, require_canonical=True))
            report = json.loads(output.getvalue())
            self.assertTrue(report["canonical"])
            self.assertEqual(report["objects"], 1)
            self.assertEqual(report["paths"], 2)
            self.assertEqual(report["materialized_object_bytes"], 7)

    def test_read_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            target = directory_path / "target.ucsan"
            link = directory_path / "link.ucsan"
            ucsan_seed.write_seed(
                target,
                [ucsan_seed.SeedEntry(ucsan_seed.ROOT_ENTRY, 0, 0, (0,), b"root")],
            )
            link.symlink_to(target)
            with self.assertRaises(OSError):
                ucsan_seed.read_seed(link)


if __name__ == "__main__":
    unittest.main()
