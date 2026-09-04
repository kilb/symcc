# RUN: python3 %s

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmark"))

from check_interprocedural_heap_effect_oracles import (  # noqa: E402
    Interval,
    covers,
    mutation_oracle,
    run_oracle,
)
from generate_interprocedural_heap_effect_fixture import generate  # noqa: E402
from benchmark_interprocedural_heap_effect_summary import (  # noqa: E402
    benchmark,
)


def test_interval_cover_is_half_open() -> None:
    assert covers(Interval(1, 3), Interval(2, 2))
    assert not covers(Interval(1, 2), Interval(2, 2))


def test_certificate_mutations_fail_closed() -> None:
    assert mutation_oracle() == 8


def test_finite_domain_oracle_closes() -> None:
    result = run_oracle()
    assert result["all_passed"] is True
    assert result["interval_instantiations"] > result["accepted_covers"]
    assert result["noncover_rejections"] > 0


def test_generator_has_callsite_local_loads() -> None:
    fixture = generate(64)
    assert fixture.count("call void @generated_initialize") == 64
    assert fixture.count("load i8") == 64
    assert "ret i8 %sum63" in fixture


def test_reference_benchmark_preserves_relation_boundary() -> None:
    result = benchmark(callsites=8, repeats=3, iterations=1)
    assert result["all_certificates_valid"] is True
    assert result["analytic_relation_checks"] == {
        "context_insensitive_cross_product": 64,
        "callsite_instantiated": 8,
    }
