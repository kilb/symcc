# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

import json
import math
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.parallel_scale_model import (  # noqa: E402
    CoverageObservation,
    ThroughputObservation,
    combine_ceilings,
    fit_coverage_saturation,
    fit_usl,
    universal_capacity,
)
from benchmark.analyze_parallel_scaling import _load_rows, _summaries  # noqa: E402


def test_usl_fit_recovers_known_curve_and_peak():
    scale = 12.0
    sigma = 0.08
    kappa = 0.003
    rows = [
        ThroughputObservation(
            n, universal_capacity(n, scale, sigma, kappa))
        for n in (1, 2, 4, 8, 16, 32)
    ]
    fitted = fit_usl(rows, maximum_parallelism=256)
    assert fitted.scale == pytest.approx(scale, rel=0.03)
    assert fitted.contention == pytest.approx(sigma, abs=0.01)
    assert fitted.coherency == pytest.approx(kappa, rel=0.20)
    assert fitted.r_squared > 0.999
    assert fitted.analytic_peak == pytest.approx(
        math.sqrt((1.0 - sigma) / kappa), rel=0.10)


def test_coverage_fit_and_combined_ceiling_are_finite():
    rows = [
        CoverageObservation(n, 100.0 + 220.0 * (1.0 - math.exp(-0.12 * n)))
        for n in (1, 2, 4, 8, 16, 32)
    ]
    coverage = fit_coverage_saturation(
        rows, seed_edges=100.0, total_edges=500.0,
        maximum_parallelism=256, minimum_edge_gain=1.0)
    assert coverage.asymptotic_edges == pytest.approx(320.0, rel=0.05)
    assert coverage.rate == pytest.approx(0.12, rel=0.10)
    assert coverage.r_squared > 0.99

    throughput = fit_usl([
        ThroughputObservation(n, universal_capacity(n, 10.0, 0.1, 0.002))
        for n in (1, 2, 4, 8, 16, 32)
    ], maximum_parallelism=256)
    combined = combine_ceilings(
        throughput, coverage, resource_ceiling=96)
    assert 1 <= combined.recommended_parallelism <= 96
    assert combined.limiting_factors


def test_models_reject_underdetermined_input():
    with pytest.raises(ValueError, match="four"):
        fit_usl([
            ThroughputObservation(1, 1.0),
            ThroughputObservation(2, 2.0),
            ThroughputObservation(4, 4.0),
        ])
    with pytest.raises(ValueError, match="four"):
        fit_coverage_saturation(
            [
                CoverageObservation(1, 100.0),
                CoverageObservation(2, 120.0),
                CoverageObservation(4, 140.0),
            ],
            seed_edges=50.0,
            total_edges=500.0,
        )


def test_linear_usl_has_no_artificial_horizon_ceiling():
    fitted = fit_usl(
        [ThroughputObservation(n, 7.0 * n) for n in (1, 2, 4, 8, 16)],
        maximum_parallelism=64,
    )
    assert fitted.contention == pytest.approx(0.0, abs=1e-9)
    assert fitted.coherency == pytest.approx(0.0, abs=1e-9)
    assert fitted.doubling_ceiling is None


def test_models_reject_zero_variance_quality_claims():
    with pytest.raises(ValueError, match="zero weighted variance"):
        fit_usl([
            ThroughputObservation(n, 10.0) for n in (1, 2, 4, 8)
        ])
    with pytest.raises(ValueError, match="zero weighted variance"):
        fit_coverage_saturation(
            [
                CoverageObservation(1, 100.0),
                CoverageObservation(2, 120.0),
                CoverageObservation(4, 120.0),
                CoverageObservation(8, 120.0),
            ],
            seed_edges=50.0,
            total_edges=500.0,
        )


def test_repeated_round_noise_is_retained_in_fit_quality():
    throughput_rows = [
        ThroughputObservation(
            n,
            universal_capacity(n, 12.0, 0.08, 0.003) + offset,
        )
        for n in (1, 2, 4, 8, 16, 32)
        for offset in (-0.5, 0.5)
    ]
    throughput = fit_usl(
        throughput_rows, maximum_parallelism=256, minimum_r_squared=0.0
    )
    assert throughput.r_squared < 1.0
    assert throughput.rmse > 0.0

    coverage_rows = [
        CoverageObservation(
            n,
            100.0 + 220.0 * (1.0 - math.exp(-0.12 * (n - 1))) + offset,
        )
        for n in (1, 2, 4, 8, 16, 32)
        for offset in (-0.5, 0.5)
    ]
    coverage = fit_coverage_saturation(
        coverage_rows,
        seed_edges=50.0,
        total_edges=500.0,
        maximum_parallelism=256,
        minimum_r_squared=0.0,
    )
    assert coverage.r_squared < 1.0
    assert coverage.rmse > 0.0


def test_coverage_fit_quality_retains_baseline_round_noise():
    rows = [CoverageObservation(1, value) for value in (90.0, 110.0)]
    rows.extend(
        CoverageObservation(
            n,
            100.0 + 220.0 * (1.0 - math.exp(-0.12 * (n - 1))),
        )
        for n in (2, 4, 8, 16, 32)
    )
    coverage = fit_coverage_saturation(
        rows,
        seed_edges=50.0,
        total_edges=500.0,
        maximum_parallelism=256,
        minimum_r_squared=0.0,
    )
    assert coverage.r_squared < 1.0
    assert coverage.rmse > 3.0


def test_acceptance_ratio_uses_counts_and_rejects_impossible_unique(tmp_path):
    summaries = _summaries([
        {
            "np": 2.0,
            "workers": 1.0,
            "masters": 1.0,
            "afl_instances": 0.0,
            "parallelism": 1.0,
            "generated_rate": 100.0,
            "unique_rate": 50.0,
            "afl_rate": 0.0,
            "symcc_rate": 100.0,
            "generated": 100,
            "unique": 50,
            "edges": 10,
            "random_seed": 1,
            "wall_budget": 1.0,
        },
        {
            "np": 2.0,
            "workers": 1.0,
            "masters": 1.0,
            "afl_instances": 0.0,
            "parallelism": 1.0,
            "generated_rate": 10.0,
            "unique_rate": 10.0,
            "afl_rate": 0.0,
            "symcc_rate": 10.0,
            "generated": 100,
            "unique": 100,
            "edges": 11,
            "random_seed": 2,
            "wall_budget": 10.0,
        },
    ])
    assert summaries[0]["acceptance_ratio"] == pytest.approx(0.75)

    csv_path = tmp_path / "impossible.csv"
    csv_path.write_text(
        "target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total,run_id,random_seed\n"
        "case,mpi,success,2,1,1,1,4,5,10,100,run-1,1\n",
        encoding="ascii",
    )
    with pytest.raises(ValueError, match="unique corpus count exceeds"):
        _load_rows([csv_path], "case", "mpi", 90)


def test_models_reject_invalid_observations_before_numeric_fitting():
    with pytest.raises(ValueError, match="invalid throughput observation"):
        fit_usl([
            ThroughputObservation(1, 1.0),
            ThroughputObservation(2, 2.0),
            ThroughputObservation(4, 4.0),
            ThroughputObservation(8, 8.0, weight=float("nan")),
        ])
    with pytest.raises(ValueError, match="invalid coverage observation"):
        fit_coverage_saturation(
            [
                CoverageObservation(1, 100.0),
                CoverageObservation(2, 120.0),
                CoverageObservation(4, 140.0),
                CoverageObservation(8, 160.0, weight=0.0),
            ],
            seed_edges=50.0,
            total_edges=500.0,
        )
    with pytest.raises(ValueError, match="below an observed level"):
        fit_usl(
            [ThroughputObservation(n, float(n)) for n in (1, 2, 4, 8)],
            maximum_parallelism=4,
        )
    with pytest.raises(ValueError, match="minimum_doubling_gain"):
        fit_usl(
            [ThroughputObservation(n, float(n)) for n in (1, 2, 4, 8)],
            minimum_doubling_gain="0.1",
        )
    with pytest.raises(ValueError, match="invalid throughput observation"):
        fit_usl([
            ThroughputObservation(n, 10 ** 10_000)
            for n in (1, 2, 4, 8)
        ])
    with pytest.raises(ValueError, match="invalid throughput observation"):
        fit_usl([
            ThroughputObservation(n, 1e200, weight=1e200)
            for n in (1, 2, 4, 8)
        ])


def test_csv_counts_remain_exact_above_binary_float_precision(tmp_path):
    csv_path = tmp_path / "large-count.csv"
    exact = 9_007_199_254_740_993
    csv_path.write_text(
        "target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total,run_id,random_seed\n"
        f"case,mpi,success,2,1,1,1,{exact},{exact},10,100,run-1,1\n",
        encoding="ascii",
    )
    runs, _reliability, _seed, _total = _load_rows(
        [csv_path], "case", "mpi", 90
    )
    assert runs[0]["generated"] == exact
    assert runs[0]["unique"] == exact

    csv_path.write_text(
        "target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total,run_id,random_seed\n"
        "case,mpi,success,2,1,1,1,1e1000000,1,10,100,run-2,2\n",
        encoding="ascii",
    )
    with pytest.raises(ValueError, match="non-negative integer count"):
        _load_rows([csv_path], "case", "mpi", 90)


def test_coverage_model_rejects_monotone_but_misspecified_curve():
    rows = [
        CoverageObservation(n, edges)
        for n, edges in zip(
            (1, 2, 4, 8, 16),
            (100.0, 800.0, 800.0, 800.0, 1200.0),
        )
    ]
    with pytest.raises(ValueError, match="fit quality"):
        fit_coverage_saturation(
            rows,
            seed_edges=50.0,
            total_edges=2000.0,
            maximum_parallelism=256,
        )


def test_scaling_analysis_preserves_nonmonotone_coverage_results(tmp_path):
    csv_path = tmp_path / "scale.csv"
    rows = [
        (9, 60, 6000, 1800, 190),
        (33, 60, 7200, 2100, 180),
        (130, 60, 7800, 2300, 170),
        (257, 60, 8100, 2400, 165),
    ]
    csv_path.write_text(
        "target,mode,status,np,wall_time_sec,symcc_generated,unique,"
        "edges_found,edges_total\n"
        + "".join(
            f"synthetic,mpi,success,{np_value},{wall},{generated},{unique},"
            f"{edges},768\n"
            for np_value, wall, generated, unique, edges in rows
        ),
        encoding="utf-8",
    )
    output = tmp_path / "analysis"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target",
            "synthetic",
            "--mode",
            "mpi",
            "--output",
            str(output),
            "--seed-edges",
            "2",
            "--total-edges",
            "768",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (output / "parallel_scale_model.json").read_text(encoding="utf-8")
    )
    assert payload["coverage_saturation"] is None
    assert payload["coverage_fit_error"] == "invalid coverage observation"
    assert len(payload["summaries"]) == 4


def test_hybrid_analysis_uses_total_and_component_parallel_axes(tmp_path):
    csv_path = tmp_path / "hybrid.csv"
    csv_path.write_text(
        "target,mode,status,np,num_workers,num_masters,afl_instances,"
        "wall_time_sec,generated,unique,afl_executions,symcc_generated,"
        "edges_found,edges_total\n"
        + "".join(
            f"synthetic,hybrid,success,{np_value},{symcc},1,{afl},10,"
            f"{afl_execs + symcc_generated},{unique},{afl_execs},"
            f"{symcc_generated},{edges},2000\n"
            for np_value, symcc, afl, afl_execs, symcc_generated, unique, edges in (
                (5, 1, 3, 3000, 50, 40, 100),
                (9, 2, 6, 5700, 95, 72, 160),
                (17, 4, 12, 10200, 180, 120, 230),
                (33, 8, 24, 18000, 320, 190, 300),
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "analysis"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "hybrid",
            "--output", str(output),
            "--seed-edges", "50",
            "--total-edges", "2000",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (output / "parallel_scale_model.json").read_text(encoding="utf-8")
    )
    assert payload["parallel_unit"] == "compute-workers(concolic+afl)"
    assert [row["parallelism"] for row in payload["summaries"]] == [4, 8, 16, 32]
    assert [row["concolic_workers"] for row in payload["summaries"]] == [1, 2, 4, 8]
    assert [row["afl_instances"] for row in payload["summaries"]] == [3, 6, 12, 24]
    assert payload["generated_throughput_usl"] is None
    assert "mixes AFL executions" in payload["generated_throughput_fit_error"]
    assert payload["symcc_component_usl"] is not None
    assert payload["afl_component_usl"] is not None
    assert payload["reliability"]["auxiliary_slots_missing"] == 4
    assert any(
        "auxiliary_compute_slots is missing" in reason
        for reason in payload["ceiling"]["decision_reasons"]
    )


def test_hybrid_resource_ceiling_reserves_declared_auxiliary_compute(tmp_path):
    csv_path = tmp_path / "hybrid-auxiliary.csv"
    rows = []
    for np_value, compute, rate, edges in (
        (3, 1, 10, 100),
        (5, 2, 19, 160),
        (9, 4, 34, 250),
        (17, 8, 55, 360),
        (33, 16, 70, 450),
    ):
        for repeat, offset in ((1, -1), (2, 0), (3, 1)):
            afl_executions = compute * 1000 + repeat
            symcc_generated = compute * 100 + repeat
            generated = afl_executions + symcc_generated
            unique = (rate + offset) * 10
            rows.append(
                f"run-{np_value}-{repeat},synthetic,hybrid,success,"
                f"{np_value},{compute},1,{compute},4,{repeat},{repeat},10,10,"
                f"{generated},{unique},{afl_executions},{symcc_generated},"
                f"{edges + offset},1000,1,existing_edges,0,100,100\n"
            )
    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,afl_instances,"
        "auxiliary_compute_slots,round,random_seed,wall_time_sec,"
        "wall_budget_seconds,generated,unique,afl_executions,symcc_generated,"
        "edges_found,edges_total,coverage_measure_ok,"
        "coverage_denominator_kind,coverage_sampled,coverage_sampled_cases,"
        "coverage_total_cases\n"
        + "".join(rows),
        encoding="utf-8",
    )
    output = tmp_path / "analysis"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "hybrid",
            "--output", str(output),
            "--seed-edges", "50",
            "--total-edges", "1000",
            "--physical-cores", "40",
            "--bootstrap-samples", "20",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (output / "parallel_scale_model.json").read_text(encoding="utf-8")
    )
    assert payload["reserved_auxiliary_compute_slots"] == 4
    assert payload["ceiling"]["resource_ceiling"] == 35
    assert payload["ceiling"]["decision_eligible"] is True
    assert "auxiliary" in payload["resource_ceiling_basis"]


@pytest.mark.parametrize(
    ("mode", "physical_cores", "expected"),
    [
        ("mpi", 1, "one MPI coordinator and one compute worker"),
        ("hybrid", 5, "hybrid coordinators and one compute worker"),
    ],
)
def test_resource_ceiling_rejects_budget_without_one_worker(
    tmp_path, mode, physical_cores, expected
):
    csv_path = tmp_path / f"{mode}-insufficient.csv"
    rows = []
    for np_value, compute, rate, edges in (
        (3, 1, 10, 100),
        (5, 2, 19, 160),
        (9, 4, 34, 250),
        (17, 8, 55, 360),
        (33, 16, 70, 450),
    ):
        for repeat, offset in ((1, -1), (2, 0), (3, 1)):
            if mode == "mpi":
                row_np = compute + 1
                workers = compute
                afl = 0
                auxiliary = 0
                afl_executions = 0
            else:
                row_np = np_value
                workers = compute
                afl = compute
                auxiliary = 4
                afl_executions = compute * 1000 + repeat
            symcc_generated = compute * 1000 + repeat
            generated = afl_executions + symcc_generated
            unique = (rate + offset) * 10
            rows.append(
                f"run-{np_value}-{repeat},synthetic,{mode},success,{row_np},"
                f"{workers},1,"
                f"{afl},{auxiliary},{repeat},{repeat},10,10,{generated},{unique},"
                f"{afl_executions},{symcc_generated},{edges + offset},1000,1,"
                "existing_edges,0,100,100\n"
            )
    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,afl_instances,"
        "auxiliary_compute_slots,round,random_seed,wall_time_sec,"
        "wall_budget_seconds,generated,unique,afl_executions,symcc_generated,"
        "edges_found,edges_total,coverage_measure_ok,coverage_denominator_kind,"
        "coverage_sampled,coverage_sampled_cases,coverage_total_cases\n"
        + "".join(rows),
        encoding="utf-8",
    )
    output = tmp_path / "analysis"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", mode,
            "--output", str(output),
            "--seed-edges", "50",
            "--total-edges", "1000",
            "--physical-cores", str(physical_cores),
            "--bootstrap-samples", "20",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (output / "parallel_scale_model.json").read_text(encoding="utf-8")
    )
    assert payload["ceiling"]["decision_eligible"] is False
    assert any(expected in reason for reason in payload["ceiling"]["decision_reasons"])


def test_hybrid_auxiliary_ledger_includes_failed_attempts(tmp_path):
    csv_path = tmp_path / "hybrid-failed-auxiliary.csv"
    rows = []
    for np_value, compute, rate, edges in (
        (3, 1, 10, 100),
        (5, 2, 19, 160),
        (9, 4, 34, 250),
        (17, 8, 55, 360),
        (33, 16, 70, 450),
    ):
        for repeat, offset in ((1, -1), (2, 0), (3, 1)):
            afl_executions = compute * 1000 + repeat
            symcc_generated = compute * 100 + repeat
            rows.append(
                f"run-{np_value}-{repeat},synthetic,hybrid,success,{np_value},"
                f"{compute},1,{compute},4,{repeat},{repeat},10,10,"
                f"{afl_executions + symcc_generated},{(rate + offset) * 10},"
                f"{afl_executions},{symcc_generated},{edges + offset},1000,1,"
                "existing_edges,0,100,100\n"
            )
    rows.append(
        "failed-extra,synthetic,hybrid,failed,3,1,1,1,7,4,4,,,,,,,,,,,\n"
    )
    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,afl_instances,"
        "auxiliary_compute_slots,round,random_seed,wall_time_sec,"
        "wall_budget_seconds,generated,unique,afl_executions,symcc_generated,"
        "edges_found,edges_total,coverage_measure_ok,coverage_denominator_kind,"
        "coverage_sampled,coverage_sampled_cases,coverage_total_cases\n"
        + "".join(rows),
        encoding="utf-8",
    )
    output = tmp_path / "analysis"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "hybrid",
            "--output", str(output),
            "--seed-edges", "50",
            "--total-edges", "1000",
            "--physical-cores", "64",
            "--minimum-success-rate", "0.9",
            "--bootstrap-samples", "20",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (output / "parallel_scale_model.json").read_text(encoding="utf-8")
    )
    assert payload["reliability"]["observed_auxiliary_compute_slots"] == [4, 7]
    assert payload["ceiling"]["decision_eligible"] is False
    assert any(
        "auxiliary compute-slot allocation changes" in reason
        for reason in payload["ceiling"]["decision_reasons"]
    )


def test_scaling_analysis_counts_failures_and_withholds_survivor_fit(tmp_path):
    csv_path = tmp_path / "unreliable.csv"
    header = (
        "target,mode,status,np,round,wall_time_sec,symcc_generated,unique,"
        "edges_found,edges_total\n"
    )
    successful = "".join(
        f"synthetic,mpi,success,{np_value},{round_value},60,"
        f"{np_value * 100},{np_value * 10},{100 + np_value},1000\n"
        for np_value in (2, 3, 5, 9, 17)
        for round_value in (1, 2, 3)
    )
    timed_out = "".join(
        f"synthetic,mpi,timeout,17,{round_value},,,,,\n"
        for round_value in range(4, 34)
    )
    csv_path.write_text(header + successful + timed_out, encoding="utf-8")
    output = tmp_path / "analysis"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "mpi",
            "--output", str(output),
            "--seed-edges", "50",
            "--total-edges", "1000",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (output / "parallel_scale_model.json").read_text(encoding="utf-8")
    )
    assert payload["reliability"]["attempted"] == 45
    assert payload["reliability"]["successful"] == 15
    assert payload["reliability"]["timed_out"] == 30
    assert payload["reliability"]["success_rate"] == pytest.approx(1 / 3)
    assert payload["unique_throughput_usl"] is None
    assert "success rate" in payload["unique_throughput_fit_error"]
    assert payload["coverage_saturation"] is None
    assert payload["ceiling"]["decision_eligible"] is False
    assert payload["ceiling"]["recommended_parallelism"] is None


def test_scaling_analysis_rejects_impossible_role_ledger(tmp_path):
    csv_path = tmp_path / "invalid-roles.csv"
    csv_path.write_text(
        "target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total\n"
        "synthetic,mpi,success,8,8,1,60,100,10,20,100\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "mpi",
            "--output", str(tmp_path / "analysis"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "role ledger" in result.stderr


def test_scaling_decision_reports_bootstrap_uncertainty(tmp_path):
    csv_path = tmp_path / "replicated.csv"
    rows = []
    for np_value, workers, rate, edges in (
        (2, 1, 10, 100),
        (3, 2, 19, 160),
        (5, 4, 34, 250),
        (9, 8, 55, 360),
        (17, 16, 70, 450),
    ):
        for round_value, offset in ((1, -1), (2, 0), (3, 1)):
            unique = (rate + offset) * 10
            rows.append(
                f"synthetic-{np_value}-{round_value},synthetic,mpi,success,"
                f"{np_value},{workers},1,{round_value},"
                f"{round_value},10,10,{unique * 2},{unique},"
                f"{edges + offset},1000,1,existing_edges,0,100,100\n"
            )
    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,round,random_seed,"
        "wall_time_sec,wall_budget_seconds,symcc_generated,unique,edges_found,"
        "edges_total,coverage_measure_ok,coverage_denominator_kind,"
        "coverage_sampled,coverage_sampled_cases,coverage_total_cases\n"
        + "".join(rows),
        encoding="utf-8",
    )
    output = tmp_path / "analysis"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "mpi",
            "--output", str(output),
            "--seed-edges", "50",
            "--total-edges", "1000",
            "--physical-cores", "64",
            "--bootstrap-samples", "20",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(
        (output / "parallel_scale_model.json").read_text(encoding="utf-8")
    )
    uncertainty = payload["model_uncertainty"]
    assert uncertainty["requested_replicates"] == 20
    assert uncertainty["successful_replicates"] >= 16
    assert uncertainty["unique_resamples_evaluated"] <= 10
    assert uncertainty["method"] == (
        "paired cluster bootstrap over complete random-seed blocks"
    )
    assert uncertainty["independent_seed_blocks"] == 3
    assert len(uncertainty["unique_usl"]["contention_ci95"]) == 2
    assert len(uncertainty["recommended_parallelism_ci95"]) == 2
    assert payload["ceiling"]["decision_eligible"] is True
    assert payload["ceiling"]["recommended_parallelism"] is not None


def _write_mpi_scale_campaign(
    path,
    *,
    seed_for=lambda _np, repeat: repeat,
    budget_for=lambda _np, _repeat: 10,
    edges_for=lambda workers, repeat: 80 + workers * 20 + repeat,
):
    rows = []
    for np_value, workers, rate in (
        (2, 1, 10),
        (3, 2, 19),
        (5, 4, 34),
        (9, 8, 55),
        (17, 16, 70),
    ):
        for repeat in (1, 2, 3):
            unique = rate * 10 + repeat
            rows.append(
                f"run-{np_value}-{repeat},synthetic,mpi,success,{np_value},"
                f"{workers},1,{repeat},{seed_for(np_value, repeat)},10,"
                f"{budget_for(np_value, repeat)},{unique * 2},{unique},"
                f"{edges_for(workers, repeat)},1000,1,existing_edges,0,100,100\n"
            )
    path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,round,random_seed,"
        "wall_time_sec,wall_budget_seconds,symcc_generated,unique,edges_found,"
        "edges_total,coverage_measure_ok,coverage_denominator_kind,"
        "coverage_sampled,coverage_sampled_cases,coverage_total_cases\n"
        + "".join(rows),
        encoding="utf-8",
    )


def _analyze_scale_campaign(csv_path, output, *, mode="mpi"):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", mode,
            "--output", str(output),
            "--seed-edges", "50",
            "--total-edges", "1000",
            "--bootstrap-samples", "20",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(
        (output / "parallel_scale_model.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    ("campaign_options", "reason"),
    [
        ({"seed_for": lambda _np, _repeat: 7}, "reuse a random seed"),
        (
            {"budget_for": lambda np_value, _repeat: 20 if np_value == 17 else 10},
            "planned wall budgets differ",
        ),
        (
            {
                "edges_for": lambda workers, repeat: (
                    400 - workers * 10 + repeat
                )
            },
            "coverage-saturation model is unavailable",
        ),
    ],
)
def test_scaling_decision_rejects_invalid_evidence_design(
    tmp_path, campaign_options, reason,
):
    csv_path = tmp_path / "invalid-evidence.csv"
    _write_mpi_scale_campaign(csv_path, **campaign_options)
    payload = _analyze_scale_campaign(csv_path, tmp_path / "analysis")
    assert payload["ceiling"]["decision_eligible"] is False
    assert payload["ceiling"]["recommended_parallelism"] is None
    assert any(
        reason in item for item in payload["ceiling"]["decision_reasons"]
    )


def test_scaling_decision_rejects_tuple_only_coverage_denominator(tmp_path):
    csv_path = tmp_path / "tuple-only.csv"
    _write_mpi_scale_campaign(csv_path)
    csv_path.write_text(
        csv_path.read_text(encoding="utf-8").replace(
            ",1,existing_edges,0,", ",0,unavailable,0,"
        ),
        encoding="utf-8",
    )

    payload = _analyze_scale_campaign(csv_path, tmp_path / "analysis")

    assert payload["schema"] == "symcc-parallel-scale-analysis-v6"
    assert payload["ceiling"]["decision_eligible"] is False
    assert payload["ceiling"]["recommended_parallelism"] is None
    assert any(
        "existing-edge coverage measurement is invalid" in reason
        for reason in payload["ceiling"]["decision_reasons"]
    )


def test_scaling_decision_rejects_corpus_sampled_endpoint_coverage(tmp_path):
    csv_path = tmp_path / "sampled.csv"
    _write_mpi_scale_campaign(csv_path)
    csv_path.write_text(
        csv_path.read_text(encoding="utf-8").replace(
            ",existing_edges,0,100,100\n",
            ",existing_edges,1,50,100\n",
        ),
        encoding="utf-8",
    )

    payload = _analyze_scale_campaign(csv_path, tmp_path / "analysis")

    assert payload["ceiling"]["decision_eligible"] is False
    assert payload["ceiling"]["recommended_parallelism"] is None
    assert any(
        "endpoint coverage was corpus-sampled" in reason
        for reason in payload["ceiling"]["decision_reasons"]
    )


def test_hybrid_scaling_decision_requires_a_fixed_allocation_ray(tmp_path):
    csv_path = tmp_path / "hybrid-confounded.csv"
    rows = []
    allocations = (
        (5, 1, 3),
        (9, 2, 6),
        (17, 8, 8),
        (33, 8, 24),
        (65, 16, 48),
    )
    for np_value, symcc, afl in allocations:
        for repeat in (1, 2, 3):
            afl_executions = afl * 1000 + repeat
            symcc_generated = symcc * 100 + repeat
            generated = afl_executions + symcc_generated
            rows.append(
                f"run-{np_value}-{repeat},synthetic,hybrid,success,{np_value},"
                f"{symcc},1,{afl},{repeat},{repeat},10,10,{generated},"
                f"{np_value * 10},{afl_executions},{symcc_generated},"
                f"{100 + np_value * 5 + repeat},1000\n"
            )
    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,afl_instances,"
        "round,random_seed,wall_time_sec,wall_budget_seconds,generated,unique,"
        "afl_executions,symcc_generated,edges_found,edges_total\n"
        + "".join(rows),
        encoding="utf-8",
    )
    payload = _analyze_scale_campaign(
        csv_path, tmp_path / "analysis", mode="hybrid",
    )
    assert payload["ceiling"]["decision_eligible"] is False
    assert any(
        "allocation ratio changes" in reason
        for reason in payload["ceiling"]["decision_reasons"]
    )


def test_scaling_analysis_rejects_missing_counters_and_duplicate_runs(tmp_path):
    missing = tmp_path / "missing.csv"
    missing.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total\n"
        "run-1,synthetic,mpi,success,2,1,1,10,100,,20,100\n",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(ROOT / "benchmark/analyze_parallel_scaling.py"),
        str(missing),
        "--target", "synthetic",
        "--mode", "mpi",
        "--output", str(tmp_path / "missing-output"),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "missing unique" in result.stderr

    duplicate = tmp_path / "duplicate.csv"
    row = "run-1,synthetic,mpi,success,2,1,1,10,100,10,20,100\n"
    duplicate.write_text(missing.read_text(encoding="utf-8").splitlines()[0] + "\n" + row + row)
    command[2] = str(duplicate)
    command[-1] = str(tmp_path / "duplicate-output")
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "duplicate run_id" in result.stderr


def test_scaling_analysis_rejects_mixed_coverage_universes(tmp_path):
    csv_path = tmp_path / "mixed.csv"
    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total\n"
        "run-1,synthetic,mpi,success,2,1,1,10,100,10,20,100\n"
        "run-2,synthetic,mpi,success,3,2,1,10,200,20,30,200\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "mpi",
            "--output", str(tmp_path / "output"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "coverage universes disagree" in result.stderr

    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total\n"
        "run-1,synthetic,mpi,success,2,1,1,10,100,10,101,100\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "mpi",
            "--output", str(tmp_path / "impossible-output"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "edge count exceeds coverage universe" in result.stderr

    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total\n"
        "run-1,synthetic,mpi,success,2,1,1,10,100.5,10,20,100\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "mpi",
            "--output", str(tmp_path / "fractional-output"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "non-negative integer count" in result.stderr

    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,wall_time_sec,"
        "symcc_generated,unique,edges_found,edges_total\n"
        "run-1,synthetic,mpi,success,2,1,1,10,100,10,20,100\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "mpi",
            "--output", str(tmp_path / "conflict-output"),
            "--total-edges", "101",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "conflicts with the CSV coverage universe" in result.stderr

    csv_path.write_text(
        "run_id,target,mode,status,np,num_workers,num_masters,afl_instances,"
        "wall_time_sec,generated,unique,afl_executions,symcc_generated,"
        "edges_found,edges_total\n"
        "run-1,synthetic,hybrid,success,3,1,1,1,10,101,10,90,10,20,100\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmark/analyze_parallel_scaling.py"),
            str(csv_path),
            "--target", "synthetic",
            "--mode", "hybrid",
            "--output", str(tmp_path / "hybrid-ledger-output"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "must equal AFL executions plus SymCC candidates" in result.stderr
