#!/usr/bin/env python3
# RUN: python3 %s

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
QA3 = ROOT / "benchmark" / "qa3_repro"
sys.path.insert(0, str(QA3))

import qa3_common  # noqa: E402

sys.path.insert(0, str(ROOT / "benchmark"))
import run_qa3_coverage_campaign as campaign_module  # noqa: E402


class QA3CommonTests(unittest.TestCase):
    def test_parse_edge_map_accepts_afl_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "map"
            path.write_text("000001:1\n65535:255\n", encoding="ascii")
            self.assertEqual(
                qa3_common.parse_edge_map(path, map_size=65_536),
                frozenset({1, 65_535}),
            )

    def test_parse_edge_map_rejects_untrustworthy_rows(self):
        cases = (
            "",
            "not-a-row\n",
            "1:0\n",
            "1:256\n",
            "1:1\n1:2\n",
            "65536:1\n",
            "1:1:2\n",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "map"
            for content in cases:
                with self.subTest(content=content):
                    path.write_text(content, encoding="ascii")
                    with self.assertRaises(qa3_common.MeasurementError):
                        qa3_common.parse_edge_map(path, map_size=65_536)

    def test_measure_edges_preserves_all_afl_terminal_statuses(self):
        for returncode, status in qa3_common.SHOWMAP_RETURN_STATUS.items():
            with self.subTest(
                returncode=returncode
            ), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                target = directory / "target"
                target.write_bytes(b"binary")
                input_path = directory / "input"
                input_path.write_bytes(b"payload")
                emitted: list[Path] = []

                def fake_run(command, **_kwargs):
                    map_path = Path(command[command.index("-o") + 1])
                    emitted.append(map_path)
                    map_path.write_text("000007:1\n", encoding="ascii")
                    return SimpleNamespace(returncode=returncode, stderr="")

                with mock.patch.object(
                    qa3_common.subprocess, "run", side_effect=fake_run
                ):
                    measurement = qa3_common.measure_edges(
                        target, input_path, input_mode="file"
                    )
                self.assertEqual(measurement.edges, frozenset({7}))
                self.assertEqual(measurement.status, status)
                self.assertFalse(emitted[0].exists())

    def test_measure_edges_rejects_tool_failures_and_cleans_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "target"
            target.write_bytes(b"binary")
            input_path = directory / "input"
            input_path.write_bytes(b"payload")
            emitted: list[Path] = []

            def fake_run(command, **_kwargs):
                map_path = Path(command[command.index("-o") + 1])
                emitted.append(map_path)
                map_path.write_text("000007:1\n", encoding="ascii")
                return SimpleNamespace(
                    returncode=4, stderr="broken instrumentation"
                )

            with mock.patch.object(
                qa3_common.subprocess, "run", side_effect=fake_run
            ):
                with self.assertRaisesRegex(
                    qa3_common.MeasurementError, "broken instrumentation"
                ):
                    qa3_common.measure_edges(
                        target, input_path, input_mode="file"
                    )
            self.assertFalse(emitted[0].exists())

    def test_one_shot_and_streaming_resolve_the_same_input_abi(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "target"
            signature = b"##SIG_AFL_PERSISTENT##"
            target.write_bytes(b"x" * (1024 * 1024 - 8) + signature)
            input_path = directory / "input"
            input_path.write_bytes(b"exact-payload")
            commands = []

            def fake_run(command, **kwargs):
                commands.append((command, kwargs.get("input"), kwargs))
                map_path = Path(command[command.index("-o") + 1])
                map_path.write_text("7:1\n", encoding="ascii")
                return SimpleNamespace(returncode=0, stderr=b"")

            self.assertEqual(
                qa3_common.detect_afl_input_mode(target), "stdin"
            )
            with mock.patch.object(
                qa3_common.subprocess, "run", side_effect=fake_run
            ):
                qa3_common.measure_edges(target, input_path)
                qa3_common.measure_edges(target, input_path, input_mode="file")
            self.assertEqual(commands[0][0][-1], str(target.resolve()))
            self.assertEqual(
                commands[1][0][-2:], [str(target.resolve()), str(input_path.resolve())]
            )
            self.assertEqual(commands[0][1], b"exact-payload")
            self.assertIsNone(commands[1][1])
            self.assertIs(commands[1][2]["stdin"], subprocess.DEVNULL)

    def test_terminal_status_policy_rejects_or_stratifies(self):
        self.assertTrue(
            qa3_common.coverage_status_is_eligible(
                "ok", policy="normal-only", label="input"
            )
        )
        self.assertFalse(
            qa3_common.coverage_status_is_eligible(
                "crash", policy="stratified", label="input"
            )
        )
        with self.assertRaisesRegex(
            qa3_common.MeasurementError, "abnormal AFL terminal status"
        ):
            qa3_common.coverage_status_is_eligible(
                "timeout", policy="normal-only", label="input"
            )

    def test_input_reads_are_bounded_before_oracle_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input"
            path.write_bytes(b"12345")
            with mock.patch.object(qa3_common, "MAX_INPUT_BYTES", 4):
                with self.assertRaisesRegex(
                    qa3_common.MeasurementError, "exceeds 4 bytes"
                ):
                    qa3_common.read_bounded_input(path)

    def test_replicated_measurement_requires_stable_status(self):
        samples = [
            qa3_common.EdgeMeasurement(frozenset({1}), "ok", 0),
            qa3_common.EdgeMeasurement(frozenset({1}), "crash", 2),
        ]
        with mock.patch.object(
            qa3_common, "measure_edges", side_effect=samples
        ):
            with self.assertRaisesRegex(
                qa3_common.MeasurementError, "terminal status changed"
            ):
                qa3_common.measure_edges_repeated(
                    "target", "input", repeats=2
                )

    def test_replicated_measurement_has_explicit_stability_policies(self):
        samples = [
            qa3_common.EdgeMeasurement(frozenset({1, 2}), "ok", 0),
            qa3_common.EdgeMeasurement(frozenset({2, 3}), "ok", 0),
        ]
        with mock.patch.object(
            qa3_common, "measure_edges", side_effect=samples
        ):
            with self.assertRaisesRegex(
                qa3_common.MeasurementError, "unstable_edges=2"
            ):
                qa3_common.measure_edges_repeated(
                    "target", "input", repeats=2
                )

        for policy, expected in (
            ("intersection", frozenset({2})),
            ("union", frozenset({1, 2, 3})),
        ):
            with self.subTest(policy=policy), mock.patch.object(
                qa3_common, "measure_edges", side_effect=samples
            ):
                result = qa3_common.measure_edges_repeated(
                    "target",
                    "input",
                    repeats=2,
                    stability_policy=policy,
                )
                self.assertEqual(result.edges, expected)
                self.assertEqual(result.unstable_edges, 2)
                self.assertEqual(result.union_edges, 3)
                self.assertEqual(result.intersection_edges, 1)

    def test_streaming_oracle_reuses_one_persistent_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / "target"
            binary.write_bytes(b"prefix##SIG_AFL_PERSISTENT##suffix")
            input_path = directory / "input"
            input_path.write_bytes(b"data")
            fake = mock.Mock()
            fake.restart_count = 2
            fake.get_result.side_effect = [
                SimpleNamespace(
                    raw_status=0,
                    status="ok",
                    status_detail=0,
                    edges=((7, 1), (9, 2)),
                ),
                SimpleNamespace(
                    raw_status=0,
                    status="ok",
                    status_detail=0,
                    edges=((7, 3), (9, 1)),
                ),
            ]
            isolated = qa3_common.EdgeMeasurement(
                frozenset({7, 9}), "ok", 0
            )
            with mock.patch.object(
                qa3_common, "StreamingShowmap", return_value=fake
            ) as constructor, mock.patch.object(
                qa3_common, "measure_edges", return_value=isolated
            ):
                with qa3_common.StreamingCoverageOracle(
                    binary, repeats=2, timeout_ms=17
                ) as oracle:
                    result = oracle.measure(input_path)
                    self.assertEqual(result.edges, frozenset({7, 9}))
                    self.assertEqual(oracle.restart_count, 2)
                    self.assertEqual(oracle.mode, "streaming")
                    self.assertEqual(oracle.fallbacks, 0)
                    self.assertEqual(oracle.probe_observations, 2)
                    self.assertEqual(oracle.probe["result"], "compatible")
            constructor.assert_called_once_with(
                "afl-showmap", [str(binary.resolve())], timeout_ms=17
            )
            self.assertEqual(fake.get_result.call_count, 2)
            fake.close.assert_called_once_with()

    def test_streaming_oracle_detects_temporal_edge_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / "target"
            binary.write_bytes(b"non-persistent")
            input_path = directory / "input"
            input_path.write_bytes(b"data")
            fake = mock.Mock()
            fake.restart_count = 0
            fake.get_result.side_effect = [
                SimpleNamespace(
                    raw_status=0,
                    status="ok",
                    status_detail=0,
                    edges=((1, 1), (2, 1)),
                ),
                SimpleNamespace(
                    raw_status=0,
                    status="ok",
                    status_detail=0,
                    edges=((2, 1), (3, 1)),
                ),
            ]
            isolated = qa3_common.EdgeMeasurement(
                frozenset({1, 2}), "ok", 0
            )
            with mock.patch.object(
                qa3_common, "StreamingShowmap", return_value=fake
            ) as constructor, mock.patch.object(
                qa3_common, "measure_edges", return_value=isolated
            ):
                oracle = qa3_common.StreamingCoverageOracle(
                    binary, repeats=2, input_mode="stdin"
                )
                try:
                    with self.assertRaisesRegex(
                        qa3_common.MeasurementError, "unstable_edges=2"
                    ):
                        oracle.measure(input_path)
                finally:
                    oracle.close()
            constructor.assert_called_once_with(
                "afl-showmap",
                [str(binary.resolve())],
                timeout_ms=5_000,
            )

    def test_streaming_oracle_falls_back_on_incompatible_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / "target"
            binary.write_bytes(b"##SIG_AFL_PERSISTENT##")
            input_path = directory / "input"
            input_path.write_bytes(b"data")
            fake = mock.Mock()
            fake.restart_count = 0
            fake.get_result.return_value = SimpleNamespace(
                raw_status=1,
                status="timeout",
                status_detail=0,
                edges=((1, 1),),
            )
            isolated = qa3_common.EdgeMeasurement(
                frozenset({1, 2}), "ok", 0
            )
            fallback = qa3_common.ReplicatedEdgeMeasurement(
                edges=frozenset({1, 2}),
                status="ok",
                replicas=3,
                unstable_edges=0,
                union_edges=2,
                intersection_edges=2,
            )
            with mock.patch.object(
                qa3_common, "StreamingShowmap", return_value=fake
            ), mock.patch.object(
                qa3_common, "measure_edges", return_value=isolated
            ), mock.patch.object(
                qa3_common, "measure_edges_repeated", return_value=fallback
            ) as repeated:
                with qa3_common.StreamingCoverageOracle(binary) as oracle:
                    self.assertEqual(oracle.measure(input_path), fallback)
                    self.assertEqual(oracle.mode, "one-shot")
                    self.assertEqual(oracle.fallbacks, 1)
                    self.assertEqual(oracle.probe_observations, 2)
                    self.assertEqual(
                        oracle.probe["result"],
                        "status-mismatch+edge-set-mismatch",
                    )
            repeated.assert_called_once()
            fake.close.assert_called_once_with()

    def test_corpus_replicas_are_interleaved_and_reconciled(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = directory / "first"
            second = directory / "second"
            first.write_bytes(b"a")
            second.write_bytes(b"b")

            def sample(edges):
                return qa3_common.ReplicatedEdgeMeasurement(
                    edges=frozenset(edges),
                    status="ok",
                    replicas=1,
                    unstable_edges=0,
                    union_edges=len(edges),
                    intersection_edges=len(edges),
                )

            oracle = mock.Mock()
            # Round 1: first, second. Round 2 rotates to second, first.
            oracle.measure.side_effect = [
                sample({1}),
                sample({2}),
                sample({2, 3}),
                sample({1}),
            ]
            with mock.patch.object(qa3_common.time, "sleep") as sleep:
                campaign = qa3_common.measure_corpus_interleaved(
                    oracle,
                    [first, second],
                    rounds=2,
                    stability_policy="intersection",
                    round_delay_seconds=0.25,
                )
            self.assertEqual(
                [call.args[0] for call in oracle.measure.call_args_list],
                [first.resolve(), second.resolve(), second.resolve(), first.resolve()],
            )
            measured = campaign.measurements
            self.assertEqual(measured[first.resolve()].edges, frozenset({1}))
            self.assertEqual(measured[second.resolve()].edges, frozenset({2}))
            self.assertEqual(measured[second.resolve()].unstable_edges, 1)
            self.assertEqual(measured[second.resolve()].replicas, 2)
            self.assertEqual(
                campaign.orderings,
                (
                    (first.resolve(), second.resolve()),
                    (second.resolve(), first.resolve()),
                ),
            )
            self.assertEqual(len(campaign.samples[first.resolve()]), 2)
            sleep.assert_called_once_with(0.25)

    def test_solver_telemetry_is_strict_and_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "telemetry.json"
            path.write_text(
                json.dumps({"solver_queries": 3, "solver_time_us": 17}),
                encoding="utf-8",
            )
            self.assertEqual(
                qa3_common.load_solver_counts(path),
                qa3_common.SolverCounts(queries=3, time_us=17),
            )
            for invalid in (
                {},
                {"solver_queries": True, "solver_time_us": 17},
                {"solver_queries": 3, "solver_time_us": -1},
                {"solver_queries": 3, "solver_time_us": 1 << 64},
            ):
                with self.subTest(invalid=invalid):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(qa3_common.MeasurementError):
                        qa3_common.load_solver_counts(path)
            path.write_bytes(b" " * (qa3_common.MAX_TELEMETRY_BYTES + 1))
            with self.assertRaises(qa3_common.MeasurementError):
                qa3_common.load_solver_counts(path)

    def test_output_iteration_is_deterministic_and_ignores_non_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "z").write_bytes(b"z")
            (directory / "a").write_bytes(b"a")
            (directory / "skip.hints").write_bytes(b"hint")
            (directory / "subdir").mkdir()
            self.assertEqual(
                [path.name for path in qa3_common.iter_output_files(directory)],
                ["a", "z"],
            )


class QA3CommandTests(unittest.TestCase):
    @staticmethod
    def _fake_symbolic(directory: Path) -> Path:
        path = directory / "fake symbolic.py"
        path.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path

output = Path(os.environ["SYMCC_OUTPUT_DIR"])
(output / "id-next").write_bytes(b"A")
Path(os.environ["SYMCC_TELEMETRY_OUT"]).write_text(
    json.dumps({"solver_queries": 1, "solver_time_us": 2}),
    encoding="utf-8",
)
""",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def test_all_commands_have_side_effect_free_help(self):
        for name in ("iterate.py", "iterate2.py", "landing.py", "landing2.py"):
            with self.subTest(name=name):
                completed = subprocess.run(
                    [sys.executable, str(QA3 / name), "--help"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_iterate_executes_a_minimal_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = self._fake_symbolic(Path(temporary))
            completed = subprocess.run(
                [sys.executable, str(QA3 / "iterate.py"), str(binary), "1", "1"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Z3查询=1", completed.stdout)

    def test_fifo_policy_does_not_require_showmap_measurement(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = self._fake_symbolic(Path(temporary))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(QA3 / "iterate2.py"),
                    str(binary),
                    str(Path(temporary) / "missing-afl-binary"),
                    "1",
                    "1",
                    "fifo",
                    "1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("[fifo]", completed.stdout)
            self.assertIn("Z3查询=1", completed.stdout)

    def test_campaign_driver_derives_both_metrics_from_one_campaign(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "afl-target"
            binary.write_bytes(b"afl")
            corpus = root / "corpus"
            corpus.mkdir()
            strict = corpus / "id-strict"
            optimistic = corpus / "id-optimistic"
            terminal = corpus / "id-terminal"
            strict.write_bytes(b"strict")
            optimistic.write_bytes(b"optimistic")
            terminal.write_bytes(b"terminal")
            seed = corpus / "seed"
            seed.write_bytes(b"seed")
            output = root / "evidence"

            def selected(edges, unstable=0, replicas=2, status="ok"):
                return qa3_common.ReplicatedEdgeMeasurement(
                    edges=frozenset(edges),
                    status=status,
                    replicas=replicas,
                    unstable_edges=unstable,
                    union_edges=len(edges) + unstable,
                    intersection_edges=len(edges),
                )

            measurements = {
                seed.resolve(): selected({1, 2}),
                strict.resolve(): selected({1, 3}),
                optimistic.resolve(): selected({1, 4}, unstable=1),
                terminal.resolve(): selected({1, 99}, status="crash"),
            }
            samples = {
                path: (
                    selected(
                        measurement.edges,
                        replicas=1,
                        status=measurement.status,
                    ),
                    selected(
                        measurement.edges,
                        replicas=1,
                        status=measurement.status,
                    ),
                )
                for path, measurement in measurements.items()
            }
            campaign = qa3_common.InterleavedCorpusMeasurement(
                measurements=measurements,
                samples=samples,
                orderings=(tuple(measurements),),
            )
            oracle = mock.MagicMock()
            oracle.__enter__.return_value = oracle
            oracle.mode = "one-shot"
            oracle.fallbacks = 1
            oracle.restart_count = 0
            oracle.probe_observations = 0
            oracle.probe = {"result": "mocked"}
            oracle.input_mode = "file"
            args = SimpleNamespace(
                afl_binary=binary,
                corpus=corpus,
                seed=seed,
                output=output,
                rounds=2,
                stability_policy="intersection",
                replica_delay_ms=0,
                showmap_timeout_ms=5_000,
                showmap_binary="/bin/true",
                default_strategy="nominal",
                input_mode="auto",
                terminal_status_policy="stratified",
            )
            with mock.patch.object(
                campaign_module, "StreamingCoverageOracle", return_value=oracle
            ), mock.patch.object(
                campaign_module,
                "measure_corpus_interleaved",
                return_value=campaign,
            ):
                summary = campaign_module.run(args)

            self.assertEqual(summary["candidate_inputs"], 3)
            self.assertEqual(
                summary["schema"],
                "symcc-qa3-interleaved-coverage-summary-v2",
            )
            self.assertEqual(summary["input_mode"], "file")
            self.assertEqual(summary["terminal_status_policy"], "stratified")
            self.assertEqual(summary["campaign_observations"], 8)
            self.assertEqual(summary["total_oracle_observations"], 8)
            self.assertEqual(summary["unstable_input_count"], 1)
            self.assertFalse(summary["strict_stability_passed"])
            self.assertEqual(
                summary["strategies"]["strict"]["exclusive_novel_edges"], 1
            )
            self.assertEqual(
                summary["strategies"]["optimistic"][
                    "outputs_with_seed_novel_edges"
                ],
                1,
            )
            terminal_summary = summary["strategies"]["terminal"]
            self.assertEqual(terminal_summary["normal_outputs"], 0)
            self.assertEqual(terminal_summary["excluded_terminal_outputs"], 1)
            self.assertEqual(
                terminal_summary["terminal_outputs"], {"crash": 1}
            )
            self.assertIsNone(terminal_summary["landing_rate_ppm"])
            self.assertEqual(terminal_summary["novel_edge_union"], 0)
            self.assertTrue((output / "raw-campaign.json").is_file())
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "README.md").is_file())
            manifest = (output / "SHA256SUMS.txt").read_text(encoding="ascii")
            self.assertIn("raw-campaign.json", manifest)
            self.assertIn("summary.json", manifest)
            self.assertNotIn("nominal", summary["strategies"])

            args.output = root / "normal-only-rejection"
            args.terminal_status_policy = "normal-only"
            with mock.patch.object(
                campaign_module, "StreamingCoverageOracle", return_value=oracle
            ), mock.patch.object(
                campaign_module,
                "measure_corpus_interleaved",
                return_value=campaign,
            ), self.assertRaisesRegex(
                qa3_common.MeasurementError, "abnormal AFL terminal status"
            ):
                campaign_module.run(args)


if __name__ == "__main__":
    unittest.main()
