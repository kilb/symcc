# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))
sys.path.insert(0, str(ROOT / "benchmark"))

from check_executable_loop_summary_transfer_oracles import concrete_loop  # noqa: E402
from live_state_search import (  # noqa: E402
    LiveProgramGraph,
    location_name,
    search_decision_token,
)


def _summary_control_program() -> dict:
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "functions": {
            "main": {
                "entry": "entry",
                "blocks": {
                    "entry": [{
                        "op": "loop_summary_transfer",
                        "fallback": "loop",
                        "target": "exit",
                        "site": 777,
                    }],
                    "loop": [{"op": "jump", "target": "exit"}],
                    "exit": [{"op": "halt", "value": 0}],
                },
            },
        },
    }


def test_program_graph_tracks_summary_and_fallback_without_fake_branch() -> None:
    graph = LiveProgramGraph(
        _summary_control_program(),
        path_cover_enabled=True,
        cbc_enabled=True,
        cgs_enabled=True,
    )
    entry = location_name("main", "entry")
    assert graph.local_adjacency["main"][entry] == {
        location_name("main", "loop"),
        location_name("main", "exit"),
    }
    assert search_decision_token(777, True) not in graph._path_cover_decisions
    assert search_decision_token(777, False) not in graph._path_cover_decisions
    assert not graph._cbc_decisions
    assert not graph._cgs_targets


def test_summary_control_graph_is_deterministic() -> None:
    first = LiveProgramGraph(_summary_control_program())
    second = LiveProgramGraph(_summary_control_program())
    assert first.adjacency == second.adjacency
    assert first.local_adjacency == second.local_adjacency


def test_independent_source_loop_oracle_covers_written_and_zero_trip_loads() -> None:
    assert concrete_loop(0, 1, 3, 0x1234) == (13994, True)
    assert concrete_loop(8, 3, 3, 0x1234) == (9898, True)
    assert concrete_loop(1, 1, 1, 0x1234) == (54, False)
    assert concrete_loop(0, 0, 0, 0x1234) == (0, False)
