# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from concolic_engine import SymSanEngine  # noqa: E402


def test_symsan_preserves_target_arguments_and_replaces_input_marker(monkeypatch):
    monkeypatch.setenv("SYMSAN_FGTEST", "/opt/symsan/fgtest")
    engine = SymSanEngine()
    command, _env, feed_stdin = engine.wrap_run(
        ["/work/target_symsan", "--mode", "file name", "@@", ""],
        "/work/seed",
        "/work/out",
        {},
        False,
        30,
    )
    assert command == [
        "timeout", "-k", "5", "30", "/opt/symsan/fgtest",
        "/work/target_symsan", "/work/seed", "--",
        "--mode", "file name", "/work/seed", "",
    ]
    assert feed_stdin is False


def test_symsan_keeps_legacy_two_argument_driver_contract(monkeypatch):
    monkeypatch.setenv("SYMSAN_FGTEST", "fgtest")
    command, _env, _feed_stdin = SymSanEngine().wrap_run(
        ["/work/target_symsan"], "/work/seed", "/work/out", {}, True, 5)
    assert command[-3:] == ["fgtest", "/work/target_symsan", "/work/seed"]
