# RUN: python3 %s

from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from hybrid_feedback import (  # noqa: E402
    AdaptiveHybridScheduler,
    CSTGTransition,
    ConcolicStateTransitionGraph,
    ConstraintSummaryCache,
    DataCoverageTracker,
    EdgeDependenceCoverage,
    HierarchicalConcurrencyGuidance,
    LinUCBModel,
    ParetoCorpusArchive,
    PrefixDAG,
    PrefixNode,
    ReplayJob,
    SeedWorkerBandit,
    SeedWorkerProfile,
    SolverTelemetry,
    StrategyPortfolio,
    load_static_dependency_map,
    load_directed_distance_map,
)
from mpi_fuzzing_helper import (  # noqa: E402
    CoverageBitmap,
    _build_work_items,
    _comparison_taint_offsets,
    _normalize_s2f_actions,
    _strategy_executor,
    _write_s2f_action_file,
    _write_compact_focus_set,
)


class SolverTelemetryTests(unittest.TestCase):
    def test_engine_contract_preserves_partial_observations(self):
        telemetry = SolverTelemetry.from_observation(
            None,
            engine="symsan",
            input_bytes=31,
            generated=2,
            elapsed=0.125,
            return_code=-9,
            killed=True,
            solver_algorithm="rgd",
        )
        self.assertEqual(telemetry.engine, "symsan")
        self.assertEqual(telemetry.solver_algorithm, "rgd")
        self.assertEqual(telemetry.capabilities, ("execution",))
        self.assertEqual(telemetry.return_code, -9)
        self.assertTrue(telemetry.killed)
        self.assertEqual(telemetry.input_bytes, 31)
        self.assertEqual(telemetry.generated, 2)
        self.assertEqual(telemetry.elapsed_us, 125_000)
        self.assertIn("solver_queries", telemetry.missing_fields)
        self.assertIn("branch_trace", telemetry.missing_fields)

    def test_engine_contract_discovers_reported_capabilities(self):
        telemetry = SolverTelemetry.from_observation(
            {
                "solver_queries": 3,
                "solver_sat": 1,
                "solver_unsat": 2,
                "solver_unknown": 0,
                "solver_time_us": 1500,
                "branch_trace": [[1, 2, 3, 4, 1, 0]],
                "comparison_taints": [[3, 4, 1, 0, 0, 1, 1]],
            },
            engine="symcc",
            input_bytes=8,
            generated=1,
            elapsed=0.01,
            return_code=0,
            killed=False,
        )
        self.assertTrue(telemetry.has_capability("solver"))
        self.assertTrue(telemetry.has_capability("branch_trace"))
        self.assertTrue(telemetry.has_capability("comparison_taint"))
        self.assertNotIn("solver_queries", telemetry.missing_fields)
        self.assertNotIn("branch_trace", telemetry.missing_fields)

    def test_materialized_telemetry_round_trip_preserves_capabilities(self):
        execution_only = SolverTelemetry.from_observation(
            None,
            engine="symsan",
            input_bytes=31,
            generated=2,
            elapsed=0.125,
            return_code=-9,
            killed=True,
            solver_algorithm="rgd",
        )
        restored = SolverTelemetry.from_mapping(asdict(execution_only))

        self.assertEqual(restored, execution_only)
        self.assertEqual(restored.capabilities, ("execution",))
        self.assertFalse(restored.has_capability("solver"))
        self.assertFalse(restored.has_capability("backsolver"))
        self.assertFalse(restored.has_capability("data_coverage"))

    def test_query_ir_structure_capability_and_counters(self):
        telemetry = SolverTelemetry.from_mapping({
            "query_exports": 2,
            "query_ir_nodes": 123,
            "query_ir_input_bytes": 9,
            "query_ir_max_bits": 128,
            "query_ir_comparison_ops": 17,
            "query_ir_nonlinear_ops": 4,
            "query_ir_bitwise_ops": 13,
            "query_ir_structural_ops": 8,
        })
        self.assertTrue(telemetry.has_capability("query_ir_structure"))
        self.assertEqual(telemetry.query_ir_nodes, 123)
        self.assertEqual(telemetry.query_ir_input_bytes, 9)
        self.assertEqual(telemetry.query_ir_max_bits, 128)

    def test_load_and_derived_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "telemetry.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({
                    "schema": 1,
                    "input_bytes": 16,
                    "symbolic_branches": 20,
                    "interesting_branches": 10,
                    "solver_queries": 8,
                    "z3_solves": 8,
                    "z3_timeouts": 2,
                    "backsolver_targets": 3,
                    "backsolver_attempts": 2,
                    "backsolver_sat": 1,
                    "backsolver_constraints_kept": 4,
                    "backsolver_constraints_dropped": 2,
                    "backsolver_direct_attempts": 3,
                    "backsolver_direct_sat": 1,
                    "backsolver_validations": 5,
                    "backsolver_validation_failures": 2,
                    "backsolver_z3_fallbacks": 1,
                    "poly_cache_hits": 1,
                    "poly_cache_entries": 2,
                    "poly_samples": 3,
                    "poly_dense_walks": 4,
                    "poly_john_steps": 5,
                    "poly_dense_fallbacks": 1,
                    "prefix_context_hits": 4,
                    "prefix_context_entries": 5,
                    "unsat_core_hits": 6,
                    "unsat_core_entries": 7,
                    "unsat_core_clauses": 8,
                    "unsat_core_minimized": 9,
                    "unsat_core_unification_hits": 10,
                    "generated": 4,
                    "max_dependency_bytes": 8,
                    "solver_time_us": 20_000,
                    "target_branch": 99,
                    "target_reached": True,
                    "open_branches": [101, 202, 101],
                    "branch_trace": [[1, 2, 3, 4, 1, 0]],
                    "data_comparisons": 2,
                    "data_coverage_map_updates": 11,
                    "empirical_domain_profiles_loaded": 3,
                    "empirical_domain_context_skips": 2,
                    "empirical_domain_parse_failures": 0,
                    "empirical_domain_attempts": 5,
                    "empirical_domain_prefilter_rejects": 3,
                    "empirical_domain_solver_queries": 2,
                    "empirical_domain_solver_time_us": 3456,
                    "empirical_domain_sat": 2,
                    "empirical_domain_validated": 2,
                    "empirical_domain_validation_failures": 0,
                    "empirical_domain_unsat_fallbacks": 3,
                    "empirical_domain_unknown_fallbacks": 0,
                    "data_features": [[55, 12, 16]],
                    "empirical_value_profile_context": "a" * 64,
                    "empirical_value_profiles": [
                        [91, 8, 6, 0, [[1, 4], [2, 2]]],
                        [92, 8, 9, 0, [[value, 1] for value in range(9)]],
                        [93, 8, 2, 0, [[1, 1], [1, 1]]],
                        [94, 8, 8, 1, [[1, 8]]],
                    ],
                    "empirical_domain_feedback": [[
                        91, 8, [1, 2], 5, 3, 2, 2, 2, 0, 0, 0, 3456,
                    ]],
                    "static_data_regions": 3,
                    "static_data_accesses": 4,
                    "data_switches": 2,
                    "data_switch_probes": 4,
                    "static_data_features": [[77, 2, 15, 16, 1]],
                    "comparison_taints": [[9, 10, 2, 4, 5, 1, 1]],
                }, stream)
            telemetry = SolverTelemetry.load(path)

        self.assertIsNotNone(telemetry)
        self.assertAlmostEqual(telemetry.solve_yield, 0.5)
        self.assertAlmostEqual(telemetry.timeout_ratio, 0.25)
        self.assertAlmostEqual(telemetry.backsolver_yield, 0.5)
        self.assertAlmostEqual(telemetry.backsolver_direct_yield, 0.5)
        self.assertAlmostEqual(
            telemetry.backsolver_validation_failure_ratio, 0.4)
        self.assertGreater(telemetry.difficulty, 0.0)
        self.assertLessEqual(telemetry.difficulty, 1.0)
        self.assertTrue(telemetry.target_reached)
        self.assertEqual(telemetry.open_branches, (101, 202))
        self.assertEqual(telemetry.branch_trace, ((1, 2, 3, 4, 1, 0),))
        self.assertEqual(telemetry.data_features, ((55, 12, 16),))
        self.assertEqual(
            telemetry.empirical_value_profiles,
            ((91, 8, 6, 0, ((1, 4), (2, 2))),),
        )
        self.assertEqual(telemetry.empirical_value_profile_context, "a" * 64)
        self.assertTrue(telemetry.has_capability("empirical_value_profile"))
        self.assertTrue(telemetry.has_capability("empirical_domain_solver"))
        self.assertEqual(telemetry.empirical_domain_profiles_loaded, 3)
        self.assertEqual(telemetry.empirical_domain_attempts, 5)
        self.assertEqual(telemetry.empirical_domain_prefilter_rejects, 3)
        self.assertEqual(telemetry.empirical_domain_solver_queries, 2)
        self.assertEqual(telemetry.empirical_domain_solver_time_us, 3456)
        self.assertEqual(telemetry.empirical_domain_validated, 2)
        self.assertEqual(telemetry.empirical_domain_unsat_fallbacks, 3)
        self.assertEqual(
            telemetry.empirical_domain_feedback,
            ((91, 8, (1, 2), 5, 3, 2, 2, 2, 0, 0, 0, 3456),),
        )
        self.assertEqual(
            telemetry.static_data_features, ((77, 2, 15, 16, 1),))
        self.assertEqual(telemetry.data_switches, 2)
        self.assertEqual(telemetry.data_switch_probes, 4)
        self.assertTrue(telemetry.has_capability("static_data_coverage"))
        self.assertEqual(telemetry.comparison_taints, ((9, 10, 2, 4, 5, 1, 1),))
        self.assertAlmostEqual(telemetry.comparison_taint_locality, 1.0)
        self.assertEqual(telemetry.data_coverage_map_updates, 11)
        self.assertEqual(telemetry.poly_cache_hits, 1)
        self.assertEqual(telemetry.poly_samples, 3)
        self.assertEqual(telemetry.poly_dense_walks, 4)
        self.assertEqual(telemetry.poly_john_steps, 5)
        self.assertEqual(telemetry.poly_dense_fallbacks, 1)
        self.assertEqual(telemetry.prefix_context_hits, 4)
        self.assertEqual(telemetry.prefix_context_entries, 5)
        self.assertEqual(telemetry.unsat_core_clauses, 8)
        self.assertEqual(telemetry.unsat_core_unification_hits, 10)
        self.assertEqual(telemetry.backsolver_targets, 3)
        self.assertEqual(telemetry.backsolver_constraints_kept, 4)
        self.assertEqual(telemetry.backsolver_constraints_dropped, 2)
        self.assertEqual(telemetry.backsolver_direct_attempts, 3)
        self.assertEqual(telemetry.backsolver_direct_sat, 1)
        self.assertEqual(telemetry.backsolver_validations, 5)
        self.assertEqual(telemetry.backsolver_validation_failures, 2)
        self.assertEqual(telemetry.backsolver_z3_fallbacks, 1)


class StructuredFeedbackTests(unittest.TestCase):
    def test_static_dependency_map_rejects_malformed_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dependencies")
            Path(path).write_text(
                "# symcc-static-input-dependence-v1\n"
                "10 2 4 main\n10 8 8 main\n"
                "bad 1 2\n11 9 3 invalid\n"
            )
            self.assertEqual(
                load_static_dependency_map(path),
                {10: ((2, 4), (8, 8))})
            self.assertEqual(
                load_static_dependency_map(path, max_bytes=4),
                {},
            )

    def test_cstg_tracks_divergence_and_emits_extended_actionseed(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            graph = ConcolicStateTransitionGraph(
                max_transitions=128, action_cap=4)
            graph.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "target_branch": 99,
                    "target_reached": False,
                    "branch_trace": [
                        [1, 2, 99, 10, 1, 0],
                        [2, 3, 100, 20, 0, 0],
                    ],
                    "solver_time_us": 5000,
                }),
                reward=0.2,
                elapsed=0.5,
                killed=False,
                now=10.0,
            )
            self.assertEqual(graph.transitions[99].divergences, 1)
            jobs = graph.select(1, cooldown=1.0, now=100.0)
            self.assertEqual(jobs[0].path, seed)
            self.assertEqual(jobs[0].target_branch, 100)
            self.assertGreaterEqual(len(jobs[0].actions), 2)

            restored = ConcolicStateTransitionGraph(max_transitions=128)
            restored.restore(graph.to_mapping())
            self.assertEqual(restored.transitions[99].divergences, 1)

    def test_cstg_uses_target_outcome_when_trace_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            graph = ConcolicStateTransitionGraph(max_transitions=128)
            graph.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 2, 99, 10, 1, 0]],
                }),
                reward=0.1,
                elapsed=0.1,
                killed=False,
                now=10.0,
            )

            graph.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "target_branch": 99,
                    "target_reached": True,
                    "target_status": "sat",
                    "branch_trace": [],
                }),
                reward=0.8,
                elapsed=0.2,
                killed=False,
                now=20.0,
            )

            transition = graph.transitions[99]
            self.assertEqual(transition.status, "sat")
            self.assertEqual(transition.arrivals, 1)
            self.assertEqual(transition.divergences, 0)

    def test_coverage_bitmap_exports_sparse_bit_delta(self):
        coverage = CoverageBitmap()
        self.assertEqual(coverage.merge_delta([(2, 1), (2, 3), (9, 4)]), 3)
        self.assertEqual(coverage.consume_delta(), [(2, 3), (9, 4)])
        self.assertEqual(coverage.consume_delta(), [])

    def test_data_coverage_replaces_dominated_seed(self):
        tracker = DataCoverageTracker()
        self.assertEqual(tracker.observe("a", ((7, 4, 16),)), 4)
        self.assertEqual(tracker.observe("b", ((7, 3, 16),)), 0)
        self.assertEqual(tracker.observe("b", ((7, 12, 16),)), 8)
        self.assertEqual(tracker.best[7], (12, 16, "b"))
        self.assertEqual(tracker.path_bonus("a"), 0.0)
        self.assertGreater(tracker.path_bonus("b"), 0.0)

        self.assertEqual(
            tracker.observe(
                "c",
                (),
                static_features=((99, 4, 8, 16, 1),),
                code_summary=123,
            ),
            8,
        )
        self.assertEqual(
            tracker.observe(
                "d",
                (),
                static_features=((99, 4, 12, 16, 1),),
                code_summary=123,
            ),
            4,
        )
        self.assertEqual(tracker.refinements, 1)
        self.assertEqual(
            tracker.static_best[(99, 4, 16)], (12, "d", 123, 1))
        self.assertEqual(
            tracker.observe(
                "e",
                (),
                static_features=((99, 4, 24, 32, 1),),
                code_summary=123,
            ),
            24,
        )
        self.assertIn((99, 4, 16), tracker.static_best)
        self.assertIn((99, 4, 32), tracker.static_best)
        restored = DataCoverageTracker()
        restored.restore(tracker.to_mapping())
        self.assertEqual(restored.static_best, tracker.static_best)
        self.assertEqual(restored.refinements, 1)
        self.assertAlmostEqual(
            restored.path_bonus("d"), tracker.path_bonus("d")
        )

    def test_data_coverage_winners_and_path_aggregate_are_bounded(self):
        tracker = DataCoverageTracker(128, 128)
        for feature_id in range(1, 257):
            tracker.observe(
                f"seed-{feature_id}",
                ((feature_id, 8, 8),),
                static_features=((feature_id, 0, 16, 16, 1),),
            )
        self.assertEqual((len(tracker.best), len(tracker.static_best)), (128, 128))
        self.assertEqual(tracker.path_bonus("seed-1"), 0.0)
        self.assertGreater(tracker.path_bonus("seed-256"), 0.0)
        self.assertLessEqual(len(tracker._path_quality), 128)
        self.assertEqual(
            tracker.observe("invalid", ((999, 1, 0),)),
            0,
        )

        restored = DataCoverageTracker(128, 128)
        restored.restore(tracker.to_mapping())
        self.assertAlmostEqual(
            restored.path_bonus("seed-256"),
            tracker.path_bonus("seed-256"),
        )

    def test_pareto_corpus_preserves_extremes_and_replaces_dominated(self):
        archive = ParetoCorpusArchive(max_entries=8, grid_bins=4)

        def telemetry(
            *,
            path_hash=0,
            sites=(),
            string_queries=0,
            string_verified=0,
        ):
            return SolverTelemetry.from_mapping({
                "path_hash": path_hash,
                "branch_trace": [
                    [index, index + 1, index + 2, site, 1, 0]
                    for index, site in enumerate(sites, 1)
                ],
                "string_solver_queries": string_queries,
                "string_solver_verified": string_verified,
            })

        self.assertTrue(archive.observe(
            "edge",
            coverage_delta=16,
            data_delta=0,
            reward=0.8,
            elapsed=0.2,
            telemetry=telemetry(path_hash=1, sites=(10,)),
        ))
        self.assertTrue(archive.observe(
            "data",
            coverage_delta=0,
            data_delta=32,
            reward=0.7,
            elapsed=0.2,
            telemetry=telemetry(path_hash=2, sites=(20,)),
        ))
        self.assertTrue(archive.observe(
            "string",
            coverage_delta=0,
            data_delta=0,
            reward=0.6,
            elapsed=0.2,
            telemetry=telemetry(
                path_hash=3,
                sites=(30,),
                string_queries=4,
                string_verified=4,
            ),
        ))
        self.assertFalse(archive.observe(
            "dominated",
            coverage_delta=0,
            data_delta=0,
            reward=0.0,
            elapsed=2.0,
            telemetry=telemetry(),
        ))
        self.assertNotIn("dominated", archive.entries)
        self.assertEqual(archive.rejections, 1)

        self.assertTrue(archive.observe(
            "super",
            coverage_delta=128,
            data_delta=128,
            reward=1.0,
            elapsed=0.01,
            telemetry=telemetry(
                path_hash=4,
                sites=tuple(range(100, 132)),
                string_queries=8,
                string_verified=8,
            ),
        ))
        self.assertEqual(set(archive.entries), {"super"})
        self.assertEqual(archive.dominance_evictions, 3)
        self.assertGreater(archive.priority("super"), 0.0)

        restored = ParetoCorpusArchive(max_entries=8, grid_bins=4)
        restored.restore(archive.to_mapping())
        self.assertEqual(set(restored.entries), {"super"})
        self.assertEqual(restored.dominance_evictions, 3)

        dense = ParetoCorpusArchive(max_entries=8, grid_bins=2)
        for index in range(9):
            dense.observe(
                f"tradeoff-{index}",
                coverage_delta=index * 4,
                data_delta=(8 - index) * 4,
                reward=0.5,
                elapsed=1.0,
                telemetry=telemetry(
                    path_hash=index + 1, sites=(index + 1,)),
            )
        self.assertEqual(len(dense.entries), 8)
        self.assertEqual(dense.density_evictions, 1)
        self.assertIn("tradeoff-0", dense.entries)
        self.assertIn("tradeoff-8", dense.entries)

    def test_edge_dependence_coverage_schedules_distinct_context_seeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed_a = os.path.join(tmp, "a")
            seed_b = os.path.join(tmp, "b")
            Path(seed_a).write_bytes(b"a")
            Path(seed_b).write_bytes(b"b")
            coverage = EdgeDependenceCoverage(max_branches=128, max_cells=1024)

            first_delta = coverage.observe(
                seed_a,
                SolverTelemetry.from_mapping({
                    "branch_trace": [
                        [1, 2, 3, 10, 1, 0],
                        [2, 4, 5, 20, 0, 0],
                    ],
                }),
                now=10.0,
            )
            second_delta = coverage.observe(
                seed_b,
                SolverTelemetry.from_mapping({
                    "branch_trace": [
                        [1, 6, 7, 10, 0, 0],
                        [6, 8, 9, 30, 1, 0],
                    ],
                }),
                now=20.0,
            )

            self.assertGreaterEqual(first_delta, 4)
            self.assertGreaterEqual(second_delta, 4)
            jobs = coverage.select(2, cooldown=1.0, now=100.0)
            self.assertEqual({job.path for job in jobs}, {seed_a, seed_b})
            self.assertEqual(
                {
                    job.path: job.target_branch in {3, 5}
                    if job.path == seed_a else job.target_branch in {7, 9}
                    for job in jobs
                },
                {seed_a: True, seed_b: True},
            )

    def test_edge_dependence_directed_distance_prioritizes_near_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            near = os.path.join(tmp, "near")
            far = os.path.join(tmp, "far")
            Path(near).write_bytes(b"near")
            Path(far).write_bytes(b"far")
            coverage = EdgeDependenceCoverage(
                max_branches=128,
                max_cells=1024,
                directed_distances={44: 0.0, 99: 12.0},
            )
            coverage.observe(
                far,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 900, 990, 99, 1, 0]],
                }),
                now=10.0,
            )
            coverage.observe(
                near,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 400, 440, 44, 1, 0]],
                }),
                now=20.0,
            )

            self.assertEqual(
                [(job.path, job.target_branch)
                 for job in coverage.select(1, cooldown=1.0, now=100.0)],
                [(near, 440)],
            )

    def test_edge_dependence_utility_feedback_round_trips_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            fast = os.path.join(tmp, "fast")
            slow = os.path.join(tmp, "slow")
            Path(fast).write_bytes(b"fast")
            Path(slow).write_bytes(b"slow")
            coverage = EdgeDependenceCoverage(
                max_branches=128,
                max_cells=1024,
                directed_distances={77: 1.0, 88: 1.0},
            )
            coverage.observe(
                slow,
                SolverTelemetry.from_mapping({
                    "target_branch": 880,
                    "target_status": "unknown",
                    "branch_trace": [[1, 800, 880, 88, 1, 0]],
                    "z3_solves": 1,
                    "z3_timeouts": 1,
                }),
                now=10.0,
                elapsed=5.0,
            )
            coverage.observe(
                fast,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 700, 770, 77, 1, 0]],
                    "solver_queries": 1,
                    "solver_sat": 1,
                    "generated": 1,
                }),
                now=20.0,
                coverage_delta=3,
                interesting_cases=1,
                elapsed=0.2,
            )

            restored = EdgeDependenceCoverage(
                max_branches=128,
                max_cells=1024,
                directed_distances={77: 1.0, 88: 1.0},
            )
            restored.restore(coverage.to_mapping())
            self.assertEqual(
                [(job.path, job.target_branch)
                 for job in restored.select(1, cooldown=1.0, now=100.0)],
                [(fast, 770)],
            )

    def test_edge_dependence_duplicate_observation_preserves_round_robin(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            coverage = EdgeDependenceCoverage(
                max_branches=128, max_cells=1024)
            for target in (100, 200):
                coverage.observe(
                    seed,
                    SolverTelemetry.from_mapping({
                        "branch_trace": [[1, 2, target, 77, 1, 0]],
                    }),
                    now=float(target),
                )
            row = next(iter(coverage.rows.values()))
            self.assertEqual(row.target_branches, (100, 200))
            row.target_cursor = 1

            coverage.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 2, 100, 77, 1, 0]],
                }),
                now=300.0,
            )

            self.assertEqual(row.target_branches, (100, 200))
            self.assertEqual(row.target_cursor, 1)
            self.assertEqual(
                coverage.select(1, cooldown=0.0, now=400.0)[0].target_branch,
                200,
            )

    def test_edge_dependence_terminal_without_trace_retires_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            coverage = EdgeDependenceCoverage(
                max_branches=128, max_cells=1024)
            coverage.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 2, 300, 77, 1, 0]],
                }),
                now=10.0,
            )
            job = coverage.select(1, cooldown=0.0, now=20.0)[0]
            self.assertEqual(job.target_branch, 300)
            self.assertIn(300, coverage.target_leases)

            coverage.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "target_branch": 300,
                    "target_reached": True,
                    "target_status": "sat",
                }),
                now=30.0,
            )

            self.assertNotIn(300, coverage.target_leases)
            self.assertTrue(all(
                300 not in row.target_branches
                for row in coverage.rows.values()
            ))

    def test_edge_dependence_target_lease_suppresses_parallel_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "first")
            second = os.path.join(tmp, "second")
            Path(first).write_bytes(b"first")
            Path(second).write_bytes(b"second")
            coverage = EdgeDependenceCoverage(
                max_branches=128,
                max_cells=1024,
                target_lease_seconds=120.0,
            )
            coverage.observe(
                first,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 2, 900, 11, 1, 0]],
                }),
                now=10.0,
            )
            coverage.observe(
                second,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 3, 900, 22, 1, 0]],
                }),
                now=20.0,
            )

            jobs = coverage.select(2, cooldown=0.0, now=100.0)
            self.assertEqual([job.target_branch for job in jobs], [900])
            self.assertEqual(
                sum(row.scheduled for row in coverage.rows.values()), 1)
            self.assertEqual(coverage.lease_reservations, 1)
            self.assertGreaterEqual(coverage.lease_suppressions, 1)
            self.assertEqual(coverage.select(
                2, cooldown=0.0, now=219.0), [])

            expired = coverage.select(2, cooldown=0.0, now=221.0)
            self.assertEqual([job.target_branch for job in expired], [900])

            coverage.observe(
                expired[0].path,
                SolverTelemetry.from_mapping({
                    "target_branch": 900,
                    "target_reached": False,
                    "target_status": "unknown",
                }),
                now=222.0,
            )
            self.assertNotIn(900, coverage.target_leases)

    def test_edge_dependence_group_lease_releases_action_targets(self):
        coverage = EdgeDependenceCoverage(
            max_branches=128,
            max_cells=1024,
            target_lease_seconds=120.0,
        )
        self.assertTrue(coverage.reserve_targets(
            100, (100, 200, 300), cooldown=1.0, now=10.0))
        self.assertEqual(coverage.leased_targets(20.0), {100, 200, 300})
        coverage.release_target(100)
        self.assertEqual(coverage.leased_targets(20.0), set())

    def test_edge_dependence_branch_pruning_rebuilds_cell_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            coverage = EdgeDependenceCoverage(
                max_branches=128,
                max_cells=20000,
                trace_cap=128,
            )
            coverage.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "branch_trace": [
                        [site, site + 1000, site + 2000, site, 1, 0]
                        for site in range(1, 129)
                    ],
                }),
                now=10.0,
            )
            coverage.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "branch_trace": [
                        [1, 1001, 2001, 1, 1, 0],
                        [129, 1129, 2129, 129, 1, 0],
                    ],
                }),
                now=20.0,
            )

            actual: dict[int, int] = {}
            for source, _dest in coverage.cells:
                actual[source] = actual.get(source, 0) + 1
            self.assertEqual(len(coverage.rows), 128)
            self.assertEqual(coverage.row_cells, actual)
            self.assertTrue(all(
                source in coverage.rows and dest in coverage.rows
                for source, dest in coverage.cells
            ))

    def test_edge_dependence_restore_is_idempotent_and_drops_orphan_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            coverage = EdgeDependenceCoverage(
                max_branches=128, max_cells=1024)
            coverage.observe(
                seed,
                SolverTelemetry.from_mapping({
                    "branch_trace": [
                        [1, 2, 3, 10, 1, 0],
                        [2, 4, 5, 20, 0, 0],
                    ],
                }),
                now=10.0,
            )
            raw = coverage.to_mapping()
            raw["cells"].append([999, 999, 1, 1])
            restored = EdgeDependenceCoverage(
                max_branches=128, max_cells=1024)

            restored.restore(raw)
            first_cells = dict(restored.cells)
            first_counts = dict(restored.row_cells)
            restored.restore(raw)

            self.assertNotIn((999, 999), restored.cells)
            self.assertEqual(restored.cells, first_cells)
            self.assertEqual(restored.row_cells, first_counts)

    def test_hierarchical_concurrency_guidance_replays_nearest_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            guidance_path = os.path.join(tmp, "concurrency.txt")
            Path(guidance_path).write_text("44 0\n66 4\n", encoding="utf-8")
            near_seed = os.path.join(tmp, "near")
            far_seed = os.path.join(tmp, "far")
            Path(near_seed).write_bytes(b"near")
            Path(far_seed).write_bytes(b"far")
            guidance = HierarchicalConcurrencyGuidance(guidance_path)
            guidance.observe(
                far_seed,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[0, 20, 21, 66, 1, 0]],
                }),
                reward=0.5,
                now=10.0,
            )
            guidance.observe(
                near_seed,
                SolverTelemetry.from_mapping({
                    "branch_trace": [[0, 30, 31, 44, 1, 0]],
                }),
                reward=0.1,
                now=20.0,
            )

            jobs = guidance.select(2, cooldown=1.0, now=100.0,
                                   exclude_paths=set())
            self.assertEqual((jobs[0].path, jobs[0].target_branch),
                             (near_seed, 31))
            self.assertEqual(jobs[1].path, far_seed)

    def test_constraint_cache_retires_terminal_target(self):
        cache = ConstraintSummaryCache()
        cache.observe(SolverTelemetry.from_mapping({
            "target_branch": 42,
            "target_reached": True,
            "target_status": "unsat",
        }), now=10.0)
        self.assertFalse(cache.allows(42, now=1000.0))

    def test_constraint_cache_retries_diverged_target(self):
        cache = ConstraintSummaryCache()
        cache.observe(SolverTelemetry.from_mapping({
            "target_branch": 42,
            "target_reached": False,
            "solver_unsat": 9,
        }), now=10.0)
        self.assertEqual(cache.entries[42].status, "diverged")
        self.assertFalse(cache.allows(42, now=14.0))
        self.assertTrue(cache.allows(42, now=15.0))

    def test_constraint_cache_is_bounded_and_persists_relative_retry(self):
        cache = ConstraintSummaryCache(max_entries=128)
        for branch_id in range(1, 257):
            cache.observe(SolverTelemetry.from_mapping({
                "target_branch": branch_id,
                "target_reached": False,
            }), now=100.0)
        self.assertEqual(len(cache.entries), 128)
        self.assertNotIn(1, cache.entries)
        self.assertIn(256, cache.entries)

        with mock.patch("hybrid_feedback.time.monotonic", return_value=102.0):
            persisted = cache.to_mapping()
        entry = next(item for item in persisted if item["branch_id"] == 256)
        self.assertEqual(entry["clock"], "relative")
        self.assertEqual(entry["retry_after"], 3.0)

        restored = ConstraintSummaryCache(max_entries=128)
        with mock.patch("hybrid_feedback.time.monotonic", return_value=5000.0):
            restored.restore([entry])
        self.assertFalse(restored.allows(256, now=5002.0))
        self.assertTrue(restored.allows(256, now=5003.0))

    def test_directed_distance_map_accepts_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            text_path = os.path.join(tmp, "distance.txt")
            Path(text_path).write_text("0x10 3.5\n20:0\nbad row\n",
                                      encoding="utf-8")
            self.assertEqual(
                load_directed_distance_map(text_path), {16: 3.5, 20: 0.0})

            json_path = os.path.join(tmp, "distance.json")
            Path(json_path).write_text(
                json.dumps({"distances": {"16": 2, "0x20": 7}}),
                encoding="utf-8")
            self.assertEqual(
                load_directed_distance_map(json_path), {16: 2.0, 32: 7.0})

            bounded_path = os.path.join(tmp, "bounded.txt")
            Path(bounded_path).write_text(
                "1 4\n2 3\n3 2\n1 1\n", encoding="ascii"
            )
            self.assertEqual(
                load_directed_distance_map(bounded_path, max_entries=2),
                {1: 1.0, 2: 3.0},
            )
            self.assertEqual(
                load_directed_distance_map(bounded_path, max_bytes=4),
                {},
            )

    def test_invalid_file_is_missing_telemetry(self):
        self.assertIsNone(SolverTelemetry.load("/does/not/exist"))
        with tempfile.TemporaryDirectory() as tmp:
            oversized = os.path.join(tmp, "oversized.json")
            Path(oversized).write_text('{"schema": 1}', encoding="ascii")
            self.assertIsNone(SolverTelemetry.load(oversized, max_bytes=4))
            fifo = os.path.join(tmp, "telemetry.fifo")
            os.mkfifo(fifo)
            self.assertIsNone(SolverTelemetry.load(fifo))

    def test_prefix_dag_stays_within_node_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            dag = PrefixDAG(128)
            for index in range(200):
                opposite = 10_000 + index
                dag.ingest(
                    seed,
                    SolverTelemetry.from_mapping({
                        "open_branches": [opposite],
                        "branch_trace": [[
                            0, 20_000 + index, opposite, 30_000 + index,
                            index & 1, 0,
                        ]],
                    }),
                    reward=0.1,
                    now=1000.0 + index,
                )
            self.assertLessEqual(len(dag.nodes), dag.max_nodes)

    def test_prefix_dag_frequency_sketch_is_bounded_and_keeps_heavy_site(self):
        dag = PrefixDAG(128)
        dag.site_frequency_cap = 128
        for site in range(1, 257):
            dag._record_site_frequency(site)
        for _ in range(300):
            dag._record_site_frequency(256)
        self.assertLessEqual(len(dag.site_frequency), 128)
        self.assertIn(256, dag.site_frequency)
        self.assertGreaterEqual(dag.site_frequency[256], 300)
        self.assertLessEqual(len(dag._site_frequency_heap), 512)

    def test_prefix_dag_exposes_sampling_profile_for_hard_branch(self):
        dag = PrefixDAG(128)
        dag.nodes[10] = PrefixNode(
            branch_id=10,
            parent_id=0,
            site_id=11,
            outcome=1,
            status="observed",
            reward=0.3,
            difficulty=0.4,
        )
        dag.nodes[33] = PrefixNode(
            branch_id=33,
            parent_id=10,
            site_id=44,
            outcome=0,
            status="open",
            attempts=1,
            reward=0.2,
            difficulty=0.7,
        )
        dag.nodes[33].actions["exact"].attempts = 1
        self.assertIn(6, dag.preferred_strategies(33, 7))
        dag.nodes[33].actions["sampling"].attempts = dag.sampling_budget
        self.assertNotIn(6, dag.preferred_strategies(33, 7))

    def test_prefix_dag_tracks_per_seed_executor_actions(self):
        dag = PrefixDAG(128)
        dag.nodes[33] = PrefixNode(
            branch_id=33,
            parent_id=0,
            site_id=44,
            outcome=0,
            status="open",
            difficulty=0.7,
        )
        self.assertEqual(dag.preferred_strategies(33, 7), [0])
        telemetry = SolverTelemetry.from_mapping({
            "target_branch": 33,
            "target_reached": True,
            "solver_unknown": 1,
            "solver_queries": 1,
        })
        dag.ingest(
            "seed", telemetry, reward=0.0, now=10.0,
            strategy=0, elapsed=2.0)
        self.assertEqual(dag.nodes[33].actions["exact"].attempts, 1)
        self.assertEqual(
            dag.nodes[33].actions["exact"].consecutive_failures, 1)
        self.assertIn(6, dag.preferred_strategies(33, 7))

    def test_prefix_dag_interleaves_high_and_low_seed_queues(self):
        with tempfile.TemporaryDirectory() as tmp:
            high_seed = os.path.join(tmp, "high")
            low_seed = os.path.join(tmp, "low")
            Path(high_seed).write_bytes(b"high")
            Path(low_seed).write_bytes(b"low")
            dag = PrefixDAG(128)
            dag.nodes[10] = PrefixNode(
                10, 0, 11, 0, "open", reward=0.8,
                seed_paths=(high_seed,))
            dag.nodes[20] = PrefixNode(
                20, 0, 22, 0, "open", difficulty=0.5,
                seed_paths=(low_seed,))
            selected = [
                dag.select(1, cooldown=0.0, now=100.0 + index)[0].path
                for index in range(4)
            ]
            self.assertEqual(selected[:3], [high_seed] * 3)
            self.assertEqual(selected[3], low_seed)

    def test_prefix_dag_builds_s2f_actionseed_for_one_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            dag = PrefixDAG(128)
            dag.nodes[10] = PrefixNode(
                10, 0, 11, 0, "open", reward=0.8,
                seed_paths=(seed,))
            dag.nodes[20] = PrefixNode(
                20, 0, 11, 1, "open", reward=0.3, difficulty=0.8,
                seed_paths=(seed,))
            dag.nodes[20].actions["exact"].attempts = 1
            dag.nodes[30] = PrefixNode(
                30, 0, 22, 0, "open", difficulty=0.2,
                seed_paths=(seed,))

            job = dag.select(1, cooldown=0.0, now=100.0)[0]

            self.assertEqual(job.path, seed)
            self.assertEqual(job.target_branch, 10)
            self.assertIn((10, "solve"), job.actions)
            self.assertIn((20, "sample"), job.actions)
            self.assertIn((30, "skip"), job.actions)

    def test_prefix_dag_records_multigo_path_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            dag = PrefixDAG(128)
            dag.ingest(
                seed,
                SolverTelemetry.from_mapping({
                    "open_branches": [30],
                    "branch_trace": [
                        [0, 10, 20, 100, 1, 1],
                        [10, 20, 30, 200, 0, 0],
                    ],
                }),
                reward=0.4,
                now=100.0,
            )

            self.assertGreater(dag.total_site_frequency, 0)
            self.assertGreater(dag.nodes[20].path_difficulty, 0.0)
            self.assertGreater(dag.nodes[30].target_path_visits, 0)
            self.assertGreater(dag.nodes[30].taco_generation_bonus, 0.0)

    def test_prefix_dag_symcts_prioritizes_directed_data_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            cold_seed = os.path.join(tmp, "cold")
            target_seed = os.path.join(tmp, "target")
            Path(cold_seed).write_bytes(b"cold")
            Path(target_seed).write_bytes(b"target")
            dag = PrefixDAG(128)
            dag.directed_sites.add(44)

            dag.ingest(
                cold_seed,
                SolverTelemetry.from_mapping({
                    "open_branches": [11],
                    "branch_trace": [[0, 10, 11, 99, 1, 0]],
                }),
                reward=0.2,
                now=10.0,
            )
            dag.ingest(
                target_seed,
                SolverTelemetry.from_mapping({
                    "input_bytes": 8,
                    "open_branches": [33],
                    "branch_trace": [[0, 20, 33, 44, 1, 0]],
                    "data_coverage_map_updates": 96,
                    "data_features": [[123, 14, 16]],
                }),
                reward=0.05,
                now=20.0,
            )

            jobs = dag.select(1, cooldown=1.0, now=100.0)
            self.assertEqual(
                [(job.path, job.target_branch) for job in jobs],
                [(target_seed, 33)],
            )

    def test_prefix_dag_colorgo_mdp_prioritizes_feasible_low_cost_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale_seed = os.path.join(tmp, "stale")
            feasible_seed = os.path.join(tmp, "feasible")
            Path(stale_seed).write_bytes(b"stale")
            Path(feasible_seed).write_bytes(b"feasible")
            dag = PrefixDAG(128)
            dag.nodes[33] = PrefixNode(
                branch_id=33,
                parent_id=0,
                site_id=44,
                outcome=1,
                status="open",
                solver_cost=0.8,
                timeout_penalty=0.7,
                color_feasibility=0.03,
                mdp_value=0.02,
                mdp_cost=0.9,
                infeasible_streak=4,
                seed_paths=(stale_seed,),
            )
            dag.nodes[55] = PrefixNode(
                branch_id=55,
                parent_id=0,
                site_id=66,
                outcome=1,
                status="open",
                color_feasibility=0.65,
                mdp_value=0.55,
                mdp_cost=0.05,
                seed_paths=(feasible_seed,),
            )

            job = dag.select(1, cooldown=1.0, now=100.0)[0]
            self.assertEqual((job.path, job.target_branch), (feasible_seed, 55))

    def test_prefix_dag_selective_mdp_laplace_values_converge_on_cycles(self):
        dag = PrefixDAG(128)
        dag.nodes[10] = PrefixNode(
            branch_id=10,
            parent_id=0,
            site_id=100,
            outcome=1,
            status="observed",
            visits=9,
        )
        dag.nodes[20] = PrefixNode(
            branch_id=20,
            parent_id=0,
            site_id=100,
            outcome=0,
            status="open",
        )
        dag.nodes[30] = PrefixNode(
            branch_id=30,
            parent_id=40,
            site_id=300,
            outcome=1,
            status="open",
        )
        dag.nodes[40] = PrefixNode(
            branch_id=40,
            parent_id=30,
            site_id=400,
            outcome=1,
            status="open",
        )

        dag._refresh_mdp_values()

        self.assertAlmostEqual(
            dag.nodes[10].mdp_transition_probability
            + dag.nodes[20].mdp_transition_probability,
            1.0,
        )
        self.assertAlmostEqual(
            dag.nodes[10].mdp_transition_probability, 10.0 / 11.0
        )
        self.assertAlmostEqual(dag.nodes[20].mdp_novelty_reward, 1.0)
        self.assertGreater(dag.nodes[20].mdp_value, 0.0)
        self.assertGreater(dag.nodes[30].mdp_value, 0.0)
        self.assertGreater(dag.nodes[40].mdp_value, 0.0)
        self.assertLessEqual(
            dag.selective_mdp_last_iterations,
            dag.selective_mdp_iterations,
        )
        self.assertLessEqual(
            dag.selective_mdp_last_residual,
            dag.selective_mdp_tolerance,
        )
        self.assertEqual(dag.selective_mdp_transition_groups, 3)

    def test_prefix_dag_mdp_refresh_is_bounded_on_large_graph(self):
        previous_interval = os.environ.get("SYMCC_SELECTIVE_MDP_REFRESH_INTERVAL")
        previous_small = os.environ.get("SYMCC_SELECTIVE_MDP_SMALL_GRAPH_NODES")
        try:
            os.environ["SYMCC_SELECTIVE_MDP_REFRESH_INTERVAL"] = "4"
            os.environ["SYMCC_SELECTIVE_MDP_SMALL_GRAPH_NODES"] = "0"
            dag = PrefixDAG(1024)
            for index in range(1, 8):
                dag.ingest(
                    f"seed{index}",
                    SolverTelemetry.from_mapping({
                        "branch_trace": [[
                            0,
                            index,
                            index + 1000,
                            100 + index,
                            1,
                            0,
                        ]],
                        "open_branches": [index + 1000],
                    }),
                    reward=0.0,
                    now=float(index),
                )

            self.assertEqual(dag.selective_mdp_refreshes, 1)
            self.assertEqual(dag.selective_mdp_skipped_refreshes, 6)
            self.assertGreater(len(dag.nodes), 0)
        finally:
            if previous_interval is None:
                os.environ.pop("SYMCC_SELECTIVE_MDP_REFRESH_INTERVAL", None)
            else:
                os.environ[
                    "SYMCC_SELECTIVE_MDP_REFRESH_INTERVAL"] = previous_interval
            if previous_small is None:
                os.environ.pop("SYMCC_SELECTIVE_MDP_SMALL_GRAPH_NODES", None)
            else:
                os.environ[
                    "SYMCC_SELECTIVE_MDP_SMALL_GRAPH_NODES"] = previous_small

    def test_prefix_dag_prioritizes_concurrency_guidance_sites(self):
        previous = os.environ.get("SYMCC_CONCURRENCY_GUIDANCE")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                guidance = os.path.join(tmp, "concurrency.txt")
                Path(guidance).write_text("44 0 # concurrency\n",
                                          encoding="utf-8")
                os.environ["SYMCC_CONCURRENCY_GUIDANCE"] = guidance
                target_seed = os.path.join(tmp, "target")
                other_seed = os.path.join(tmp, "other")
                Path(target_seed).write_bytes(b"target")
                Path(other_seed).write_bytes(b"other")
                dag = PrefixDAG(128)
                dag.nodes[33] = PrefixNode(
                    33, 0, 44, 1, "open", seed_paths=(target_seed,))
                dag.nodes[55] = PrefixNode(
                    55, 0, 66, 1, "open", seed_paths=(other_seed,))

                job = dag.select(1, cooldown=1.0, now=100.0)[0]
                self.assertEqual((job.path, job.target_branch),
                                 (target_seed, 33))
        finally:
            if previous is None:
                os.environ.pop("SYMCC_CONCURRENCY_GUIDANCE", None)
            else:
                os.environ["SYMCC_CONCURRENCY_GUIDANCE"] = previous

    def test_prefix_dag_dynamic_coloration_updates_target_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            dag = PrefixDAG(128)
            dag.nodes[33] = PrefixNode(
                branch_id=33,
                parent_id=0,
                site_id=44,
                outcome=1,
                status="open",
                color_feasibility=0.8,
                seed_paths=(seed,),
            )
            dag.ingest(
                seed,
                SolverTelemetry.from_mapping({
                    "target_branch": 33,
                    "target_reached": False,
                    "solver_unknown": 1,
                    "z3_timeouts": 1,
                    "solver_time_us": 90000,
                    "branch_trace": [[0, 20, 33, 44, 1, 0]],
                }),
                reward=0.0,
                now=20.0,
            )

            node = dag.nodes[33]
            self.assertGreaterEqual(node.infeasible_streak, 1)
            self.assertGreaterEqual(node.mdp_cost, 0.5)
            self.assertLess(node.color_feasibility, 0.8)

    def test_prefix_dag_cost_pressure_uses_cheap_retry_profile(self):
        dag = PrefixDAG(128)
        dag.nodes[55] = PrefixNode(
            branch_id=55,
            parent_id=0,
            site_id=66,
            outcome=1,
            status="open",
            attempts=2,
            solver_cost=0.70,
            timeout_penalty=0.60,
        )
        self.assertEqual(
            dag.preferred_strategies(55, 7, dual_executor=False), [0, 1, 5])

    def test_prefix_dag_round_trips_symcts_pressure_metrics(self):
        dag = PrefixDAG(128)
        dag.nodes[77] = PrefixNode(
            branch_id=77,
            parent_id=0,
            site_id=88,
            outcome=1,
            status="open",
            data_reward=0.40,
            backsolver_reward=0.25,
            solver_cost=0.50,
            timeout_penalty=0.60,
            path_difficulty=0.44,
            target_distance=0.55,
            target_path_reward=0.35,
            target_path_visits=3,
            taco_generation_bonus=0.42,
            color_feasibility=0.70,
            mdp_value=0.33,
            mdp_cost=0.20,
            infeasible_streak=2,
            seed_paths=("seed",),
        )
        dag.nodes[77].actions["sampling"].attempts = 2
        dag.nodes[77].actions["sampling"].successes = 1
        dag.nodes[77].actions["sampling"].reward_sum = 0.75
        restored = PrefixDAG(128)
        restored.restore(dag.to_mapping())

        self.assertAlmostEqual(restored.nodes[77].data_reward, 0.40)
        self.assertAlmostEqual(restored.nodes[77].backsolver_reward, 0.25)
        self.assertAlmostEqual(restored.nodes[77].solver_cost, 0.50)
        self.assertAlmostEqual(restored.nodes[77].timeout_penalty, 0.60)
        self.assertAlmostEqual(restored.nodes[77].path_difficulty, 0.44)
        self.assertAlmostEqual(restored.nodes[77].target_distance, 0.55)
        self.assertAlmostEqual(restored.nodes[77].target_path_reward, 0.35)
        self.assertEqual(restored.nodes[77].target_path_visits, 3)
        self.assertAlmostEqual(restored.nodes[77].taco_generation_bonus, 0.42)
        self.assertAlmostEqual(restored.nodes[77].color_feasibility, 0.70)
        self.assertGreater(restored.nodes[77].mdp_value, 0.0)
        self.assertGreaterEqual(restored.nodes[77].mdp_cost, 0.20)
        self.assertGreaterEqual(
            restored.nodes[77].mdp_transition_probability, 0.0)
        self.assertGreaterEqual(restored.nodes[77].mdp_novelty_reward, 0.0)
        self.assertEqual(restored.nodes[77].infeasible_streak, 2)
        self.assertEqual(restored.nodes[77].actions["sampling"].attempts, 2)
        self.assertAlmostEqual(
            restored.nodes[77].actions["sampling"].reward_sum, 0.75)


class LinUCBTests(unittest.TestCase):
    def test_linucb_exploration_coefficient_is_finite_and_bounded(self):
        self.assertEqual(LinUCBModel(2, alpha=1e308).alpha, 10.0)
        self.assertEqual(LinUCBModel(2, alpha=float("inf")).alpha, 0.65)
        with mock.patch.dict(
            os.environ,
            {"SYMCC_SIMIFUZZ_ALPHA": "1e308"},
        ):
            bandit = SeedWorkerBandit()
        self.assertEqual(bandit.model.alpha, 10.0)

    def test_observed_productive_context_is_preferred(self):
        model = LinUCBModel(2, alpha=0.0)
        productive = (1.0, 1.0)
        unproductive = (1.0, 0.0)
        for _ in range(20):
            model.update(productive, 1.0)
            model.update(unproductive, 0.0)
        self.assertGreater(model.score(productive)[0], model.score(unproductive)[0])

    def test_linucb_rejects_nonfinite_updates_and_restores_atomically(self):
        model = LinUCBModel(2)
        model.update((1.0, 0.5), 0.75)
        baseline = json.loads(json.dumps(model.to_mapping()))
        model.update((float("nan"), 0.5), 0.8)
        model.update((1.0, 0.5), float("nan"))
        self.assertEqual(model.to_mapping(), baseline)

        malformed = json.loads(json.dumps(baseline))
        malformed["a_inv"][0][0] = 99.0
        malformed["b"][1] = float("inf")
        model.restore(malformed)
        self.assertEqual(model.to_mapping(), baseline)

    def test_strategy_portfolio_learns_reward_per_cost(self):
        portfolio = StrategyPortfolio(2, exploration=0.0)
        portfolio.update(0, reward=0.8, elapsed=8.0)
        portfolio.update(1, reward=0.6, elapsed=1.0)
        self.assertEqual(portfolio.select(), 1)

    def test_strategy_portfolio_reserves_parallel_assignments(self):
        portfolio = StrategyPortfolio(3)
        self.assertEqual([portfolio.select() for _ in range(3)], [0, 1, 2])

    def test_strategy_portfolio_releases_invalid_feedback_without_learning(self):
        portfolio = StrategyPortfolio(2)
        selected = portfolio.select()
        portfolio.update(selected, reward=float("nan"), elapsed=1.0)
        self.assertEqual(portfolio.pending[selected], 0)
        self.assertEqual(portfolio.pulls, [0, 0])

        baseline = json.loads(json.dumps(portfolio.to_mapping()))
        malformed = json.loads(json.dumps(baseline))
        malformed["reward_sum"][0] = float("inf")
        portfolio.restore(malformed)
        self.assertEqual(portfolio.to_mapping(), baseline)


class SeedWorkerBanditTests(unittest.TestCase):
    def test_worker_local_novelty_changes_pair_ranking(self):
        scheduler = AdaptiveHybridScheduler(2)
        scheduler.seed_worker.model.alpha = 0.0
        first = scheduler.context("first")
        second = scheduler.context("second")
        scheduler.seed_worker.seeds[first.path] = SeedWorkerProfile(
            path_hash=11, sites={1})
        scheduler.seed_worker.seeds[second.path] = SeedWorkerProfile(
            path_hash=22, sites={2})
        state = scheduler.seed_worker._worker(1)
        state.seen_paths.add(11)
        state.seen_sites.add(1)
        scheduler.seed_worker.global_seen_sites.update({1, 2})

        selected = scheduler.work_index(
            1,
            [("first", None, 0), ("second", None, 0)],
            0,
            active_worker_count=2,
        )
        self.assertEqual(selected, 1)

    def test_cross_learning_is_aggregated_by_time_slice(self):
        bandit = SeedWorkerBandit()
        bandit.model.alpha = 0.0
        first = AdaptiveHybridScheduler(1).context("seed")
        telemetry = SolverTelemetry.from_mapping({
            "path_hash": 77,
            "branch_trace": [[0, 1, 2, 10, 1, 0]],
        })

        bandit.reserve(1, first)
        bandit.observe(
            1, first, base_reward=0.2, coverage_delta=1,
            interesting_cases=1, elapsed=1.0, telemetry=telemetry, now=10.0)
        self.assertEqual(bandit.model.observations, 0)
        self.assertEqual(bandit.flush(force=True, now=11.0), 1)

        bandit.reserve(2, first)
        bandit.observe(
            2, first, base_reward=0.1, coverage_delta=0,
            interesting_cases=0, elapsed=1.0, telemetry=telemetry, now=12.0)
        self.assertEqual(bandit.cross_learning_gain, 1)
        self.assertEqual(bandit.flush(force=True, now=13.0), 1)
        self.assertEqual(bandit.model.observations, 2)
        self.assertEqual(bandit.workers[2].cross_learning_gain, 1)

    def test_release_rewinds_unsent_assignment_and_discard_keeps_pull(self):
        bandit = SeedWorkerBandit()
        context = AdaptiveHybridScheduler(1).context("seed")
        bandit.reserve(1, context)
        self.assertTrue(bandit.release(1))
        self.assertEqual(bandit.assignments, 0)
        self.assertNotIn(1, bandit.active)

        bandit.reserve(1, context)
        self.assertTrue(bandit.discard(1))
        self.assertEqual(bandit.assignments, 1)
        self.assertNotIn(1, bandit.active)

    def test_pair_policy_state_round_trip(self):
        bandit = SeedWorkerBandit()
        context = AdaptiveHybridScheduler(1).context("seed")
        telemetry = SolverTelemetry.from_mapping({
            "path_hash": 88,
            "branch_trace": [[0, 1, 2, 20, 1, 0]],
        })
        bandit.reserve(3, context, task_region=9)
        bandit.observe(
            3, context, base_reward=0.5, coverage_delta=2,
            interesting_cases=1, elapsed=2.0, telemetry=telemetry, now=20.0)
        bandit.flush(force=True, now=21.0)

        restored = SeedWorkerBandit()
        restored.restore(bandit.to_mapping())
        self.assertEqual(restored.model.observations, 1)
        self.assertEqual(restored.workers[3].seen_sites, {20})
        self.assertEqual(restored.seeds["seed"].path_hash, 88)
        self.assertAlmostEqual(restored.workers[3].region_reward[9], 0.5)

    def test_pair_policy_restore_obeys_worker_and_profile_budgets(self):
        raw = {
            "workers": {
                str(index): {"executions": index}
                for index in range(1, 257)
            },
            "seeds": [
                {"path": f"seed-{index}", "path_hash": index}
                for index in range(1, 257)
            ],
        }
        bandit = SeedWorkerBandit(max_profiles=128, max_workers=128)
        bandit.restore(raw)
        self.assertEqual((len(bandit.workers), len(bandit.seeds)), (128, 128))
        self.assertNotIn(1, bandit.workers)
        self.assertIn(256, bandit.workers)
        self.assertNotIn("seed-1", bandit.seeds)
        self.assertIn("seed-256", bandit.seeds)

        context = AdaptiveHybridScheduler(1).context("seed")
        for worker in range(257, 513):
            bandit.reserve(worker, context)
        self.assertEqual((len(bandit.workers), len(bandit.active)), (128, 128))


class AdaptiveSchedulerTests(unittest.TestCase):
    def test_candidate_context_cache_obeys_global_state_budget(self):
        with mock.patch.dict(
            os.environ,
            {"SYMCC_ADAPTIVE_STATE_ENTRIES": "128"},
        ):
            scheduler = AdaptiveHybridScheduler(2)
        for index in range(256):
            scheduler.context(f"seed-{index}", size=index)
        self.assertEqual(len(scheduler.contexts), 128)
        self.assertNotIn("seed-0", scheduler.contexts)
        self.assertIn("seed-255", scheduler.contexts)

    def test_policy_state_reader_and_writer_share_byte_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            with mock.patch.dict(
                os.environ,
                {"SYMCC_ADAPTIVE_STATE_MAX_BYTES": str(1024 * 1024)},
            ):
                scheduler = AdaptiveHybridScheduler(2, state)
            context = scheduler.context("seed", size=1)
            scheduler.model.update(context.vector, 0.5)
            baseline = json.loads(json.dumps(scheduler.model.to_mapping()))

            Path(state).write_bytes(b" " * (scheduler.state_max_bytes + 1))
            scheduler._load_state()
            self.assertEqual(scheduler.model.to_mapping(), baseline)

            Path(state).write_bytes(b"old-state")
            oversized = {
                "schema": 14,
                "payload": "x" * scheduler.state_max_bytes,
            }
            with mock.patch.object(
                scheduler, "snapshot", return_value=oversized
            ):
                scheduler.save()
            self.assertEqual(Path(state).read_bytes(), b"old-state")
            self.assertFalse(Path(state + ".tmp").exists())

    def test_prefix_dag_schedules_open_branch_and_retires_sat_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            scheduler = AdaptiveHybridScheduler(5)
            scheduler.context(seed)
            scheduler.observe(
                seed,
                coverage_delta=2,
                interesting_cases=1,
                elapsed=1.0,
                killed=False,
                strategy=0,
                telemetry=SolverTelemetry.from_mapping({
                    "input_bytes": 4,
                    "symbolic_branches": 1,
                    "interesting_branches": 0,
                    "path_hash": 22,
                    "open_branches": [33],
                    "branch_trace": [[11, 22, 33, 44, 1, 0]],
                }),
            )
            self.assertEqual(
                scheduler.prefix_dag.preferred_strategies(33, 5), [0])
            job = scheduler.replay_candidates(1, cooldown=1.0, now=100.0)[0]
            self.assertEqual((job.path, job.target_branch), (seed, 33))
            self.assertIn(scheduler.select_strategy(33), {0, 1, 2, 4})

            scheduler.observe(
                seed,
                coverage_delta=1,
                interesting_cases=1,
                elapsed=1.0,
                killed=False,
                strategy=1,
                telemetry=SolverTelemetry.from_mapping({
                    "target_branch": 33,
                    "target_reached": True,
                    "target_status": "sat",
                    "solver_sat": 1,
                    "generated": 1,
                    "branch_trace": [[11, 22, 33, 44, 1, 1]],
                }),
            )
            self.assertEqual(scheduler.prefix_dag.nodes[33].status, "sat")
            self.assertFalse(scheduler.prefix_dag.constraints.allows(33, 1000.0))

    def test_edge_dependence_replay_is_available_without_new_afl_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            scheduler = AdaptiveHybridScheduler(2)
            scheduler.context(seed)
            reward = scheduler.observe(
                seed,
                coverage_delta=0,
                interesting_cases=0,
                elapsed=1.0,
                killed=False,
                strategy=0,
                telemetry=SolverTelemetry.from_mapping({
                    "branch_trace": [[1, 2, 3, 55, 1, 0]],
                }),
            )
            self.assertGreater(reward, 0.0)
            replay = scheduler.replay_candidates(1, cooldown=1.0, now=100.0)
            self.assertEqual(
                [(job.path, job.target_branch) for job in replay], [(seed, 3)])
            snapshot = scheduler.snapshot()
            self.assertGreaterEqual(snapshot["edge_dependence_cells"], 1)
            self.assertEqual(snapshot["edge_dependence_targets"], 1)
            self.assertEqual(snapshot["edge_dependence_inflight_targets"], 1)

    def test_target_lane_deduplicates_branch_across_prefix_and_cstg(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix_seed = os.path.join(tmp, "prefix")
            cstg_seed = os.path.join(tmp, "cstg")
            Path(prefix_seed).write_bytes(b"prefix")
            Path(cstg_seed).write_bytes(b"cstg")
            scheduler = AdaptiveHybridScheduler(2)
            scheduler.observe(
                prefix_seed,
                coverage_delta=0,
                interesting_cases=0,
                elapsed=0.1,
                killed=False,
                strategy=0,
                telemetry=SolverTelemetry.from_mapping({
                    "open_branches": [77],
                    "branch_trace": [[1, 2, 77, 10, 1, 0]],
                }),
            )
            scheduler.prefix_dag.nodes[77].seed_paths = (prefix_seed,)
            scheduler.cstg.transitions[77].seeds = (cstg_seed,)

            jobs = scheduler.target_candidates(
                2, cooldown=0.0, now=100.0)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].target_branch, 77)
            self.assertEqual(
                scheduler.edge_dependence.leased_targets(101.0), {77})
            self.assertGreaterEqual(
                scheduler.edge_dependence.lease_suppressions, 1)
            self.assertEqual(scheduler.cstg.scheduled, 0)
            self.assertEqual(
                scheduler.cstg.transitions[77].last_scheduled, 0.0)
            self.assertEqual(scheduler.prefix_dag.nodes[77].attempts, 1)

    def test_target_lane_backfills_after_cross_lane_lease_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix_seed = os.path.join(tmp, "prefix")
            duplicate_seed = os.path.join(tmp, "duplicate")
            alternate_seed = os.path.join(tmp, "alternate")
            for path in (prefix_seed, duplicate_seed, alternate_seed):
                Path(path).write_bytes(path.encode())
            scheduler = AdaptiveHybridScheduler(2)
            scheduler.prefix_dag.nodes[77] = PrefixNode(
                branch_id=77,
                parent_id=1,
                site_id=10,
                outcome=0,
                status="open",
                seed_paths=(prefix_seed,),
            )
            scheduler.cstg.transitions[77] = CSTGTransition(
                source=1,
                target=77,
                branch_id=77,
                site_id=10,
                seeds=(duplicate_seed,),
            )
            scheduler.cstg.transitions[88] = CSTGTransition(
                source=1,
                target=88,
                branch_id=88,
                site_id=20,
                seeds=(alternate_seed,),
            )

            jobs = scheduler.target_candidates(
                2, cooldown=0.0, now=100.0)

            self.assertEqual([job.target_branch for job in jobs], [77, 88])
            self.assertEqual(scheduler.prefix_dag.nodes[77].attempts, 1)
            self.assertEqual(scheduler.cstg.scheduled, 1)
            self.assertEqual(
                scheduler.cstg.transitions[77].last_scheduled, 0.0)
            self.assertEqual(
                scheduler.cstg.transitions[88].last_scheduled, 100.0)

    def test_external_target_admission_rejects_without_scheduler_commit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prefix_77 = os.path.join(tmp.name, "prefix-77")
        cstg_77 = os.path.join(tmp.name, "cstg-77")
        cstg_88 = os.path.join(tmp.name, "cstg-88")
        for path in (prefix_77, cstg_77, cstg_88):
            Path(path).write_bytes(path.encode())
        scheduler = AdaptiveHybridScheduler(2)
        scheduler.prefix_dag.nodes[77] = PrefixNode(
            branch_id=77,
            parent_id=1,
            site_id=10,
            outcome=0,
            status="open",
            seed_paths=(prefix_77,),
        )
        scheduler.cstg.transitions[77] = CSTGTransition(
            source=1,
            target=77,
            branch_id=77,
            site_id=10,
            seeds=(cstg_77,),
        )
        scheduler.cstg.transitions[88] = CSTGTransition(
            source=1,
            target=88,
            branch_id=88,
            site_id=20,
            seeds=(cstg_88,),
        )
        attempted: list[int] = []

        def admit(job: ReplayJob) -> bool:
            attempted.append(job.target_branch)
            return job.target_branch != 77

        jobs = scheduler.target_candidates(
            1,
            cooldown=0.0,
            now=100.0,
            external_admission=admit,
        )

        self.assertEqual([job.target_branch for job in jobs], [88])
        self.assertEqual(attempted, [77, 77, 88])
        self.assertEqual(scheduler.prefix_dag.nodes[77].attempts, 0)
        self.assertEqual(
            scheduler.prefix_dag.nodes[77].last_scheduled, 0.0)
        self.assertEqual(scheduler.cstg.transitions[77].attempts, 0)
        self.assertEqual(
            scheduler.cstg.transitions[77].last_scheduled, 0.0)
        self.assertEqual(scheduler.cstg.scheduled, 1)
        self.assertEqual(
            scheduler.cstg.transitions[88].last_scheduled, 100.0)
        self.assertEqual(
            scheduler.edge_dependence.leased_targets(101.0), {88})

    def test_external_rejection_preserves_fallback_replay_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            scheduler = AdaptiveHybridScheduler(2)
            scheduler.observe(
                seed,
                coverage_delta=0,
                interesting_cases=0,
                elapsed=0.1,
                killed=False,
                strategy=0,
                telemetry=SolverTelemetry.from_mapping({
                    "open_branches": [91, 92],
                }),
            )
            scheduler.prefix_dag_enabled = False
            scheduler.cstg_enabled = False
            scheduler.edge_dependence_enabled = False

            jobs = scheduler.replay_candidates(
                1,
                cooldown=0.0,
                now=100.0,
                external_admission=lambda _job: False,
            )

            self.assertEqual(jobs, [])
            self.assertEqual(scheduler.replay[seed].target_cursor, 0)
            self.assertEqual(scheduler.replay[seed].last_replay, 0.0)
            self.assertEqual(
                scheduler.edge_dependence.leased_targets(101.0), set())

    def test_rejected_concurrency_job_does_not_enter_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            distance_path = os.path.join(tmp, "distance")
            Path(distance_path).write_text("11 0\n", encoding="utf-8")
            first = os.path.join(tmp, "first")
            second = os.path.join(tmp, "second")
            Path(first).write_bytes(b"first")
            Path(second).write_bytes(b"second")
            guidance = HierarchicalConcurrencyGuidance(distance_path)
            for path in (first, second):
                guidance.observe(
                    path,
                    SolverTelemetry.from_mapping({
                        "branch_trace": [[1, 2, 900, 11, 1, 0]],
                    }),
                    reward=0.2,
                    now=10.0,
                )
            scheduler = AdaptiveHybridScheduler(2)
            scheduler.prefix_dag_enabled = False
            scheduler.edge_dependence_enabled = False
            scheduler.concurrency_guidance = guidance

            jobs = scheduler.replay_candidates(
                2, cooldown=0.0, now=100.0)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].target_branch, 900)
            committed = [
                record for record in guidance.records.values()
                if record.last_scheduled == 100.0
            ]
            untouched = [
                record for record in guidance.records.values()
                if record.last_scheduled == 0.0
            ]
            self.assertEqual(len(committed), 1)
            self.assertEqual(len(untouched), 1)

    def test_rejected_action_group_does_not_shadow_viable_fallback(self):
        scheduler = AdaptiveHybridScheduler(2)
        scheduler.prefix_dag.nodes[77] = PrefixNode(
            77, 1, 10, 0, "open")
        scheduler.prefix_dag.nodes[99] = PrefixNode(
            99, 1, 20, 0, "open")
        scheduler.prefix_dag.nodes[88] = PrefixNode(
            88, 1, 30, 0, "open")
        scheduler.cstg.transitions[77] = CSTGTransition(1, 77, 77, 10)
        scheduler.cstg.transitions[88] = CSTGTransition(1, 88, 88, 30)
        prefix_jobs = [
            ReplayJob("first", 77, ((77, "solve"), (99, "solve"))),
            ReplayJob("shared", 88, ((88, "solve"), (99, "solve"))),
        ]
        cstg_jobs = [
            ReplayJob("duplicate", 77, ((77, "solve"),)),
            ReplayJob("shared", 88, ((88, "solve"),)),
        ]
        scheduler.prefix_dag.select = lambda *args, **kwargs: prefix_jobs
        scheduler.cstg.select = lambda *args, **kwargs: cstg_jobs

        jobs = scheduler.target_candidates(2, cooldown=0.0, now=100.0)

        self.assertEqual(
            [(job.path, job.target_branch) for job in jobs],
            [("first", 77), ("shared", 88)],
        )
        self.assertEqual(scheduler.prefix_dag.nodes[88].attempts, 0)
        self.assertEqual(scheduler.cstg.transitions[88].last_scheduled, 100.0)

    def test_targeted_replay_is_not_split_into_focus_regions(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"0123456789abcdef")
            items = _build_work_items(
                [seed],
                target_count=4,
                diversity=True,
                focus_parts=4,
                target_branches={seed: 123},
            )
        self.assertEqual(items, [(seed, None, 123)])

    def test_targeted_replay_carries_s2f_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"0123456789abcdef")
            actions = ((123, "solve"), (456, "sample"), (789, "skip"))
            items = _build_work_items(
                [seed],
                target_count=4,
                diversity=True,
                focus_parts=4,
                target_branches={seed: 123},
                target_actions={seed: actions},
            )
        self.assertEqual(items, [(seed, None, 123, actions)])

    def test_s2f_action_file_filters_and_writes_rows(self):
        actions = _normalize_s2f_actions([
            [10, "SOLVE"], [10, "sample"], [20, "sample"],
            [30, "skip"], [0, "solve"], [40, "bad"],
        ])
        self.assertEqual(actions, ((10, "solve"), (20, "sample"), (30, "skip")))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "actions")
            self.assertTrue(_write_s2f_action_file(path, actions))
            self.assertEqual(
                Path(path).read_text(encoding="ascii"),
                "10 solve\n20 sample\n30 skip\n",
            )

    def test_compact_focus_set_uses_comparison_taint_offsets(self):
        telemetry = SolverTelemetry.from_mapping({
            "comparison_taints": [
                [10, 11, 3, 4, 6, 1, 1],
                [20, 21, 9, 100, 200, 1, 1],
                [30, 31, 1, 9, 9, 0, 0],
            ],
        })
        self.assertEqual(
            _comparison_taint_offsets(telemetry, max_span=8),
            [4, 5, 6, 100, 200],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "focus")
            self.assertTrue(_write_compact_focus_set(
                path, [4, 4, 2, 9], max_entries=8))
            self.assertEqual(Path(path).read_text(encoding="utf-8"),
                             "2\n4\n9\n")

    def test_rewarded_hard_seed_can_be_replayed_after_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            scheduler = AdaptiveHybridScheduler(2)
            scheduler.context(seed, seed_type="normal")
            telemetry = SolverTelemetry.from_mapping({
                "symbolic_branches": 10,
                "interesting_branches": 8,
                "solver_queries": 2,
                "solver_sat": 1,
                "generated": 1,
                "input_bytes": 4,
                "max_dependency_bytes": 4,
                "path_hash": 123,
                "open_branches": [456, 789],
            })
            scheduler.observe(
                seed,
                coverage_delta=4,
                interesting_cases=1,
                elapsed=1.0,
                killed=False,
                strategy=0,
                telemetry=telemetry,
            )
            jobs = scheduler.replay_candidates(1, cooldown=30.0, now=100.0)
            self.assertEqual([(job.path, job.target_branch) for job in jobs],
                             [(seed, 456)])
            self.assertEqual(
                scheduler.replay_candidates(1, cooldown=30.0, now=110.0),
                [],
            )

            scheduler.observe(
                seed,
                coverage_delta=1,
                interesting_cases=1,
                elapsed=1.0,
                killed=False,
                strategy=1,
                telemetry=SolverTelemetry.from_mapping({
                    "target_branch": 456,
                    "target_reached": True,
                    "open_branches": [789],
                }),
            )
            follow_up = scheduler.replay_candidates(
                1, cooldown=30.0, now=140.0)
            self.assertEqual(
                [(job.path, job.target_branch) for job in follow_up],
                [(seed, 789)],
            )
            self.assertEqual(scheduler.strategies.pulls, [1, 1])

    def test_policy_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            scheduler = AdaptiveHybridScheduler(2, state)
            context = scheduler.context("seed", base_score=50.0)
            scheduler.model.update(context.vector, 0.75)
            scheduler.strategies.update(1, 0.75, 1.0)
            scheduler.save()

            restored = AdaptiveHybridScheduler(2, state)
            self.assertEqual(restored.model.observations, 1)
            self.assertEqual(restored.strategies.pulls, [0, 1])
            self.assertTrue(restored.simifuzz_enabled)

    def test_scheduler_persists_pareto_corpus_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state.json")
            seed = os.path.join(tmp, "seed")
            Path(seed).write_bytes(b"seed")
            scheduler = AdaptiveHybridScheduler(2, state)
            scheduler.context(seed)
            scheduler.observe(
                seed,
                coverage_delta=3,
                interesting_cases=1,
                elapsed=0.2,
                killed=False,
                strategy=0,
                telemetry=SolverTelemetry.from_mapping({
                    "path_hash": 99,
                    "data_features": [[7, 4, 8]],
                    "branch_trace": [[1, 2, 3, 10, 1, 0]],
                    "string_solver_queries": 2,
                    "string_solver_verified": 1,
                }),
            )
            self.assertIn(seed, scheduler.pareto_corpus.entries)
            self.assertEqual(scheduler.snapshot()["schema"], 14)
            scheduler.save()

            restored = AdaptiveHybridScheduler(2, state)
            self.assertIn(seed, restored.pareto_corpus.entries)
            entry = restored.pareto_corpus.entries[seed]
            self.assertEqual(entry.edge_features, 3)
            self.assertEqual(
                (entry.string_queries, entry.string_verified), (2, 1))

    def test_strategy_executor_labels(self):
        self.assertEqual(_strategy_executor(0), "exact")
        self.assertEqual(_strategy_executor(1), "tailored")
        self.assertEqual(_strategy_executor(5), "tailored")
        self.assertEqual(_strategy_executor(6), "sampling")
        self.assertEqual(_strategy_executor(99), "exact")


if __name__ == "__main__":
    unittest.main()
