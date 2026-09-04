# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from analyze_parallel_profiles import load_run  # noqa: E402


def test_master_triage_detail_is_parsed(tmp_path):
    run_dir = tmp_path / "run"
    profile_dir = run_dir / "profiles"
    profile_dir.mkdir(parents=True)
    (run_dir / "benchmark_data.csv").write_text(
        "\n".join([
            "mode,status,np,afl_instances,wall_time_sec,edges_found,"
            "coverage_auc,afl_execs_per_sec,symcc_peer_sync_complete",
            "hybrid,success,16,8,100,5700,5300,240000,1",
        ]) + "\n",
        encoding="utf-8",
    )
    (profile_dir / "phase_timing_rank1.csv").write_text(
        "1,10,1,0.5,0.5,7,1,0.5\n", encoding="utf-8")
    (profile_dir / "redun_rank1.csv").write_text(
        "1,100,20,0,20,0,0,0,3\n", encoding="utf-8")
    (profile_dir / "redun_master.csv").write_text(
        "accepted\n9\n", encoding="utf-8")
    (profile_dir / "mpi_master.log").write_text(
        "\n".join([
            "[PROF] scan=3.00s/30x dispatch=1.00s/10x "
            "recv=0.25s/5x(10KB) triage=4.00s/8x idle=2.00s",
            "[PROF-TRIAGE] adaptive.prefix_dag=2.20s "
            "adaptive_scheduler=2.29s batch_core=4.99s",
        ]) + "\n",
        encoding="utf-8",
    )

    row = load_run(run_dir)

    assert row["master_triage_detail_adaptive_prefix_dag_s"] == 2.20
    assert row["master_triage_detail_adaptive_scheduler_s"] == 2.29
    assert row["master_scan_ms_per_call"] == 100.0
