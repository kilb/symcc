#!/usr/bin/env python3
"""Validate LLVM lowering artifacts and execute supported continuation IR."""

from __future__ import annotations

import argparse
import copy
import json
import tempfile

from distributed_state import LiveStateStore
from live_continuation import LiveContinuationExecutor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact")
    parser.add_argument("--input-hex", default="")
    parser.add_argument("--expect-values", default="")
    parser.add_argument("--expect-rejected", default="")
    parser.add_argument("--expect-runtime-error", default="")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--expect-zero-forks", action="store_true")
    parser.add_argument(
        "--expect-executable-loop-summary-transfer",
        action="store_true",
    )
    parser.add_argument(
        "--enable-loop-summary-transfer",
        action="store_true",
    )
    parser.add_argument(
        "--expect-loop-summary-transfer-applied",
        action="store_true",
    )
    parser.add_argument(
        "--expect-loop-summary-transfer-fallback",
        action="store_true",
    )
    parser.add_argument("--expect-max-steps", type=int)
    parser.add_argument("--expect-input-buffer", action="store_true")
    parser.add_argument("--expect-stack", action="store_true")
    parser.add_argument("--expect-heap", action="store_true")
    parser.add_argument("--expect-heap-pool", action="store_true")
    parser.add_argument("--expect-heap-lifetime-pointer-union", action="store_true")
    parser.add_argument(
        "--expect-collective-heap-union-initialization",
        action="store_true",
    )
    parser.add_argument(
        "--expect-guard-correlated-heap-union-initialization",
        action="store_true",
    )
    parser.add_argument(
        "--expect-memoryssa-aa-heap-initialization",
        action="store_true",
    )
    parser.add_argument(
        "--expect-memoryssa-aa-no-modref",
        action="store_true",
    )
    parser.add_argument(
        "--expect-interprocedural-heap-effect",
        action="store_true",
    )
    parser.add_argument(
        "--expect-interprocedural-allocator-effect",
        action="store_true",
    )
    parser.add_argument(
        "--expect-interprocedural-argument-effect",
        action="store_true",
    )
    parser.add_argument(
        "--expect-interprocedural-noalias-skip",
        action="store_true",
    )
    parser.add_argument("--expect-symbolic-region-effect", action="store_true")
    parser.add_argument("--expect-dynamic-byte-lane-cover", action="store_true")
    parser.add_argument(
        "--expect-loop-memoryphi-byte-lane-induction",
        action="store_true",
    )
    parser.add_argument(
        "--expect-strided-loop-memoryphi-byte-lane-induction",
        action="store_true",
    )
    parser.add_argument(
        "--expect-conditional-loop-memoryphi-byte-lane-induction",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multilatch-loop-memoryphi-fixed-point",
        action="store_true",
    )
    parser.add_argument(
        "--expect-ordered-multilatch-writer-transfer",
        action="store_true",
    )
    parser.add_argument(
        "--expect-nested-loop-memoryphi-summary-composition",
        action="store_true",
    )
    parser.add_argument(
        "--expect-nested-loop-memoryphi-last-write-value-summary",
        action="store_true",
    )
    parser.add_argument(
        "--expect-nested-loop-memoryphi-two-dimensional-affine-summary",
        action="store_true",
    )
    parser.add_argument(
        "--expect-nested-loop-memoryphi-affine-symbolic-value-summary",
        action="store_true",
    )
    parser.add_argument(
        "--expect-nested-loop-memoryphi-piecewise-affine-value-summary",
        action="store_true",
    )
    parser.add_argument(
        "--expect-nested-loop-memoryphi-decision-dag-value-summary",
        action="store_true",
    )
    parser.add_argument("--expect-symbolic-region-copy", action="store_true")
    parser.add_argument("--expect-dynamic-byte-lane-noalias-skip", action="store_true")
    parser.add_argument("--expect-nullable-heap", action="store_true")
    parser.add_argument("--expect-alias", action="store_true")
    parser.add_argument("--expect-alias-index-range")
    parser.add_argument("--expect-pointer-union", action="store_true")
    parser.add_argument("--expect-cross-function-pointer", action="store_true")
    parser.add_argument("--expect-indirect-call", action="store_true")
    parser.add_argument("--expect-external-summary", action="store_true")
    parser.add_argument("--expect-declarative-pure-external", action="store_true")
    parser.add_argument("--expect-region-summary", action="store_true")
    parser.add_argument("--expect-string-summary", action="store_true")
    parser.add_argument("--expect-pointer-search", action="store_true")
    parser.add_argument("--expect-string-copy", action="store_true")
    parser.add_argument("--expect-ub-guards", action="store_true")
    parser.add_argument("--expect-infeasible", action="store_true")
    parser.add_argument("--expect-pointer-memory", action="store_true")
    parser.add_argument("--expect-function-pointer-memory", action="store_true")
    parser.add_argument("--expect-pointer-table", action="store_true")
    parser.add_argument("--expect-pointer-memory-merge", action="store_true")
    parser.add_argument("--expect-nounwind-invoke", action="store_true")
    parser.add_argument("--expect-cleanup-exception", action="store_true")
    parser.add_argument("--expect-exception-ops", action="store_true")
    parser.add_argument("--expect-unwind-call", action="store_true")
    parser.add_argument("--expect-typed-exception", action="store_true")
    parser.add_argument("--expect-exception-lifecycle", action="store_true")
    parser.add_argument("--expect-scalar-catch-object", action="store_true")
    parser.add_argument("--expect-exception-object-arena", action="store_true")
    parser.add_argument("--expect-exception-object-fields", action="store_true")
    parser.add_argument("--expect-unhandled-exception", action="store_true")
    parser.add_argument("--expect-nondeterministic-freeze", action="store_true")
    parser.add_argument("--expect-acyclic-pointer-memory-ssa", action="store_true")
    parser.add_argument("--expect-scalar-summary", action="store_true")
    parser.add_argument("--expect-bitcount-intrinsic", action="store_true")
    parser.add_argument("--expect-deferred-poison-freeze", action="store_true")
    parser.add_argument("--expect-cyclic-pointer-memory-ssa", action="store_true")
    parser.add_argument("--expect-canonical-pointer-cell", action="store_true")
    parser.add_argument("--expect-bit-permutation-intrinsic", action="store_true")
    parser.add_argument("--expect-saturating-arithmetic-intrinsic", action="store_true")
    parser.add_argument("--expect-scalar-selection-intrinsic", action="store_true")
    parser.add_argument("--expect-optimization-hint-intrinsic", action="store_true")
    parser.add_argument("--expect-objectsize-intrinsic", action="store_true")
    parser.add_argument("--expect-dynamic-objectsize-intrinsic", action="store_true")
    parser.add_argument("--expect-overflow-arithmetic-intrinsic", action="store_true")
    parser.add_argument("--expect-ssa-copy-intrinsic", action="store_true")
    parser.add_argument("--expect-transitive-deferred-poison", action="store_true")
    parser.add_argument("--expect-select-deferred-poison", action="store_true")
    parser.add_argument("--expect-phi-deferred-poison", action="store_true")
    parser.add_argument("--expect-memory-deferred-poison", action="store_true")
    parser.add_argument(
        "--expect-canonical-memory-deferred-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-cross-block-memory-deferred-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-cross-function-deferred-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-cross-function-argument-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multicallsite-deferred-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-symbolic-pointer-memory",
        action="store_true",
    )
    parser.add_argument(
        "--expect-pointer-initial-definition-merge",
        action="store_true",
    )
    parser.add_argument(
        "--expect-transitive-call-deferred-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multiconsumer-deferred-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multiaccess-memory-deferred-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-branch-memory-deferred-poison",
        action="store_true",
    )
    parser.add_argument(
        "--expect-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multicell-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multicell-alias-graph",
        action="store_true",
    )
    parser.add_argument(
        "--expect-shared-phi-edge-discriminator",
        action="store_true",
    )
    parser.add_argument(
        "--expect-identified-object-multicell-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-fixed-heap-object-multicell-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-finite-pointer-domain-multicell-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-guard-correlated-pointer-domain-multicell-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-phi-correlated-pointer-domain-multicell-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-symbolic-index-interval-multicell-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-byte-lane-memory-definedness",
        action="store_true",
    )
    parser.add_argument(
        "--expect-byte-lane-writer-graph",
        action="store_true",
    )
    parser.add_argument(
        "--expect-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-byte-lane-phi-writer-graph",
        action="store_true",
    )
    parser.add_argument(
        "--expect-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-cyclic-byte-lane-writer-graph",
        action="store_true",
    )
    parser.add_argument(
        "--expect-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-conditional-cyclic-byte-lane-writer-graph",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multiarm-cyclic-byte-lane-writer-graph",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-recursive-cyclic-byte-lane-writer-graph",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-interprocedural-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multilevel-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-cyclic-memory-definedness-phi",
        action="store_true",
    )
    parser.add_argument(
        "--expect-cyclic-memory-definedness-carry",
        action="store_true",
    )
    parser.add_argument(
        "--expect-conditional-memory-definedness-carry",
        action="store_true",
    )
    parser.add_argument(
        "--expect-forwarded-conditional-memory-definedness-carry",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multiarm-conditional-memory-definedness-carry",
        action="store_true",
    )
    parser.add_argument(
        "--expect-equivalent-defined-store-memory-carry",
        action="store_true",
    )
    parser.add_argument(
        "--expect-shared-poison-store-memory-carry",
        action="store_true",
    )
    parser.add_argument(
        "--expect-nested-conditional-memory-definedness-carry",
        action="store_true",
    )
    parser.add_argument(
        "--expect-recursive-memory-definedness-condition-tree",
        action="store_true",
    )
    parser.add_argument(
        "--expect-grouped-recursive-memory-definedness-condition-tree",
        action="store_true",
    )
    parser.add_argument(
        "--expect-repeated-source-recursive-memory-definedness-condition-tree",
        action="store_true",
    )
    parser.add_argument(
        "--expect-multicarry-recursive-memory-definedness-condition-tree",
        action="store_true",
    )
    parser.add_argument(
        "--expect-initial-memory-definedness-merge",
        action="store_true",
    )
    parser.add_argument(
        "--expect-initial-subobject-definedness-merge",
        action="store_true",
    )
    parser.add_argument(
        "--reject-capability",
        action="append",
        default=[],
    )
    args = parser.parse_args()

    with open(args.artifact, encoding="utf-8") as stream:
        artifact = json.load(stream)
    if args.expect_rejected:
        if artifact.get("schema") != "symcc-llvm-continuation-lowering-v1":
            raise AssertionError("lowering rejection has the wrong schema")
        if artifact.get("status") != "rejected":
            raise AssertionError("lowering unexpectedly succeeded")
        diagnostics = "\n".join(str(item) for item in artifact.get("diagnostics", ()))
        if args.expect_rejected not in diagnostics:
            raise AssertionError(
                f"missing rejection diagnostic {args.expect_rejected!r}"
            )
        return 0

    if artifact.get("schema") != "symcc-live-program-v1":
        raise AssertionError("lowering did not produce an executable program")
    lowering = artifact.get("lowering", {})
    unexpected_capabilities = sorted(
        set(args.reject_capability) & set(lowering.get("capabilities", ()))
    )
    if unexpected_capabilities:
        raise AssertionError(
            "unexpected lowering capabilities: " + ", ".join(unexpected_capabilities)
        )
    if lowering.get("status") != "lowered":
        raise AssertionError("lowering metadata is incomplete")
    if "phi-edge-copies" not in lowering.get("capabilities", ()):
        raise AssertionError("PHI lowering capability is missing")
    if args.expect_input_buffer:
        input_buffer = artifact.get("input_buffer", {})
        input_objects = [
            item
            for item in artifact.get("memory_objects", ())
            if item.get("kind") == "input"
        ]
        if (
            input_buffer.get("schema") != "symcc-live-input-buffer-v1"
            or lowering.get("input_abi") != "pointer-size-symbolic-buffer"
            or "pointer-size-input-buffer" not in lowering.get("capabilities", ())
            or len(input_objects) != 1
            or input_objects[0].get("logical_size") != "input-length"
        ):
            raise AssertionError("input-buffer lowering contract is incomplete")
    if args.expect_stack:
        stack_objects = [
            item
            for item in artifact.get("memory_objects", ())
            if item.get("kind") == "stack"
        ]
        if (
            "frame-local-stack" not in lowering.get("capabilities", ())
            or not stack_objects
            or any(
                item.get("initialization") != "runtime-write-tracked"
                or not item.get("function")
                for item in stack_objects
            )
        ):
            raise AssertionError("stack lowering contract is incomplete")
    if (
        args.expect_heap
        or args.expect_heap_pool
        or args.expect_nullable_heap
        or args.expect_heap_lifetime_pointer_union
        or args.expect_collective_heap_union_initialization
        or args.expect_guard_correlated_heap_union_initialization
        or args.expect_memoryssa_aa_heap_initialization
        or args.expect_memoryssa_aa_no_modref
        or args.expect_interprocedural_heap_effect
        or args.expect_interprocedural_allocator_effect
        or args.expect_interprocedural_argument_effect
        or args.expect_interprocedural_noalias_skip
    ):
        heap_objects = [
            item
            for item in artifact.get("memory_objects", ())
            if item.get("kind") == "heap"
        ]
        if (
            "bounded-heap-lifetime" not in lowering.get("capabilities", ())
            or not heap_objects
            or any(
                item.get("lifetime") != "runtime-alloc-free"
                or item.get("allocation")
                not in {
                    "bounded-infallible",
                    "bounded-pool-infallible",
                    "bounded-pool-nullable",
                }
                or not item.get("site")
                or item.get("read_only")
                for item in heap_objects
            )
        ):
            raise AssertionError("heap lowering contract is incomplete")
        if args.expect_heap_pool:
            pool_sites: dict[str, list[dict]] = {}
            for item in heap_objects:
                pool_sites.setdefault(str(item["site"]), []).append(item)
            allocations = [
                instruction
                for function in artifact.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "heap_alloc"
            ]
            if (
                "bounded-multi-instance-heap" not in lowering.get("capabilities", ())
                or any(
                    item.get("allocation") != "bounded-pool-infallible"
                    for item in heap_objects
                )
                or any(
                    len(objects) != int(objects[0].get("capacity", 0))
                    or {int(item.get("slot", -1)) for item in objects}
                    != set(range(len(objects)))
                    for objects in pool_sites.values()
                )
                or any(
                    len(instruction.get("addresses", ()))
                    != int(instruction.get("capacity", 0))
                    for instruction in allocations
                )
            ):
                raise AssertionError("heap-pool lowering contract is incomplete")
        if args.expect_nullable_heap:
            allocations = [
                instruction
                for function in artifact.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "heap_alloc"
            ]
            if (
                not heap_objects
                or any(
                    item.get("allocation") != "bounded-pool-nullable"
                    or item.get("logical_size") != "runtime-allocation-size"
                    for item in heap_objects
                )
                or not allocations
                or any(
                    not instruction.get("nullable")
                    or not 1 <= int(instruction.get("size_bits", 0)) <= 64
                    or int(instruction.get("max_size", 0)) < 1
                    or (
                        instruction.get("allocator", "malloc") == "malloc"
                        and not isinstance(instruction.get("size"), dict)
                    )
                    or (
                        instruction.get("allocator") == "calloc"
                        and (
                            not isinstance(instruction.get("count"), dict)
                            or not isinstance(instruction.get("element_size"), dict)
                            or not instruction.get("zero_initialize")
                        )
                    )
                    for instruction in allocations
                )
            ):
                raise AssertionError("nullable heap lowering contract is incomplete")
        if args.expect_heap_lifetime_pointer_union:
            ordinary_bases = {
                int(item["address"])
                for item in heap_objects
                if item.get("allocation") != "bounded-exception-arena"
            }
            releases = [
                instruction
                for function in artifact.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "heap_free" and "addresses" in instruction
            ]
            if (
                "bounded-heap-lifetime-pointer-union"
                not in lowering.get("capabilities", ())
                or not releases
                or any(
                    set(instruction) != {"op", "address", "addresses", "bits"}
                    or not 1 <= int(instruction.get("bits", 0)) <= 64
                    or instruction.get("addresses")
                    != sorted(set(instruction.get("addresses", ())))
                    or any(
                        int(address) not in ordinary_bases
                        for address in instruction.get("addresses", ())
                    )
                    for instruction in releases
                )
            ):
                raise AssertionError(
                    "heap lifetime pointer-union contract is incomplete"
                )
        if args.expect_collective_heap_union_initialization:
            ordinary_bases = {
                int(item["address"])
                for item in heap_objects
                if item.get("allocation") != "bounded-exception-arena"
            }
            certified_loads = [
                instruction
                for function in artifact.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "load"
                and "initialization_bases" in instruction
            ]
            if (
                "bounded-collective-heap-union-initialization"
                not in lowering.get("capabilities", ())
                or "bounded-pointer-union" not in lowering.get("capabilities", ())
                or not certified_loads
                or any(
                    instruction["initialization_bases"]
                    != sorted(set(instruction["initialization_bases"]))
                    or len(instruction["initialization_bases"]) < 2
                    or not set(instruction["initialization_bases"]) <= ordinary_bases
                    or set(instruction["initialization_bases"])
                    != {
                        int(item["address"])
                        for case in instruction.get("alias_cases", ())
                        for alias in case.get("addresses", ())
                        for item in heap_objects
                        if (
                            int(item["address"]) <= int(alias)
                            and int(alias) + int(instruction["bytes"])
                            <= int(item["address"]) + int(item["size"])
                        )
                    }
                    for instruction in certified_loads
                )
            ):
                raise AssertionError(
                    "collective heap-union initialization contract is incomplete"
                )

            def expect_collective_tamper_rejected(
                tampered: dict, expected_error: str
            ) -> None:
                capabilities = tampered["lowering"]["capabilities"]
                if "bounded-guard-correlated-heap-union-initialization" in capabilities:
                    capabilities.remove(
                        "bounded-guard-correlated-heap-union-initialization"
                    )
                    for function in tampered["functions"].values():
                        for instructions in function["blocks"].values():
                            for instruction in instructions:
                                instruction.pop("initialization_guard_tree", None)
                if "bounded-memoryssa-aa-heap-initialization" in capabilities:
                    capabilities.remove("bounded-memoryssa-aa-heap-initialization")
                    for function in tampered["functions"].values():
                        for instructions in function["blocks"].values():
                            for instruction in instructions:
                                instruction.pop("initialization_memoryssa", None)
                with tempfile.TemporaryDirectory() as tamper_tmp:
                    executor = LiveContinuationExecutor(
                        LiveStateStore(tamper_tmp, page_size=64)
                    )
                    try:
                        executor.create(tampered)
                    except ValueError as exc:
                        if expected_error not in str(exc):
                            raise AssertionError(
                                "collective initialization tamper failed "
                                "for the wrong reason"
                            ) from exc
                    else:
                        raise AssertionError(
                            "collective initialization tamper was accepted"
                        )

            missing_capability = copy.deepcopy(artifact)
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-collective-heap-union-initialization"
            )
            expect_collective_tamper_rejected(
                missing_capability, "certificate is invalid"
            )

            incomplete = copy.deepcopy(artifact)
            incomplete_load = next(
                instruction
                for function in incomplete["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_bases" in instruction
            )
            incomplete_load["initialization_bases"] = incomplete_load[
                "initialization_bases"
            ][:1]
            expect_collective_tamper_rejected(incomplete, "certificate is invalid")

            mismatched = copy.deepcopy(artifact)
            mismatched_load = next(
                instruction
                for function in mismatched["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_bases" in instruction
            )
            mismatched_load["initialization_bases"][-1] = max(ordinary_bases) + 4096
            expect_collective_tamper_rejected(
                mismatched, "does not match its alias cases"
            )

            missing_contract = copy.deepcopy(artifact)
            for function in missing_contract["functions"].values():
                for instructions in function["blocks"].values():
                    for instruction in instructions:
                        instruction.pop("initialization_bases", None)
            expect_collective_tamper_rejected(
                missing_contract, "capability has no contract"
            )
        if args.expect_guard_correlated_heap_union_initialization:
            guarded_loads = [
                instruction
                for function in artifact.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "load"
                and "initialization_guard_tree" in instruction
            ]
            if (
                "bounded-guard-correlated-heap-union-initialization"
                not in lowering.get("capabilities", ())
                or "bounded-collective-heap-union-initialization"
                not in lowering.get("capabilities", ())
                or not guarded_loads
                or any(
                    load["initialization_guard_tree"].get("schema")
                    != "symcc-guarded-heap-union-initialization-v1"
                    or not 1
                    <= int(load["initialization_guard_tree"].get("depth", 0))
                    <= 8
                    or not 2
                    <= len(load["initialization_guard_tree"].get("paths", ()))
                    <= 64
                    or {
                        int(path["base"])
                        for path in load["initialization_guard_tree"]["paths"]
                    }
                    != set(load["initialization_bases"])
                    for load in guarded_loads
                )
            ):
                raise AssertionError(
                    "guard-correlated heap-union initialization contract is incomplete"
                )

            def expect_guarded_tamper_rejected(
                tampered: dict, expected_error: str
            ) -> None:
                with tempfile.TemporaryDirectory() as tamper_tmp:
                    executor = LiveContinuationExecutor(
                        LiveStateStore(tamper_tmp, page_size=64)
                    )
                    try:
                        executor.create(tampered)
                    except ValueError as exc:
                        if expected_error not in str(exc):
                            raise AssertionError(
                                "guarded initialization tamper failed "
                                "for the wrong reason"
                            ) from exc
                    else:
                        raise AssertionError(
                            "guarded initialization tamper was accepted"
                        )

            missing_guarded_capability = copy.deepcopy(artifact)
            missing_guarded_capability["lowering"]["capabilities"].remove(
                "bounded-guard-correlated-heap-union-initialization"
            )
            expect_guarded_tamper_rejected(
                missing_guarded_capability, "transcript is invalid"
            )

            flipped_decision = copy.deepcopy(artifact)
            flipped_load = next(
                instruction
                for function in flipped_decision["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_guard_tree" in instruction
            )
            flipped_guard = flipped_load["initialization_guard_tree"]["paths"][0][
                "decisions"
            ][0]
            flipped_guard["equals"] = not flipped_guard["equals"]
            expect_guarded_tamper_rejected(
                flipped_decision, "does not match control flow"
            )

            mismatched_path = copy.deepcopy(artifact)
            mismatched_path_load = next(
                instruction
                for function in mismatched_path["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_guard_tree" in instruction
            )
            mismatched_paths = mismatched_path_load["initialization_guard_tree"][
                "paths"
            ]
            mismatched_paths[-1] = copy.deepcopy(mismatched_paths[0])
            expect_guarded_tamper_rejected(
                mismatched_path,
                "guarded heap-union initialization transcript",
            )

            invalid_store = copy.deepcopy(artifact)
            invalid_store_load = next(
                instruction
                for function in invalid_store["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_guard_tree" in instruction
            )
            invalid_store_load["initialization_guard_tree"]["paths"][0]["store"][
                "ordinal"
            ] += 1024
            expect_guarded_tamper_rejected(invalid_store, "store witness is invalid")

            mismatched_guard_base = copy.deepcopy(artifact)
            mismatched_guard_load = next(
                instruction
                for function in mismatched_guard_base["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_guard_tree" in instruction
            )
            guard = next(
                guard
                for alias_case in mismatched_guard_load["alias_cases"]
                for guard in alias_case["guards"]
                if guard.get("equals") not in alias_case.get("addresses", ())
            )
            guard["equals"] = (
                1 - int(guard["equals"])
                if guard.get("bits") == 1
                else int(guard["equals"]) + 1024
            )
            expect_guarded_tamper_rejected(
                mismatched_guard_base, "does not match alias guards"
            )

            missing_load_identity = copy.deepcopy(artifact)
            missing_load_identity_load = next(
                instruction
                for function in missing_load_identity["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_guard_tree" in instruction
            )
            first_case = missing_load_identity_load["alias_cases"][0]
            first_case["guards"] = [
                guard
                for guard in first_case["guards"]
                if guard.get("equals") not in first_case["addresses"]
            ]
            expect_guarded_tamper_rejected(
                missing_load_identity, "does not match alias guards"
            )

            missing_store_identity = copy.deepcopy(artifact)
            guarded_function = next(
                function
                for function in missing_store_identity["functions"].values()
                if any(
                    "initialization_guard_tree" in instruction
                    for instructions in function["blocks"].values()
                    for instruction in instructions
                )
            )
            guarded_load = next(
                instruction
                for instructions in guarded_function["blocks"].values()
                for instruction in instructions
                if "initialization_guard_tree" in instruction
            )
            store_contract = guarded_load["initialization_guard_tree"]["paths"][0][
                "store"
            ]
            store_instructions = [
                instruction
                for instruction in guarded_function["blocks"][store_contract["block"]]
                if instruction.get("op") == "store"
            ]
            store_instructions[store_contract["ordinal"]]["alias_cases"][0][
                "guards"
            ] = [
                copy.deepcopy(
                    next(
                        guard
                        for alias_case in guarded_load["alias_cases"]
                        for guard in alias_case["guards"]
                        if guard.get("equals") not in alias_case.get("addresses", ())
                    )
                )
            ]
            expect_guarded_tamper_rejected(
                missing_store_identity, "store witness is invalid"
            )

            missing_guard_tree = copy.deepcopy(artifact)
            for function in missing_guard_tree["functions"].values():
                for instructions in function["blocks"].values():
                    for instruction in instructions:
                        instruction.pop("initialization_guard_tree", None)
            expect_guarded_tamper_rejected(
                missing_guard_tree, "capability has no contract"
            )
        if args.expect_memoryssa_aa_heap_initialization:
            memoryssa_loads = [
                instruction
                for function in artifact.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "load"
                and "initialization_memoryssa" in instruction
            ]
            if (
                "bounded-memoryssa-aa-heap-initialization"
                not in lowering.get("capabilities", ())
                or not memoryssa_loads
                or any(
                    transcript.get("schema")
                    != "symcc-memoryssa-aa-heap-initialization-v1"
                    or transcript.get("root_node") != 0
                    or not 3 <= len(transcript.get("nodes", ())) <= 128
                    or transcript["nodes"][0].get("kind") != "memory-phi"
                    for transcript in (
                        load["initialization_memoryssa"] for load in memoryssa_loads
                    )
                )
            ):
                raise AssertionError(
                    "MemorySSA/AA heap initialization contract is incomplete"
                )

            def expect_memoryssa_tamper_rejected(
                tampered: dict, expected_error: str
            ) -> None:
                with tempfile.TemporaryDirectory() as tamper_tmp:
                    executor = LiveContinuationExecutor(
                        LiveStateStore(tamper_tmp, page_size=64)
                    )
                    try:
                        executor.create(tampered)
                    except ValueError as exc:
                        if expected_error not in str(exc):
                            raise AssertionError(
                                "MemorySSA initialization tamper failed "
                                "for the wrong reason"
                            ) from exc
                    else:
                        raise AssertionError(
                            "MemorySSA initialization tamper was accepted"
                        )

            missing_capability = copy.deepcopy(artifact)
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-memoryssa-aa-heap-initialization"
            )
            expect_memoryssa_tamper_rejected(
                missing_capability, "transcript is invalid"
            )

            bad_edge = copy.deepcopy(artifact)
            bad_edge_load = next(
                instruction
                for function in bad_edge["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_memoryssa" in instruction
            )
            bad_edge_load["initialization_memoryssa"]["nodes"][0]["incoming"][0][
                "block"
            ] = "missing-block"
            expect_memoryssa_tamper_rejected(bad_edge, "transcript")

            bad_child = copy.deepcopy(artifact)
            bad_child_load = next(
                instruction
                for function in bad_child["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_memoryssa" in instruction
            )
            bad_child_load["initialization_memoryssa"]["nodes"][0]["incoming"][0][
                "node"
            ] = 0
            expect_memoryssa_tamper_rejected(bad_child, "transcript")

            orphan_node = copy.deepcopy(artifact)
            orphan_load = next(
                instruction
                for function in orphan_node["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_memoryssa" in instruction
            )
            orphan = copy.deepcopy(orphan_load["initialization_memoryssa"]["nodes"][-1])
            orphan["id"] = len(orphan_load["initialization_memoryssa"]["nodes"])
            orphan_load["initialization_memoryssa"]["nodes"].append(orphan)
            expect_memoryssa_tamper_rejected(orphan_node, "transcript")

            bad_store = copy.deepcopy(artifact)
            bad_store_load = next(
                instruction
                for function in bad_store["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_memoryssa" in instruction
            )
            bad_store_node = next(
                node
                for node in bad_store_load["initialization_memoryssa"]["nodes"]
                if node["kind"] == "store"
            )
            bad_store_node["store"]["ordinal"] += 1024
            expect_memoryssa_tamper_rejected(bad_store, "transcript")

            bad_base = copy.deepcopy(artifact)
            bad_base_load = next(
                instruction
                for function in bad_base["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_memoryssa" in instruction
            )
            next(
                node
                for node in bad_base_load["initialization_memoryssa"]["nodes"]
                if node["kind"] == "store"
            )["base"] += 1
            expect_memoryssa_tamper_rejected(bad_base, "transcript")

            bad_skip = copy.deepcopy(artifact)
            bad_skip_load = next(
                instruction
                for function in bad_skip["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_memoryssa" in instruction
            )
            skipped_node = next(
                (
                    node
                    for node in bad_skip_load["initialization_memoryssa"]["nodes"]
                    if node["kind"] == "store" and node["skipped_defs"]
                ),
                None,
            )
            if skipped_node is None:
                skipped_node = next(
                    node
                    for node in bad_skip_load["initialization_memoryssa"]["nodes"]
                    if node["kind"] == "store"
                )
                skipped_node["skipped_defs"].append(
                    {
                        "block": skipped_node["block"],
                        "ordinal": 0,
                        "kind": "store",
                        "opcode": "store",
                        "proof": "may-alias",
                    }
                )
            else:
                skipped_node["skipped_defs"][0]["proof"] = "may-alias"
            expect_memoryssa_tamper_rejected(bad_skip, "transcript")

            missing_transcript = copy.deepcopy(artifact)
            for function in missing_transcript["functions"].values():
                for instructions in function["blocks"].values():
                    for instruction in instructions:
                        instruction.pop("initialization_memoryssa", None)
            expect_memoryssa_tamper_rejected(
                missing_transcript, "capability has no contract"
            )
        if args.expect_memoryssa_aa_no_modref:
            no_modref_skips = [
                skipped
                for function in artifact.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                for node in instruction.get("initialization_memoryssa", {}).get(
                    "nodes", []
                )
                for skipped in node.get("skipped_defs", [])
                if skipped.get("proof") == "aa-no-modref"
            ]
            if not no_modref_skips or any(
                skipped.get("kind") != "call" or skipped.get("opcode") != "call"
                for skipped in no_modref_skips
            ):
                raise AssertionError(
                    "MemorySSA/AA NoModRef call contract is incomplete"
                )
            bad_call_ordinal = copy.deepcopy(artifact)
            bad_call_skip = next(
                skipped
                for function in bad_call_ordinal["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                for node in instruction.get("initialization_memoryssa", {}).get(
                    "nodes", []
                )
                for skipped in node.get("skipped_defs", [])
                if skipped.get("proof") == "aa-no-modref"
            )
            bad_call_skip["ordinal"] += 1024
            expect_memoryssa_tamper_rejected(bad_call_ordinal, "transcript")

            bad_call_kind = copy.deepcopy(artifact)
            bad_kind_skip = next(
                skipped
                for function in bad_call_kind["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                for node in instruction.get("initialization_memoryssa", {}).get(
                    "nodes", []
                )
                for skipped in node.get("skipped_defs", [])
                if skipped.get("proof") == "aa-no-modref"
            )
            bad_kind_skip["kind"] = "memory-def"
            expect_memoryssa_tamper_rejected(bad_call_kind, "transcript")
        if (
            args.expect_interprocedural_heap_effect
            or args.expect_interprocedural_allocator_effect
            or args.expect_interprocedural_argument_effect
            or args.expect_interprocedural_noalias_skip
        ):

            def effect_records(instruction: dict) -> list[dict]:
                raw = instruction.get("initialization_interprocedural")
                if isinstance(raw, dict):
                    return [raw]
                if isinstance(raw, list) and all(
                    isinstance(effect, dict) for effect in raw
                ):
                    return raw
                return []

            def first_effect(instruction: dict) -> dict:
                records = effect_records(instruction)
                if not records:
                    raise AssertionError(
                        "interprocedural heap effect contract is incomplete"
                    )
                return records[0]

            effect_loads = [
                instruction
                for function in artifact.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "load"
                and "initialization_interprocedural" in instruction
            ]
            effects = [
                effect for load in effect_loads for effect in effect_records(load)
            ]
            if (
                "bounded-interprocedural-heap-effect-summary"
                not in lowering.get("capabilities", ())
                or not effects
                or any(
                    effect.get("schema")
                    != "symcc-interprocedural-heap-effect-summary-v1"
                    or effect.get("kind")
                    not in {"returned-allocation", "argument-initializer"}
                    for effect in effects
                )
            ):
                raise AssertionError(
                    "interprocedural heap effect contract is incomplete"
                )
            if args.expect_interprocedural_allocator_effect and not any(
                effect.get("kind") == "returned-allocation" for effect in effects
            ):
                raise AssertionError("returned allocator effect contract is incomplete")
            if args.expect_interprocedural_argument_effect and not any(
                effect.get("kind") == "argument-initializer" for effect in effects
            ):
                raise AssertionError(
                    "argument initializer effect contract is incomplete"
                )
            noalias_skips = [
                skipped
                for effect in effects
                for skipped in effect.get("skipped_defs", [])
                if skipped.get("proof") in {"allocation-noalias", "aa-noalias"}
            ]
            if args.expect_interprocedural_noalias_skip and not noalias_skips:
                raise AssertionError(
                    "interprocedural NoAlias skip contract is incomplete"
                )

            def expect_interprocedural_tamper_rejected(
                tampered: dict, expected_error: str
            ) -> None:
                with tempfile.TemporaryDirectory() as tamper_tmp:
                    executor = LiveContinuationExecutor(
                        LiveStateStore(tamper_tmp, page_size=64)
                    )
                    try:
                        executor.create(tampered)
                    except ValueError as exc:
                        if expected_error not in str(exc):
                            raise AssertionError(
                                "interprocedural heap effect tamper failed "
                                "for the wrong reason"
                            ) from exc
                    else:
                        raise AssertionError(
                            "interprocedural heap effect tamper was accepted"
                        )

            missing_capability = copy.deepcopy(artifact)
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-interprocedural-heap-effect-summary"
            )
            expect_interprocedural_tamper_rejected(
                missing_capability, "transcript is invalid"
            )

            bad_call = copy.deepcopy(artifact)
            bad_call_effect = next(
                first_effect(instruction)
                for function in bad_call["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_interprocedural" in instruction
            )
            bad_call_effect["call"]["ordinal"] += 1024
            expect_interprocedural_tamper_rejected(bad_call, "transcript is invalid")

            bad_callee = copy.deepcopy(artifact)
            bad_callee_effect = next(
                first_effect(instruction)
                for function in bad_callee["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_interprocedural" in instruction
            )
            bad_callee_effect["callee"] = "missing_function"
            expect_interprocedural_tamper_rejected(bad_callee, "transcript is invalid")

            extra_load_case = copy.deepcopy(artifact)
            extra_load = next(
                instruction
                for function in extra_load_case["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_interprocedural" in instruction
            )
            extra_load["alias_cases"].append(
                copy.deepcopy(extra_load["alias_cases"][0])
            )
            expect_interprocedural_tamper_rejected(
                extra_load_case, "transcript is invalid"
            )

            bad_store = copy.deepcopy(artifact)
            bad_store_effect = next(
                first_effect(instruction)
                for function in bad_store["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_interprocedural" in instruction
            )
            if bad_store_effect.get("store") is not None:
                bad_store_effect["store"]["bytes"] += 1
                expect_interprocedural_tamper_rejected(
                    bad_store, "transcript is invalid"
                )

                extra_store = copy.deepcopy(artifact)
                extra_effect = next(
                    first_effect(instruction)
                    for function in extra_store["functions"].values()
                    for instructions in function["blocks"].values()
                    for instruction in instructions
                    if "initialization_interprocedural" in instruction
                )
                callee = extra_store["functions"][extra_effect["callee"]]
                callee_instructions = callee["blocks"][extra_effect["store"]["block"]]
                certified_store = next(
                    instruction
                    for instruction in callee_instructions
                    if instruction.get("op") == "store"
                )
                callee_instructions.insert(
                    len(callee_instructions) - 1,
                    copy.deepcopy(certified_store),
                )
                expect_interprocedural_tamper_rejected(
                    extra_store, "transcript is invalid"
                )

                extra_store_case = copy.deepcopy(artifact)
                extra_case_effect = next(
                    effect
                    for function in extra_store_case["functions"].values()
                    for instructions in function["blocks"].values()
                    for instruction in instructions
                    for effect in effect_records(instruction)
                    if effect.get("store") is not None
                )
                extra_case_callee = extra_store_case["functions"][
                    extra_case_effect["callee"]
                ]
                extra_case_store = next(
                    instruction
                    for instructions in extra_case_callee["blocks"].values()
                    for instruction in instructions
                    if instruction.get("op") == "store"
                )
                extra_case_store["alias_cases"].append(
                    copy.deepcopy(extra_case_store["alias_cases"][0])
                )
                expect_interprocedural_tamper_rejected(
                    extra_store_case, "transcript is invalid"
                )

            returned_effect = next(
                (
                    effect
                    for effect in effects
                    if effect.get("kind") == "returned-allocation"
                ),
                None,
            )
            if returned_effect is not None:
                bad_zero_initialization = copy.deepcopy(artifact)
                bad_zero_effect = next(
                    effect
                    for function in bad_zero_initialization["functions"].values()
                    for instructions in function["blocks"].values()
                    for instruction in instructions
                    for effect in effect_records(instruction)
                    if effect.get("kind") == "returned-allocation"
                )
                allocation = bad_zero_effect["allocation"]
                allocation["zero_initialize"] = not allocation["zero_initialize"]
                expect_interprocedural_tamper_rejected(
                    bad_zero_initialization, "transcript is invalid"
                )

            if noalias_skips:
                bad_skip = copy.deepcopy(artifact)
                bad_skip_item = next(
                    skipped
                    for function in bad_skip["functions"].values()
                    for instructions in function["blocks"].values()
                    for instruction in instructions
                    for effect in effect_records(instruction)
                    for skipped in effect.get("skipped_defs", [])
                )
                bad_skip_item["proof"] = "may-alias"
                expect_interprocedural_tamper_rejected(
                    bad_skip, "transcript is invalid"
                )

            missing_transcript = copy.deepcopy(artifact)
            for function in missing_transcript["functions"].values():
                for instructions in function["blocks"].values():
                    for instruction in instructions:
                        instruction.pop("initialization_interprocedural", None)
            expect_interprocedural_tamper_rejected(
                missing_transcript, "capability has no contract"
            )
    if (
        args.expect_symbolic_region_effect
        or args.expect_dynamic_byte_lane_cover
        or args.expect_symbolic_region_copy
        or args.expect_dynamic_byte_lane_noalias_skip
    ):
        capabilities = set(lowering.get("capabilities", ()))
        region_stores = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("op") == "store"
            and isinstance(instruction.get("symbolic_region"), dict)
        ]
        cover_loads = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("op") == "load"
            and isinstance(
                instruction.get("initialization_dynamic_byte_lane"),
                dict,
            )
        ]
        if (
            "bounded-symbolic-length-region-effect" not in capabilities
            or not region_stores
            or any(
                store["symbolic_region"].get("schema")
                != "symcc-symbolic-length-region-write-v1"
                for store in region_stores
            )
        ):
            raise AssertionError("symbolic-length region effect contract is incomplete")
        if (
            args.expect_dynamic_byte_lane_cover
            or args.expect_dynamic_byte_lane_noalias_skip
        ) and (
            "bounded-symbolic-length-byte-lane-cover" not in capabilities
            or not cover_loads
            or any(
                load["initialization_dynamic_byte_lane"].get("schema")
                != "symcc-symbolic-length-byte-lane-cover-v1"
                for load in cover_loads
            )
        ):
            raise AssertionError(
                "symbolic-length byte-lane cover contract is incomplete"
            )
        if args.expect_symbolic_region_copy and not any(
            store["symbolic_region"].get("kind") in {"memcpy", "memmove"}
            for store in region_stores
        ):
            raise AssertionError("symbolic-length copy contract is incomplete")
        noalias_skips = [
            skipped
            for load in cover_loads
            for skipped in load["initialization_dynamic_byte_lane"].get(
                "skipped_defs", []
            )
            if skipped.get("proof") in {"allocation-noalias", "aa-noalias"}
        ]
        if args.expect_dynamic_byte_lane_noalias_skip and not noalias_skips:
            raise AssertionError("symbolic-length byte-lane NoAlias skip is incomplete")

        def expect_dynamic_cover_tamper_rejected(
            tampered: dict, expected_error: str
        ) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if expected_error not in str(exc):
                        raise AssertionError(
                            "symbolic-length byte-lane tamper failed "
                            "for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "symbolic-length byte-lane tamper was accepted"
                    )

        missing_region_capability = copy.deepcopy(artifact)
        missing_region_capability["lowering"]["capabilities"].remove(
            "bounded-symbolic-length-region-effect"
        )
        expect_dynamic_cover_tamper_rejected(
            missing_region_capability, "requires symbolic region"
        )

        bad_guard = copy.deepcopy(artifact)
        guard_metadata = next(
            instruction["symbolic_region_guard"]
            for function in bad_guard["functions"].values()
            for instructions in function["blocks"].values()
            for instruction in instructions
            if "symbolic_region_guard" in instruction
        )
        guard_metadata["offset"] += 1
        expect_dynamic_cover_tamper_rejected(
            bad_guard, "region effect contract is invalid"
        )

        bad_bound = copy.deepcopy(artifact)
        bound_metadata = next(
            instruction["symbolic_region_bound"]
            for function in bad_bound["functions"].values()
            for instructions in function["blocks"].values()
            for instruction in instructions
            if instruction.get("op") == "binary"
            and "symbolic_region_bound" in instruction
        )
        bound_metadata["maximum_bytes"] += 1
        expect_dynamic_cover_tamper_rejected(
            bad_bound, "region effect contract is invalid"
        )

        missing_writer = copy.deepcopy(artifact)
        missing_writer_store = next(
            instruction
            for function in missing_writer["functions"].values()
            for instructions in function["blocks"].values()
            for instruction in instructions
            if "symbolic_region" in instruction
        )
        missing_writer_store.pop("symbolic_region")
        expect_dynamic_cover_tamper_rejected(
            missing_writer, "region effect contract is invalid"
        )

        if cover_loads:
            missing_cover_capability = copy.deepcopy(artifact)
            missing_cover_capability["lowering"]["capabilities"].remove(
                "bounded-symbolic-length-byte-lane-cover"
            )
            expect_dynamic_cover_tamper_rejected(
                missing_cover_capability, "transcript is invalid"
            )

            bad_lane = copy.deepcopy(artifact)
            bad_lane_cover = next(
                instruction["initialization_dynamic_byte_lane"]
                for function in bad_lane["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_dynamic_byte_lane" in instruction
            )
            bad_lane_cover["lanes"][0]["region_offset"] += 1
            expect_dynamic_cover_tamper_rejected(
                bad_lane, "cover transcript is invalid"
            )

            bad_index = copy.deepcopy(artifact)
            bad_index_cover = next(
                instruction["initialization_dynamic_byte_lane"]
                for function in bad_index["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if "initialization_dynamic_byte_lane" in instruction
            )
            bad_index_cover["index"]["maximum"] += 1
            expect_dynamic_cover_tamper_rejected(
                bad_index, "cover transcript is invalid"
            )

            missing_transcript = copy.deepcopy(artifact)
            for function in missing_transcript["functions"].values():
                for instructions in function["blocks"].values():
                    for instruction in instructions:
                        instruction.pop("initialization_dynamic_byte_lane", None)
            expect_dynamic_cover_tamper_rejected(
                missing_transcript, "capability has no contract"
            )

        if noalias_skips:
            bad_skip = copy.deepcopy(artifact)
            skip = next(
                skipped
                for function in bad_skip["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                for skipped in instruction.get(
                    "initialization_dynamic_byte_lane", {}
                ).get("skipped_defs", [])
            )
            skip["proof"] = "may-alias"
            expect_dynamic_cover_tamper_rejected(
                bad_skip, "cover transcript is invalid"
            )
    if args.expect_loop_memoryphi_byte_lane_induction:
        capabilities = set(lowering.get("capabilities", ()))
        loop_loads = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if isinstance(instruction.get("initialization_loop_memoryphi"), dict)
        ]
        if (
            "bounded-loop-memoryphi-byte-lane-induction" not in capabilities
            or not loop_loads
            or any(
                load["initialization_loop_memoryphi"].get("schema")
                not in {
                    "symcc-loop-memoryphi-byte-lane-induction-v1",
                    "symcc-loop-memoryphi-byte-lane-induction-v2",
                    "symcc-loop-memoryphi-byte-lane-induction-v3",
                    "symcc-loop-memoryphi-byte-lane-induction-v4",
                    "symcc-loop-memoryphi-byte-lane-induction-v5",
                    "symcc-loop-memoryphi-byte-lane-induction-v6",
                    "symcc-loop-memoryphi-byte-lane-induction-v7",
                    "symcc-loop-memoryphi-byte-lane-induction-v8",
                    "symcc-loop-memoryphi-byte-lane-induction-v9",
                    "symcc-loop-memoryphi-byte-lane-induction-v10",
                    "symcc-loop-memoryphi-byte-lane-induction-v11",
                }
                for load in loop_loads
            )
        ):
            raise AssertionError(
                "loop MemoryPhi byte-lane induction contract is incomplete"
            )

        def expect_loop_tamper_rejected(tampered: dict, expected_error: str) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if expected_error not in str(exc):
                        raise AssertionError(
                            "loop MemoryPhi tamper failed for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError("loop MemoryPhi tamper was accepted")

        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-loop-memoryphi-byte-lane-induction"
        )
        if (
            "bounded-strided-loop-memoryphi-byte-lane-induction"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-strided-loop-memoryphi-byte-lane-induction"
            )
        if (
            "bounded-conditional-loop-memoryphi-byte-lane-induction"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-conditional-loop-memoryphi-byte-lane-induction"
            )
        if (
            "bounded-multilatch-loop-memoryphi-byte-lane-fixed-point"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-multilatch-loop-memoryphi-byte-lane-fixed-point"
            )
        if (
            "bounded-multilatch-loop-memoryphi-ordered-writer-transfer"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-multilatch-loop-memoryphi-ordered-writer-transfer"
            )
        if (
            "bounded-nested-loop-memoryphi-summary-composition"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-nested-loop-memoryphi-summary-composition"
            )
        if (
            "bounded-nested-loop-memoryphi-last-write-value-summary"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-nested-loop-memoryphi-last-write-value-summary"
            )
        if (
            "bounded-nested-loop-memoryphi-two-dimensional-affine-summary"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-nested-loop-memoryphi-two-dimensional-affine-summary"
            )
        if (
            "bounded-nested-loop-memoryphi-affine-symbolic-value-summary"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-nested-loop-memoryphi-affine-symbolic-value-summary"
            )
        if (
            "bounded-nested-loop-memoryphi-piecewise-affine-value-summary"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-nested-loop-memoryphi-piecewise-affine-value-summary"
            )
        if (
            "bounded-nested-loop-memoryphi-decision-dag-value-summary"
            in missing_capability["lowering"]["capabilities"]
        ):
            missing_capability["lowering"]["capabilities"].remove(
                "bounded-nested-loop-memoryphi-decision-dag-value-summary"
            )
        expect_loop_tamper_rejected(missing_capability, "transcript is invalid")

        bad_witness = copy.deepcopy(artifact)
        bad_witness_transcript = next(
            instruction["initialization_loop_memoryphi"]
            for function in bad_witness["functions"].values()
            for instructions in function["blocks"].values()
            for instruction in instructions
            if "initialization_loop_memoryphi" in instruction
        )
        if bad_witness_transcript["schema"].endswith(("v4", "v5")):
            bad_witness_transcript["witnesses"][0]["alternatives"][0][
                "induction_value"
            ] += 1
        elif bad_witness_transcript["schema"].endswith(
            ("v6", "v7", "v8", "v9", "v10", "v11")
        ):
            cases_key = (
                "last_write_cases"
                if bad_witness_transcript["schema"].endswith(
                    ("v7", "v8", "v9", "v10", "v11")
                )
                else "alternatives"
            )
            bad_witness_transcript["witnesses"][0][cases_key][0][
                "inner_induction_value"
            ] += 1
        else:
            bad_witness_transcript["witnesses"][0]["induction_value"] += 1
        expect_loop_tamper_rejected(bad_witness, "transcript is invalid")

        bad_edge = copy.deepcopy(artifact)
        bad_edge_transcript = next(
            instruction["initialization_loop_memoryphi"]
            for function in bad_edge["functions"].values()
            for instructions in function["blocks"].values()
            for instruction in instructions
            if "initialization_loop_memoryphi" in instruction
        )
        if bad_edge_transcript["schema"].endswith(("v4", "v5")):
            bad_edge_transcript["loop"]["latch_edges"][0] = bad_edge_transcript["loop"][
                "preheader_edge"
            ]
        elif bad_edge_transcript["schema"].endswith(
            ("v6", "v7", "v8", "v9", "v10", "v11")
        ):
            bad_edge_transcript["loops"]["outer"]["latch_edge"] = (
                bad_edge_transcript["loops"]["outer"]["preheader_edge"]
            )
        else:
            bad_edge_transcript["loop"]["latch_edge"] = bad_edge_transcript["loop"][
                "preheader_edge"
            ]
        expect_loop_tamper_rejected(bad_edge, "transcript is invalid")

        bad_step_type = copy.deepcopy(artifact)
        bad_step_transcript = next(
            instruction["initialization_loop_memoryphi"]
            for function in bad_step_type["functions"].values()
            for instructions in function["blocks"].values()
            for instruction in instructions
            if "initialization_loop_memoryphi" in instruction
        )
        if bad_step_transcript["schema"].endswith(
            ("v6", "v7", "v8", "v9", "v10", "v11")
        ):
            bad_step_transcript["inner_induction"]["step"] = True
        else:
            bad_step_transcript["induction"]["step"] = True
        expect_loop_tamper_rejected(bad_step_type, "transcript is invalid")

        bad_bound_cast = copy.deepcopy(artifact)
        bad_bound_transcript = next(
            instruction["initialization_loop_memoryphi"]
            for function in bad_bound_cast["functions"].values()
            for instructions in function["blocks"].values()
            for instruction in instructions
            if "initialization_loop_memoryphi" in instruction
        )
        bound_name = (
            bad_bound_transcript["inner_guard"]["bound"]["var"]
            if bad_bound_transcript["schema"].endswith(
                ("v6", "v7", "v8", "v9", "v10", "v11")
            )
            else bad_bound_transcript["guard"]["bound"]["var"]
        )
        bound_definition = next(
            instruction
            for function in bad_bound_cast["functions"].values()
            for instructions in function["blocks"].values()
            for instruction in instructions
            if instruction.get("dst") == bound_name
        )
        bound_definition["operator"] = "identity"
        expect_loop_tamper_rejected(bad_bound_cast, "transcript is invalid")

        missing_transcript = copy.deepcopy(artifact)
        for function in missing_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    instruction.pop("initialization_loop_memoryphi", None)
        expect_loop_tamper_rejected(missing_transcript, "capability has no contract")
    if args.expect_strided_loop_memoryphi_byte_lane_induction:
        capabilities = set(lowering.get("capabilities", ()))

        def is_strided_loop_transcript(item: dict) -> bool:
            schema = item.get("schema")
            if schema == "symcc-loop-memoryphi-byte-lane-induction-v2":
                return True
            if schema == "symcc-loop-memoryphi-byte-lane-induction-v3":
                writers = [item.get("writer", {})]
            elif schema in {
                "symcc-loop-memoryphi-byte-lane-induction-v4",
                "symcc-loop-memoryphi-byte-lane-induction-v5",
            }:
                writers = [
                    writer
                    for transfer in item.get("transfers", [])
                    if transfer.get("kind") == "writer"
                    for writer in (
                        transfer.get("writers", [])
                        if schema.endswith("v5")
                        else [transfer.get("writer", {})]
                    )
                ]
            elif schema in {
                "symcc-loop-memoryphi-byte-lane-induction-v6",
                "symcc-loop-memoryphi-byte-lane-induction-v7",
                "symcc-loop-memoryphi-byte-lane-induction-v8",
                "symcc-loop-memoryphi-byte-lane-induction-v9",
                "symcc-loop-memoryphi-byte-lane-induction-v10",
                "symcc-loop-memoryphi-byte-lane-induction-v11",
            }:
                writers = item.get("summary", {}).get("writers", [])
                return item.get("inner_induction", {}).get("step") != 1 or any(
                    (
                        writer.get("scale", 1) != 1
                        if not schema.endswith(("v8", "v9", "v10", "v11"))
                        else writer.get("pointer_scale", 1)
                        * writer.get("affine_index", {}).get("inner_scale", 1)
                        != 1
                    )
                    or writer.get("bytes") != 1
                    for writer in writers
                )
            else:
                return False
            return item.get("induction", {}).get("step") != 1 or any(
                writer.get("scale", 1) != 1 or writer.get("bytes") != 1
                for writer in writers
            )

        def strided_writer(item: dict) -> dict:
            if item.get("schema") in {
                "symcc-loop-memoryphi-byte-lane-induction-v6",
                "symcc-loop-memoryphi-byte-lane-induction-v7",
                "symcc-loop-memoryphi-byte-lane-induction-v8",
                "symcc-loop-memoryphi-byte-lane-induction-v9",
                "symcc-loop-memoryphi-byte-lane-induction-v10",
                "symcc-loop-memoryphi-byte-lane-induction-v11",
            }:
                return item["summary"]["writers"][0]
            if item.get("schema") in {
                "symcc-loop-memoryphi-byte-lane-induction-v4",
                "symcc-loop-memoryphi-byte-lane-induction-v5",
            }:
                return next(
                    (
                        transfer["writers"][0]
                        if item["schema"].endswith("v5")
                        else transfer["writer"]
                    )
                    for transfer in item["transfers"]
                    if transfer["kind"] == "writer"
                )
            return item["writer"]

        strided_loads = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if isinstance(instruction.get("initialization_loop_memoryphi"), dict)
            and is_strided_loop_transcript(instruction["initialization_loop_memoryphi"])
        ]
        if (
            "bounded-strided-loop-memoryphi-byte-lane-induction" not in capabilities
            or not strided_loads
        ):
            raise AssertionError(
                "strided loop MemoryPhi byte-lane contract is incomplete"
            )

        def expect_strided_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError("strided loop MemoryPhi tamper was accepted")

        def strided_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if is_strided_loop_transcript(
                    instruction.get("initialization_loop_memoryphi", {})
                )
            )

        mutations: list[dict] = []
        missing_extension = copy.deepcopy(artifact)
        missing_extension["lowering"]["capabilities"].remove(
            "bounded-strided-loop-memoryphi-byte-lane-induction"
        )
        mutations.append(missing_extension)
        def mutate_stride(item: dict) -> None:
            writer = strided_writer(item)
            key = (
                "pointer_scale"
                if item["schema"].endswith(("v8", "v9", "v10", "v11"))
                else "address_stride"
            )
            writer[key] += 1

        def mutate_reachable_address(item: dict) -> None:
            writer = strided_writer(item)
            if item["schema"].endswith(("v8", "v9", "v10", "v11")):
                writer["instances"][0]["address"] += 1
            else:
                writer["reachable_addresses"].pop()

        def mutate_reachable_value(item: dict) -> None:
            writer = strided_writer(item)
            if item["schema"].endswith(("v8", "v9", "v10", "v11")):
                writer["instances"][0]["inner_induction_value"] += 1
            else:
                writer["reachable_index_values"][0] = 1

        def mutate_residue(item: dict) -> None:
            writer = strided_writer(item)
            if item["schema"].endswith(("v8", "v9", "v10", "v11")):
                writer["affine_index"]["inner_scale"] += 1
            else:
                writer["covered_residues"].pop()

        def mutate_residue_type(item: dict) -> None:
            writer = strided_writer(item)
            if item["schema"].endswith(("v8", "v9", "v10", "v11")):
                writer["pointer_scale"] = True
            else:
                writer["covered_residues"][-1] = True

        for mutate in (
            mutate_stride,
            mutate_reachable_address,
            mutate_reachable_value,
            mutate_residue,
            mutate_residue_type,
            lambda item: (
                item["witnesses"][0][
                    "last_write_cases"
                    if item.get("schema").endswith(
                        ("v7", "v8", "v9", "v10", "v11")
                    )
                    else "alternatives"
                ][0].__setitem__(
                    "writer_lane",
                    item["witnesses"][0][
                        "last_write_cases"
                        if item.get("schema").endswith(
                            ("v7", "v8", "v9", "v10", "v11")
                        )
                        else "alternatives"
                    ][0]["writer_lane"] + 1,
                )
                if item.get("schema").endswith(
                    ("v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11")
                )
                else item["witnesses"][0].__setitem__(
                    "writer_lane", item["witnesses"][0]["writer_lane"] + 1
                )
            ),
        ):
            candidate = copy.deepcopy(artifact)
            mutate(strided_transcript(candidate))
            mutations.append(candidate)
        bad_pointer_order = copy.deepcopy(artifact)
        order_transcript = strided_transcript(bad_pointer_order)
        writer_block = strided_writer(order_transcript)["block"]
        for function in bad_pointer_order["functions"].values():
            block = function["blocks"].get(writer_block)
            if not isinstance(block, list):
                continue
            pointer_position = next(
                (
                    position
                    for position, instruction in enumerate(block)
                    if instruction.get("op") == "pointer_offset"
                ),
                None,
            )
            store_position = next(
                (
                    position
                    for position, instruction in enumerate(block)
                    if instruction.get("op") == "store"
                ),
                None,
            )
            if pointer_position is None or store_position is None:
                continue
            pointer = block.pop(pointer_position)
            store_position = next(
                position
                for position, instruction in enumerate(block)
                if instruction.get("op") == "store"
            )
            block.insert(store_position + 1, pointer)
            break
        mutations.append(bad_pointer_order)
        for mutation in mutations:
            expect_strided_tamper_rejected(mutation)
    if args.expect_conditional_loop_memoryphi_byte_lane_induction:
        capabilities = set(lowering.get("capabilities", ()))
        conditional_loads = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("initialization_loop_memoryphi", {}).get("schema")
            == "symcc-loop-memoryphi-byte-lane-induction-v3"
        ]
        if (
            "bounded-conditional-loop-memoryphi-byte-lane-induction" not in capabilities
            or not conditional_loads
        ):
            raise AssertionError(
                "conditional loop MemoryPhi byte-lane contract is incomplete"
            )

        def expect_conditional_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError("conditional loop MemoryPhi tamper was accepted")

        def conditional_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get("schema")
                == "symcc-loop-memoryphi-byte-lane-induction-v3"
            )

        conditional_mutations: list[dict] = []
        missing_conditional_capability = copy.deepcopy(artifact)
        missing_conditional_capability["lowering"]["capabilities"].remove(
            "bounded-conditional-loop-memoryphi-byte-lane-induction"
        )
        conditional_mutations.append(missing_conditional_capability)
        for mutate in (
            lambda item: item["writer_guard"].__setitem__(
                "writer_when", not item["writer_guard"]["writer_when"]
            ),
            lambda item: item["writer_guard"].__setitem__(
                "predicate", "eq" if item["writer_guard"]["predicate"] != "eq" else "ne"
            ),
            lambda item: item["writer_guard"].__setitem__(
                "writer", item["writer_guard"]["skip"]
            ),
            lambda item: item["witnesses"][0]["writer_guard"].__setitem__(
                "equals",
                not item["witnesses"][0]["writer_guard"]["equals"],
            ),
            lambda item: item["witnesses"][0]["writer_guard"].__setitem__(
                "variable", {"var": "forged_writer_guard"}
            ),
            lambda item: item["memory_phi"].__setitem__("skip", "writer-memory-def"),
            lambda item: item["loop"].__setitem__("skip", item["loop"]["writer_block"]),
            lambda item: item["writer_guard"].__setitem__(
                "left", {"const": 0, "bits": 64}
            ),
        ):
            candidate = copy.deepcopy(artifact)
            mutate(conditional_transcript(candidate))
            conditional_mutations.append(candidate)

        skip_store = copy.deepcopy(artifact)
        skip_transcript = conditional_transcript(skip_store)
        skip_name = skip_transcript["loop"]["skip"]
        for function in skip_store["functions"].values():
            skip_values = function["blocks"].get(skip_name)
            if not isinstance(skip_values, list):
                continue
            skip_values.insert(
                -1,
                {
                    "op": "store",
                    "address": {"const": skip_transcript["base"], "bits": 64},
                    "value": {"const": 0, "bits": 8},
                    "bits": 8,
                    "bytes": 1,
                },
            )
            break
        conditional_mutations.append(skip_store)

        mismatched_guard_width = copy.deepcopy(artifact)
        mismatched_transcript = conditional_transcript(mismatched_guard_width)
        mismatched_decision = mismatched_transcript["loop"]["decision"]
        mismatched_guard = mismatched_transcript["writer_guard"]["variable"]["var"]
        mismatched_right = {"const": 0, "bits": 32}
        for function in mismatched_guard_width["functions"].values():
            decision_values = function["blocks"].get(mismatched_decision)
            if not isinstance(decision_values, list):
                continue
            definition = next(
                item for item in decision_values if item.get("dst") == mismatched_guard
            )
            definition["right"] = copy.deepcopy(mismatched_right)
            mismatched_transcript["writer_guard"]["right"] = mismatched_right
            break
        conditional_mutations.append(mismatched_guard_width)

        out_of_range_guard_constant = copy.deepcopy(artifact)
        out_of_range_transcript = conditional_transcript(out_of_range_guard_constant)
        out_of_range_decision = out_of_range_transcript["loop"]["decision"]
        out_of_range_guard = out_of_range_transcript["writer_guard"]["variable"]["var"]
        out_of_range_right = {"const": 1 << 64, "bits": 64}
        for function in out_of_range_guard_constant["functions"].values():
            decision_values = function["blocks"].get(out_of_range_decision)
            if not isinstance(decision_values, list):
                continue
            definition = next(
                item
                for item in decision_values
                if item.get("dst") == out_of_range_guard
            )
            definition["right"] = copy.deepcopy(out_of_range_right)
            out_of_range_transcript["writer_guard"]["right"] = out_of_range_right
            break
        conditional_mutations.append(out_of_range_guard_constant)

        late_guard_operand = copy.deepcopy(artifact)
        late_transcript = conditional_transcript(late_guard_operand)
        late_writer = late_transcript["loop"]["writer_block"]
        late_decision = late_transcript["loop"]["decision"]
        late_guard = late_transcript["writer_guard"]["variable"]["var"]
        for function in late_guard_operand["functions"].values():
            writer_values = function["blocks"].get(late_writer)
            decision_values = function["blocks"].get(late_decision)
            if not isinstance(writer_values, list) or not isinstance(
                decision_values, list
            ):
                continue
            store = next(item for item in writer_values if item.get("op") == "store")
            late_operand = copy.deepcopy(store["address"])
            definition = next(
                item for item in decision_values if item.get("dst") == late_guard
            )
            definition["left"] = copy.deepcopy(late_operand)
            late_transcript["writer_guard"]["left"] = late_operand
            break
        conditional_mutations.append(late_guard_operand)

        missing_conditional_transcript = copy.deepcopy(artifact)
        for function in missing_conditional_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get("initialization_loop_memoryphi", {}).get(
                        "schema"
                    ) == ("symcc-loop-memoryphi-byte-lane-induction-v3"):
                        instruction.pop("initialization_loop_memoryphi")
        conditional_mutations.append(missing_conditional_transcript)
        for mutation in conditional_mutations:
            expect_conditional_tamper_rejected(mutation)
    if args.expect_multilatch_loop_memoryphi_fixed_point:
        capabilities = set(lowering.get("capabilities", ()))
        multilatch_loads = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("initialization_loop_memoryphi", {}).get("schema")
            in {
                "symcc-loop-memoryphi-byte-lane-induction-v4",
                "symcc-loop-memoryphi-byte-lane-induction-v5",
            }
        ]
        if (
            "bounded-multilatch-loop-memoryphi-byte-lane-fixed-point"
            not in capabilities
            or not multilatch_loads
        ):
            raise AssertionError(
                "multi-latch loop MemoryPhi fixed-point contract is incomplete"
            )

        def multilatch_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get("schema")
                in {
                    "symcc-loop-memoryphi-byte-lane-induction-v4",
                    "symcc-loop-memoryphi-byte-lane-induction-v5",
                }
            )

        def expect_multilatch_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError("multi-latch loop MemoryPhi tamper was accepted")

        multilatch_mutations: list[dict] = []
        missing_multilatch_capability = copy.deepcopy(artifact)
        missing_multilatch_capability["lowering"]["capabilities"].remove(
            "bounded-multilatch-loop-memoryphi-byte-lane-fixed-point"
        )
        multilatch_mutations.append(missing_multilatch_capability)
        for mutate in (
            lambda item: item["memory_phi"]["incoming"][0].__setitem__(
                "kind", "header-memory-phi-carry"
            ),
            lambda item: item["transfers"][0]["guards"][0].__setitem__(
                "equals", not item["transfers"][0]["guards"][0]["equals"]
            ),
            lambda item: item["decisions"][0].__setitem__("predicate", "eq"),
            lambda item: item["transfers"].reverse(),
            lambda item: item["fixed_point"]["domain"].pop(),
            lambda item: item["fixed_point"]["rounds"][0].__setitem__(
                "new_lanes",
                item["fixed_point"]["rounds"][0]["new_lanes"] + 1,
            ),
            lambda item: item["fixed_point"]["rounds"][0].__setitem__(
                "new_lanes", True
            ),
            lambda item: item["fixed_point"]["final_lanes"].pop(),
            lambda item: item["fixed_point"].__setitem__("stable", False),
            lambda item: item["witnesses"][0]["alternatives"][0].__setitem__(
                "transfer", len(item["transfers"]) - 1
            ),
            lambda item: item["witnesses"][0]["alternatives"].append(
                copy.deepcopy(item["witnesses"][0]["alternatives"][0])
            ),
        ):
            candidate = copy.deepcopy(artifact)
            mutate(multilatch_transcript(candidate))
            multilatch_mutations.append(candidate)

        bad_next_definition = copy.deepcopy(artifact)
        next_transcript = multilatch_transcript(bad_next_definition)
        next_latch = next_transcript["transfers"][0]["latch"]
        next_var = next_transcript["transfers"][0]["next"]["var"]
        for function in bad_next_definition["functions"].values():
            values = function["blocks"].get(next_latch)
            if not isinstance(values, list):
                continue
            next(item for item in values if item.get("dst") == next_var)["right"][
                "const"
            ] += 1
            break
        multilatch_mutations.append(bad_next_definition)

        bad_writer_alias = copy.deepcopy(artifact)
        alias_transcript = multilatch_transcript(bad_writer_alias)
        writer_transfer = next(
            transfer
            for transfer in alias_transcript["transfers"]
            if transfer["kind"] == "writer"
        )
        for function in bad_writer_alias["functions"].values():
            values = function["blocks"].get(writer_transfer["latch"])
            if not isinstance(values, list):
                continue
            next(item for item in values if item.get("op") == "store")["aliases"][
                0
            ] += 1
            break
        multilatch_mutations.append(bad_writer_alias)

        bad_guard_width = copy.deepcopy(artifact)
        width_transcript = multilatch_transcript(bad_guard_width)
        raw_decision = width_transcript["decisions"][0]
        decision_block = raw_decision["block"]
        decision_var = raw_decision["variable"]["var"]
        mismatched_operand = {"const": 0, "bits": 32}
        for function in bad_guard_width["functions"].values():
            values = function["blocks"].get(decision_block)
            if not isinstance(values, list):
                continue
            next(item for item in values if item.get("dst") == decision_var)[
                "right"
            ] = copy.deepcopy(mismatched_operand)
            raw_decision["right"] = mismatched_operand
            break
        multilatch_mutations.append(bad_guard_width)

        pointer_guard_operand = copy.deepcopy(artifact)
        pointer_transcript = multilatch_transcript(pointer_guard_operand)
        pointer_decision = pointer_transcript["decisions"][0]
        pointer_block = pointer_decision["block"]
        pointer_var = pointer_decision["variable"]["var"]
        writer_latch = next(
            transfer["latch"]
            for transfer in pointer_transcript["transfers"]
            if transfer["kind"] == "writer"
        )
        preheader = pointer_transcript["loop"]["preheader"]
        for function in pointer_guard_operand["functions"].values():
            writer_values = function["blocks"].get(writer_latch)
            preheader_values = function["blocks"].get(preheader)
            decision_values = function["blocks"].get(pointer_block)
            if (
                not isinstance(writer_values, list)
                or not isinstance(preheader_values, list)
                or not isinstance(decision_values, list)
            ):
                continue
            pointer_definition = copy.deepcopy(
                next(
                    item for item in writer_values if item.get("op") == "pointer_offset"
                )
            )
            pointer_definition["dst"] = "forged_pointer_guard_operand"
            preheader_values.insert(-1, pointer_definition)
            forged_operand = {"var": pointer_definition["dst"]}
            next(item for item in decision_values if item.get("dst") == pointer_var)[
                "left"
            ] = copy.deepcopy(forged_operand)
            pointer_decision["left"] = forged_operand
            break
        multilatch_mutations.append(pointer_guard_operand)

        missing_multilatch_transcript = copy.deepcopy(artifact)
        for function in missing_multilatch_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get("initialization_loop_memoryphi", {}).get(
                        "schema"
                    ) in {
                        "symcc-loop-memoryphi-byte-lane-induction-v4",
                        "symcc-loop-memoryphi-byte-lane-induction-v5",
                    }:
                        instruction.pop("initialization_loop_memoryphi")
        multilatch_mutations.append(missing_multilatch_transcript)
        for mutation in multilatch_mutations:
            expect_multilatch_tamper_rejected(mutation)
    if args.expect_ordered_multilatch_writer_transfer:
        capabilities = set(lowering.get("capabilities", ()))
        ordered_loads = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("initialization_loop_memoryphi", {}).get("schema")
            == "symcc-loop-memoryphi-byte-lane-induction-v5"
        ]
        ordered_capability = "bounded-multilatch-loop-memoryphi-ordered-writer-transfer"
        if ordered_capability not in capabilities or not ordered_loads:
            raise AssertionError(
                "ordered multi-latch writer-transfer contract is incomplete"
            )

        def ordered_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get("schema")
                == "symcc-loop-memoryphi-byte-lane-induction-v5"
            )

        def ordered_writer_transfer(item: dict) -> dict:
            return next(
                transfer
                for transfer in item["transfers"]
                if transfer["kind"] == "writer" and len(transfer["writers"]) > 1
            )

        def expect_ordered_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError(
                    "ordered multi-latch writer-transfer tamper was accepted"
                )

        ordered_mutations: list[dict] = []
        missing_ordered_capability = copy.deepcopy(artifact)
        missing_ordered_capability["lowering"]["capabilities"].remove(
            ordered_capability
        )
        ordered_mutations.append(missing_ordered_capability)
        for mutate in (
            lambda item: item.__setitem__(
                "schema", "symcc-loop-memoryphi-byte-lane-induction-v4"
            ),
            lambda item: item["fixed_point"].__setitem__(
                "semantics", "mutually-exclusive-writer-transfer"
            ),
            lambda item: item["memory_phi"]["incoming"][0].__setitem__(
                "kind", "writer-memory-def"
            ),
            lambda item: ordered_writer_transfer(item)["writers"].reverse(),
            lambda item: ordered_writer_transfer(item)["writers"].pop(),
            lambda item: ordered_writer_transfer(item)["writers"].append(
                copy.deepcopy(ordered_writer_transfer(item)["writers"][-1])
            ),
            lambda item: ordered_writer_transfer(item)["writers"][0].__setitem__(
                "ordinal", 1
            ),
            lambda item: item["witnesses"][0]["alternatives"][0].__setitem__(
                "writer", len(ordered_writer_transfer(item)["writers"])
            ),
            lambda item: item["witnesses"][0]["alternatives"].pop(),
        ):
            candidate = copy.deepcopy(artifact)
            mutate(ordered_transcript(candidate))
            ordered_mutations.append(candidate)

        source_writers = ordered_writer_transfer(ordered_transcript(artifact))[
            "writers"
        ]
        writer_shapes = []
        for source_writer in source_writers[:2]:
            shape = copy.deepcopy(source_writer)
            shape.pop("ordinal")
            writer_shapes.append(shape)
        if len(writer_shapes) == 2 and writer_shapes[0] != writer_shapes[1]:
            swapped_stores = copy.deepcopy(artifact)
            swapped_transcript = ordered_transcript(swapped_stores)
            writer_block = ordered_writer_transfer(swapped_transcript)["latch"]
            for function in swapped_stores["functions"].values():
                values = function["blocks"].get(writer_block)
                if not isinstance(values, list):
                    continue
                positions = [
                    index
                    for index, instruction in enumerate(values)
                    if instruction.get("op") == "store"
                ]
                if len(positions) >= 2:
                    first, second = positions[:2]
                    values[first], values[second] = (
                        values[second],
                        values[first],
                    )
                    break
            ordered_mutations.append(swapped_stores)

        singleton_v5 = copy.deepcopy(artifact)
        singleton_transcript = ordered_transcript(singleton_v5)
        singleton_transfer = ordered_writer_transfer(singleton_transcript)
        singleton_transfer["writers"].pop()
        singleton_latch = singleton_transfer["latch"]
        for witness in singleton_transcript["witnesses"]:
            witness["alternatives"] = [
                alternative
                for alternative in witness["alternatives"]
                if alternative["writer"] == 0
            ]
        for function in singleton_v5["functions"].values():
            values = function["blocks"].get(singleton_latch)
            if not isinstance(values, list):
                continue
            positions = [
                index
                for index, instruction in enumerate(values)
                if instruction.get("op") == "store"
            ]
            if len(positions) >= 2:
                values.pop(positions[1])
                break
        ordered_mutations.append(singleton_v5)

        missing_ordered_transcript = copy.deepcopy(artifact)
        for function in missing_ordered_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get("initialization_loop_memoryphi", {}).get(
                        "schema"
                    ) == ("symcc-loop-memoryphi-byte-lane-induction-v5"):
                        instruction.pop("initialization_loop_memoryphi")
        ordered_mutations.append(missing_ordered_transcript)
        for mutation in ordered_mutations:
            expect_ordered_tamper_rejected(mutation)
    if args.expect_nested_loop_memoryphi_summary_composition:
        capabilities = set(lowering.get("capabilities", ()))
        nested_capability = (
            "bounded-nested-loop-memoryphi-summary-composition"
        )
        nested_loads = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("initialization_loop_memoryphi", {}).get(
                "schema"
            ) in {
                "symcc-loop-memoryphi-byte-lane-induction-v6",
                "symcc-loop-memoryphi-byte-lane-induction-v7",
                "symcc-loop-memoryphi-byte-lane-induction-v8",
                "symcc-loop-memoryphi-byte-lane-induction-v9",
                "symcc-loop-memoryphi-byte-lane-induction-v10",
                "symcc-loop-memoryphi-byte-lane-induction-v11",
            }
        ]
        if nested_capability not in capabilities or not nested_loads:
            raise AssertionError(
                "nested-loop MemoryPhi summary-composition contract is "
                "incomplete"
            )

        def nested_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get(
                    "schema"
                ) in {
                    "symcc-loop-memoryphi-byte-lane-induction-v6",
                    "symcc-loop-memoryphi-byte-lane-induction-v7",
                    "symcc-loop-memoryphi-byte-lane-induction-v8",
                    "symcc-loop-memoryphi-byte-lane-induction-v9",
                    "symcc-loop-memoryphi-byte-lane-induction-v10",
                    "symcc-loop-memoryphi-byte-lane-induction-v11",
                }
            )

        def expect_nested_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError(
                    "nested-loop MemoryPhi summary tamper was accepted"
                )

        nested_mutations: list[dict] = []
        missing_nested_capability = copy.deepcopy(artifact)
        missing_nested_capability["lowering"]["capabilities"].remove(
            nested_capability
        )
        nested_mutations.append(missing_nested_capability)
        for mutate in (
            lambda item: item.__setitem__(
                "schema", "symcc-loop-memoryphi-byte-lane-induction-v5"
            ),
            lambda item: item["memory_phis"].__setitem__(
                "equation", "H_outer=phi(entry,H_outer)"
            ),
            lambda item: item["memory_phis"]["outer"].__setitem__(
                "backedge", "ordered-writer-memory-def-chain"
            ),
            lambda item: item["memory_phis"]["inner"].__setitem__(
                "preheader", "live-on-entry"
            ),
            lambda item: item["loops"]["outer"].__setitem__(
                "header", item["loops"]["inner"]["header"]
            ),
            lambda item: item["loops"]["inner"].__setitem__(
                "body_edge", item["loops"]["inner"]["preheader_edge"]
            ),
            lambda item: item["outer_induction"].__setitem__(
                "variable", copy.deepcopy(item["inner_induction"]["variable"])
            ),
            lambda item: item["inner_induction"].__setitem__(
                "step", item["inner_induction"]["step"] + 1
            ),
            lambda item: item["outer_guard"].__setitem__("predicate", "eq"),
            lambda item: item["summary"].__setitem__("order", "outer-to-inner"),
            lambda item: item["summary"].__setitem__("input", "live-on-entry"),
            lambda item: item["summary"].__setitem__("output", "outer-memory-phi"),
            lambda item: item["summary"]["fixed_point"].__setitem__(
                "semantics", "unordered-writer-summary"
            ),
            lambda item: item["summary"]["writers"][0].__setitem__(
                "ordinal", 1
            ),
            lambda item: item["summary"]["writers"].append(
                copy.deepcopy(item["summary"]["writers"][0])
            ),
            lambda item: item["summary"]["fixed_point"]["domain"].pop(),
            lambda item: item["summary"]["fixed_point"]["rounds"][0].__setitem__(
                "new_lanes",
                item["summary"]["fixed_point"]["rounds"][0]["new_lanes"] + 1,
            ),
            lambda item: item["summary"]["fixed_point"]["rounds"][0].__setitem__(
                "new_lanes", True
            ),
            lambda item: item["witnesses"][0][
                "last_write_cases"
                if "last_write_cases" in item["witnesses"][0]
                else "alternatives"
            ][0].__setitem__(
                "writer", len(item["summary"]["writers"])
            ),
            lambda item: item["witnesses"][0][
                "last_write_cases"
                if "last_write_cases" in item["witnesses"][0]
                else "alternatives"
            ].append(copy.deepcopy(item["witnesses"][0][
                "last_write_cases"
                if "last_write_cases" in item["witnesses"][0]
                else "alternatives"
            ][0])),
        ):
            candidate = copy.deepcopy(artifact)
            mutate(nested_transcript(candidate))
            nested_mutations.append(candidate)

        bad_writer_alias = copy.deepcopy(artifact)
        alias_transcript = nested_transcript(bad_writer_alias)
        writer_block = alias_transcript["loops"]["inner"]["body"]
        for function in bad_writer_alias["functions"].values():
            values = function["blocks"].get(writer_block)
            if not isinstance(values, list):
                continue
            next(item for item in values if item.get("op") == "store")[
                "aliases"
            ][0] += 1
            break
        nested_mutations.append(bad_writer_alias)

        bad_inner_next = copy.deepcopy(artifact)
        next_transcript = nested_transcript(bad_inner_next)
        inner_body = next_transcript["loops"]["inner"]["body"]
        inner_next = next_transcript["inner_induction"]["next"]["var"]
        for function in bad_inner_next["functions"].values():
            values = function["blocks"].get(inner_body)
            if not isinstance(values, list):
                continue
            next(item for item in values if item.get("dst") == inner_next)[
                "right"
            ]["const"] += 1
            break
        nested_mutations.append(bad_inner_next)

        bad_load_pointer = copy.deepcopy(artifact)
        load_transcript = nested_transcript(bad_load_pointer)
        load_block = load_transcript["loops"]["outer"]["exit"]
        for function in bad_load_pointer["functions"].values():
            values = function["blocks"].get(load_block)
            if not isinstance(values, list):
                continue
            load = next(
                item for item in values
                if item.get("initialization_loop_memoryphi", {}).get("schema")
                in {
                    "symcc-loop-memoryphi-byte-lane-induction-v6",
                    "symcc-loop-memoryphi-byte-lane-induction-v7",
                    "symcc-loop-memoryphi-byte-lane-induction-v8",
                    "symcc-loop-memoryphi-byte-lane-induction-v9",
                    "symcc-loop-memoryphi-byte-lane-induction-v10",
                    "symcc-loop-memoryphi-byte-lane-induction-v11",
                }
            )
            address_var = load["address"]["var"]
            pointer = next(item for item in values if item.get("dst") == address_var)
            pointer["scale"]["const"] += 1
            break
        nested_mutations.append(bad_load_pointer)

        late_inner_bound = copy.deepcopy(artifact)
        bound_transcript = nested_transcript(late_inner_bound)
        bound_name = bound_transcript["inner_guard"]["bound"]["var"]
        preheader = bound_transcript["loops"]["outer"]["preheader"]
        exit_block = bound_transcript["loops"]["outer"]["exit"]
        for function in late_inner_bound["functions"].values():
            preheader_values = function["blocks"].get(preheader)
            exit_values = function["blocks"].get(exit_block)
            if not isinstance(preheader_values, list) or not isinstance(
                exit_values, list
            ):
                continue
            position = next(
                index for index, item in enumerate(preheader_values)
                if item.get("dst") == bound_name
            )
            definition = preheader_values.pop(position)
            exit_values.insert(0, definition)
            break
        nested_mutations.append(late_inner_bound)

        source_writers = nested_transcript(artifact)["summary"]["writers"]
        writer_shapes = []
        for source_writer in source_writers[:2]:
            shape = copy.deepcopy(source_writer)
            shape.pop("ordinal")
            writer_shapes.append(shape)
        if len(writer_shapes) == 2 and writer_shapes[0] != writer_shapes[1]:
            reordered_writers = copy.deepcopy(artifact)
            nested_transcript(reordered_writers)["summary"]["writers"].reverse()
            nested_mutations.append(reordered_writers)

            reordered_stores = copy.deepcopy(artifact)
            store_transcript = nested_transcript(reordered_stores)
            store_block = store_transcript["loops"]["inner"]["body"]
            for function in reordered_stores["functions"].values():
                values = function["blocks"].get(store_block)
                if not isinstance(values, list):
                    continue
                positions = [
                    index for index, item in enumerate(values)
                    if item.get("op") == "store"
                ]
                if len(positions) >= 2:
                    first, second = positions[:2]
                    values[first], values[second] = values[second], values[first]
                    break
            nested_mutations.append(reordered_stores)

        missing_nested_transcript = copy.deepcopy(artifact)
        for function in missing_nested_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get("initialization_loop_memoryphi", {}).get(
                        "schema"
                    ) in {
                        "symcc-loop-memoryphi-byte-lane-induction-v6",
                        "symcc-loop-memoryphi-byte-lane-induction-v7",
                        "symcc-loop-memoryphi-byte-lane-induction-v8",
                        "symcc-loop-memoryphi-byte-lane-induction-v9",
                        "symcc-loop-memoryphi-byte-lane-induction-v10",
                        "symcc-loop-memoryphi-byte-lane-induction-v11",
                    }:
                        instruction.pop("initialization_loop_memoryphi")
        nested_mutations.append(missing_nested_transcript)
        for mutation in nested_mutations:
            expect_nested_tamper_rejected(mutation)
    if args.expect_nested_loop_memoryphi_last_write_value_summary:
        capabilities = set(lowering.get("capabilities", ()))
        value_capability = (
            "bounded-nested-loop-memoryphi-last-write-value-summary"
        )

        def value_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get(
                    "schema"
                ) in {
                    "symcc-loop-memoryphi-byte-lane-induction-v7",
                    "symcc-loop-memoryphi-byte-lane-induction-v8",
                    "symcc-loop-memoryphi-byte-lane-induction-v9",
                    "symcc-loop-memoryphi-byte-lane-induction-v10",
                    "symcc-loop-memoryphi-byte-lane-induction-v11",
                }
            )

        try:
            source_value_transcript = value_transcript(artifact)
        except StopIteration as exc:
            raise AssertionError(
                "nested-loop MemoryPhi last-write value summary is missing"
            ) from exc
        if value_capability not in capabilities:
            raise AssertionError(
                "nested-loop MemoryPhi last-write value capability is missing"
            )

        def expect_value_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError(
                    "nested-loop MemoryPhi last-write value tamper was accepted"
                )

        value_mutations: list[dict] = []
        missing_value_capability = copy.deepcopy(artifact)
        missing_value_capability["lowering"]["capabilities"].remove(
            value_capability
        )
        value_mutations.append(missing_value_capability)
        value_mutators = [
            lambda item: item.__setitem__(
                "schema", "symcc-loop-memoryphi-byte-lane-induction-v6"
            ),
            lambda item: item["summary"]["value_semantics"].__setitem__(
                "kind", "symbolic-last-write"
            ),
            lambda item: item["summary"]["value_semantics"].__setitem__(
                "endianness", "unknown"
            ),
            lambda item: item["summary"]["value_semantics"].__setitem__(
                "case_order", "ascending-inner-induction"
            ),
            lambda item: item["summary"]["value_semantics"].__setitem__(
                "case_predicate", "inner-bound-greater-than-minimum"
            ),
            lambda item: item["summary"]["value_semantics"].__setitem__(
                "outer_activation", "always"
            ),
            lambda item: item["witnesses"][0]["last_write_cases"][0].__setitem__(
                "minimum_inner_bound",
                item["witnesses"][0]["last_write_cases"][0][
                    "minimum_inner_bound"
                ] + 1,
            ),
            lambda item: item["witnesses"][0]["last_write_cases"][0].__setitem__(
                "minimum_inner_bound", True
            ),
        ]
        if not source_value_transcript["schema"].endswith(("v9", "v10", "v11")):
            value_mutators.extend([
                lambda item: item["summary"]["writers"][0][
                    "stored_value"
                ].__setitem__("kind", "constant-byte-vector"),
                lambda item: item["summary"]["writers"][0][
                    "stored_value"
                ].__setitem__(
                    "bits",
                    item["summary"]["writers"][0]["stored_value"]["bits"]
                    + 1,
                ),
                lambda item: item["summary"]["writers"][0][
                    "stored_value"
                ].__setitem__(
                    "operand",
                    item["summary"]["writers"][0]["stored_value"]["operand"]
                    + 1,
                ),
                lambda item: item["summary"]["writers"][0][
                    "stored_value"
                ]["bytes"].__setitem__(
                    0,
                    (
                        item["summary"]["writers"][0]["stored_value"]
                        ["bytes"][0] + 1
                    ) % 256,
                ),
                lambda item: item["witnesses"][0]["last_write_cases"][0]
                .__setitem__(
                    "value_byte",
                    (
                        item["witnesses"][0]["last_write_cases"][0]
                        ["value_byte"] + 1
                    ) % 256,
                ),
            ])
        for mutate in value_mutators:
            candidate = copy.deepcopy(artifact)
            mutate(value_transcript(candidate))
            value_mutations.append(candidate)

        source_cases = source_value_transcript["witnesses"][0][
            "last_write_cases"
        ]
        if len(source_cases) >= 2:
            reordered_cases = copy.deepcopy(artifact)
            cases = value_transcript(reordered_cases)["witnesses"][0][
                "last_write_cases"
            ]
            cases[0], cases[1] = cases[1], cases[0]
            value_mutations.append(reordered_cases)

        changed_store = copy.deepcopy(artifact)
        changed_transcript = value_transcript(changed_store)
        writer_block = changed_transcript["loops"]["inner"]["body"]
        for function in changed_store["functions"].values():
            values = function["blocks"].get(writer_block)
            if not isinstance(values, list):
                continue
            store = next((
                item for item in values
                if item.get("op") == "store"
                and isinstance(item.get("value"), dict)
                and "const" in item["value"]
            ), None)
            if store is not None:
                store["value"]["const"] += 1
            else:
                symbolic = next(
                    writer["stored_value"]
                    for writer in changed_transcript["summary"]["writers"]
                    if writer.get("stored_value", {}).get("kind")
                    in {
                        "affine-bitvector", "piecewise-affine-bitvector",
                        "affine-decision-dag-bitvector",
                    }
                )
                definition = next(
                    item for item in values
                    if item.get("dst") == symbolic["variable"]["var"]
                )
                if symbolic["kind"] == "affine-bitvector":
                    definition["operator"] = "sub"
                else:
                    definition["true"], definition["false"] = (
                        definition["false"], definition["true"]
                    )
            break
        value_mutations.append(changed_store)

        missing_value_transcript = copy.deepcopy(artifact)
        for function in missing_value_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get(
                        "initialization_loop_memoryphi", {}
                    ).get("schema") in {
                        "symcc-loop-memoryphi-byte-lane-induction-v7",
                        "symcc-loop-memoryphi-byte-lane-induction-v8",
                        "symcc-loop-memoryphi-byte-lane-induction-v9",
                        "symcc-loop-memoryphi-byte-lane-induction-v10",
                        "symcc-loop-memoryphi-byte-lane-induction-v11",
                    }:
                        instruction.pop("initialization_loop_memoryphi")
        value_mutations.append(missing_value_transcript)
        for mutation in value_mutations:
            expect_value_tamper_rejected(mutation)
    if args.expect_nested_loop_memoryphi_two_dimensional_affine_summary:
        capabilities = set(lowering.get("capabilities", ()))
        affine_capability = (
            "bounded-nested-loop-memoryphi-two-dimensional-affine-summary"
        )

        def affine_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get(
                    "schema"
                ) in {
                    "symcc-loop-memoryphi-byte-lane-induction-v8",
                    "symcc-loop-memoryphi-byte-lane-induction-v9",
                    "symcc-loop-memoryphi-byte-lane-induction-v10",
                    "symcc-loop-memoryphi-byte-lane-induction-v11",
                }
            )

        try:
            source_affine_transcript = affine_transcript(artifact)
        except StopIteration as exc:
            raise AssertionError(
                "nested-loop MemoryPhi two-dimensional affine summary is "
                "missing"
            ) from exc
        if affine_capability not in capabilities:
            raise AssertionError(
                "nested-loop MemoryPhi two-dimensional affine capability is "
                "missing"
            )

        def expect_affine_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError(
                    "nested-loop MemoryPhi two-dimensional affine tamper was "
                    "accepted"
                )

        affine_mutations: list[dict] = []
        missing_affine_capability = copy.deepcopy(artifact)
        missing_affine_capability["lowering"]["capabilities"].remove(
            affine_capability
        )
        affine_mutations.append(missing_affine_capability)
        for mutate in (
            lambda item: item.__setitem__(
                "schema", "symcc-loop-memoryphi-byte-lane-induction-v7"
            ),
            lambda item: item["summary"]["value_semantics"].__setitem__(
                "case_order", "descending-inner-induction"
            ),
            lambda item: item["summary"]["writers"][0].__setitem__(
                "pointer_base",
                item["summary"]["writers"][0]["pointer_base"] + 1,
            ),
            lambda item: item["summary"]["writers"][0].__setitem__(
                "pointer_scale",
                item["summary"]["writers"][0]["pointer_scale"] + 1,
            ),
            lambda item: item["summary"]["writers"][0][
                "affine_index"
            ].__setitem__(
                "constant",
                item["summary"]["writers"][0]["affine_index"]["constant"]
                + 1,
            ),
            lambda item: item["summary"]["writers"][0][
                "affine_index"
            ].__setitem__(
                "outer_scale",
                item["summary"]["writers"][0]["affine_index"]["outer_scale"]
                + 1,
            ),
            lambda item: item["summary"]["writers"][0][
                "affine_index"
            ].__setitem__(
                "inner_scale",
                item["summary"]["writers"][0]["affine_index"]["inner_scale"]
                + 1,
            ),
            lambda item: item["summary"]["writers"][0]["instances"][0].__setitem__(
                "outer_induction_value", 1
            ),
            lambda item: item["summary"]["writers"][0]["instances"][0].__setitem__(
                "inner_induction_value", 1
            ),
            lambda item: item["summary"]["writers"][0]["instances"][0].__setitem__(
                "index_value", 1
            ),
            lambda item: item["summary"]["writers"][0]["instances"][0].__setitem__(
                "address",
                item["summary"]["writers"][0]["instances"][0]["address"] + 1,
            ),
            lambda item: item["summary"]["fixed_point"]["domain"][0].__setitem__(
                "outer_induction_value", 1
            ),
            lambda item: item["summary"]["fixed_point"]["rounds"][0].__setitem__(
                "inner_induction_value", 1
            ),
            lambda item: item["witnesses"][0]["last_write_cases"][0].__setitem__(
                "minimum_outer_bound",
                item["witnesses"][0]["last_write_cases"][0][
                    "minimum_outer_bound"
                ] + 1,
            ),
        ):
            candidate = copy.deepcopy(artifact)
            mutate(affine_transcript(candidate))
            affine_mutations.append(candidate)

        changed_affine_expression = copy.deepcopy(artifact)
        changed_expression_transcript = affine_transcript(
            changed_affine_expression
        )
        writer_block = changed_expression_transcript["loops"]["inner"]["body"]
        affine_name = changed_expression_transcript["summary"]["writers"][0][
            "affine_index"
        ]["variable"]["var"]
        for function in changed_affine_expression["functions"].values():
            values = function["blocks"].get(writer_block)
            if not isinstance(values, list):
                continue
            affine_definition = next(
                item for item in values if item.get("dst") == affine_name
            )
            affine_definition["operator"] = "sub"
            break
        affine_mutations.append(changed_affine_expression)

        changed_pointer_base = copy.deepcopy(artifact)
        pointer_transcript = affine_transcript(changed_pointer_base)
        writer_block = pointer_transcript["loops"]["inner"]["body"]
        affine_name = pointer_transcript["summary"]["writers"][0][
            "affine_index"
        ]["variable"]["var"]
        for function in changed_pointer_base["functions"].values():
            values = function["blocks"].get(writer_block)
            if not isinstance(values, list):
                continue
            pointer = next(
                item for item in values
                if item.get("op") == "pointer_offset"
                and item.get("index") == {"var": affine_name}
            )
            pointer["base"]["const"] += 1
            break
        affine_mutations.append(changed_pointer_base)

        missing_affine_transcript = copy.deepcopy(artifact)
        for function in missing_affine_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get(
                        "initialization_loop_memoryphi", {}
                    ).get("schema") in {
                        "symcc-loop-memoryphi-byte-lane-induction-v8",
                        "symcc-loop-memoryphi-byte-lane-induction-v9",
                        "symcc-loop-memoryphi-byte-lane-induction-v10",
                        "symcc-loop-memoryphi-byte-lane-induction-v11",
                    }:
                        instruction.pop("initialization_loop_memoryphi")
        affine_mutations.append(missing_affine_transcript)
        if not source_affine_transcript["summary"]["writers"]:
            raise AssertionError(
                "two-dimensional affine summary has no writer"
            )
        for mutation in affine_mutations:
            expect_affine_tamper_rejected(mutation)
    if args.expect_nested_loop_memoryphi_affine_symbolic_value_summary:
        capabilities = set(lowering.get("capabilities", ()))
        symbolic_capability = (
            "bounded-nested-loop-memoryphi-affine-symbolic-value-summary"
        )

        def symbolic_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get(
                    "schema"
                ) == "symcc-loop-memoryphi-byte-lane-induction-v9"
            )

        try:
            source_symbolic_transcript = symbolic_transcript(artifact)
        except StopIteration as exc:
            raise AssertionError(
                "nested-loop MemoryPhi affine symbolic value summary is "
                "missing"
            ) from exc
        if symbolic_capability not in capabilities:
            raise AssertionError(
                "nested-loop MemoryPhi affine symbolic value capability is "
                "missing"
            )
        if not any(
            writer.get("stored_value", {}).get("kind")
            == "affine-bitvector"
            for writer in source_symbolic_transcript["summary"]["writers"]
        ):
            raise AssertionError(
                "affine symbolic value summary has no symbolic writer"
            )

        def expect_symbolic_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError(
                    "nested-loop MemoryPhi affine symbolic value tamper was "
                    "accepted"
                )

        def symbolic_writer(transcript: dict) -> dict:
            return next(
                writer for writer in transcript["summary"]["writers"]
                if writer.get("stored_value", {}).get("kind")
                == "affine-bitvector"
            )

        def symbolic_expression(transcript: dict) -> dict:
            return next(
                case["value_byte_expression"]
                for witness in transcript["witnesses"]
                for case in witness["last_write_cases"]
                if case.get("value_byte_expression", {}).get("kind")
                == "extract-affine-bitvector-byte"
            )

        symbolic_mutations: list[dict] = []
        missing_symbolic_capability = copy.deepcopy(artifact)
        missing_symbolic_capability["lowering"]["capabilities"].remove(
            symbolic_capability
        )
        symbolic_mutations.append(missing_symbolic_capability)
        false_piecewise_upgrade = copy.deepcopy(artifact)
        upgraded_transcript = symbolic_transcript(false_piecewise_upgrade)
        upgraded_transcript["schema"] = (
            "symcc-loop-memoryphi-byte-lane-induction-v10"
        )
        upgraded_transcript["summary"]["value_semantics"]["kind"] = (
            "guard-specialized-piecewise-affine-bitvector-byte-"
            "two-dimensional-last-write"
        )
        false_piecewise_upgrade["lowering"]["capabilities"].append(
            "bounded-nested-loop-memoryphi-piecewise-affine-value-summary"
        )
        symbolic_mutations.append(false_piecewise_upgrade)
        symbolic_mutators = [
            lambda transcript: transcript.__setitem__(
                "schema", "symcc-loop-memoryphi-byte-lane-induction-v8"
            ),
            lambda transcript: symbolic_writer(transcript)[
                "stored_value"
            ].__setitem__("constant", 1),
            lambda transcript: symbolic_writer(transcript)[
                "stored_value"
            ].__setitem__("outer_scale", 0),
            lambda transcript: symbolic_writer(transcript)[
                "stored_value"
            ].__setitem__("inner_scale", 0),
            lambda transcript: symbolic_writer(transcript)[
                "stored_value"
            ].__setitem__("input_scale", 1),
            lambda transcript: symbolic_writer(transcript)[
                "stored_value"
            ].__setitem__("semantics", "mathematical-integer"),
            lambda transcript: symbolic_expression(transcript).__setitem__(
                "constant", symbolic_expression(transcript)["constant"] + 1
            ),
            lambda transcript: symbolic_expression(transcript).__setitem__(
                "low_bit", symbolic_expression(transcript)["low_bit"] + 8
            ),
            lambda transcript: symbolic_expression(transcript).__setitem__(
                "input_scale",
                symbolic_expression(transcript)["input_scale"] + 1,
            ),
        ]
        source_input = symbolic_writer(source_symbolic_transcript)[
            "stored_value"
        ]["input"]
        if source_input is not None:
            symbolic_mutators.extend([
                lambda transcript: symbolic_writer(transcript)[
                    "stored_value"
                ]["input"]["variable"].__setitem__(
                    "var",
                    symbolic_writer(transcript)["stored_value"]["variable"]
                    ["var"],
                ),
                lambda transcript: symbolic_writer(transcript)[
                    "stored_value"
                ]["input"].__setitem__(
                    "offset",
                    symbolic_writer(transcript)["stored_value"]["input"]
                    ["offset"] + 1,
                ),
                lambda transcript: symbolic_writer(transcript)[
                    "stored_value"
                ]["input"].__setitem__(
                    "bytes",
                    symbolic_writer(transcript)["stored_value"]["input"]
                    ["bytes"] + 1,
                ),
            ])
        for mutate in symbolic_mutators:
            candidate = copy.deepcopy(artifact)
            mutate(symbolic_transcript(candidate))
            symbolic_mutations.append(candidate)

        changed_value_expression = copy.deepcopy(artifact)
        changed_transcript = symbolic_transcript(changed_value_expression)
        writer_block = changed_transcript["loops"]["inner"]["body"]
        value_name = symbolic_writer(changed_transcript)["stored_value"][
            "variable"
        ]["var"]
        for function in changed_value_expression["functions"].values():
            values = function["blocks"].get(writer_block)
            if not isinstance(values, list):
                continue
            definition = next(
                item for item in values if item.get("dst") == value_name
            )
            definition["operator"] = "sub"
            break
        symbolic_mutations.append(changed_value_expression)

        if source_input is not None:
            changed_input_abi = copy.deepcopy(artifact)
            input_offset = symbolic_writer(
                symbolic_transcript(changed_input_abi)
            )["stored_value"]["input"]["offset"]
            for function in changed_input_abi["functions"].values():
                changed = False
                for values in function["blocks"].values():
                    for item in values:
                        if (
                            item.get("op") == "input"
                            and item.get("offset") == input_offset
                        ):
                            item["offset"] += 1
                            changed = True
                            break
                    if changed:
                        break
                if changed:
                    break
            symbolic_mutations.append(changed_input_abi)

        missing_symbolic_transcript = copy.deepcopy(artifact)
        for function in missing_symbolic_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get(
                        "initialization_loop_memoryphi", {}
                    ).get("schema") == (
                        "symcc-loop-memoryphi-byte-lane-induction-v9"
                    ):
                        instruction.pop("initialization_loop_memoryphi")
        symbolic_mutations.append(missing_symbolic_transcript)
        for mutation in symbolic_mutations:
            expect_symbolic_tamper_rejected(mutation)
    if args.expect_nested_loop_memoryphi_piecewise_affine_value_summary:
        capabilities = set(lowering.get("capabilities", ()))
        piecewise_capability = (
            "bounded-nested-loop-memoryphi-piecewise-affine-value-summary"
        )
        required_capabilities = {
            "bounded-loop-memoryphi-byte-lane-induction",
            "bounded-nested-loop-memoryphi-summary-composition",
            "bounded-nested-loop-memoryphi-last-write-value-summary",
            "bounded-nested-loop-memoryphi-two-dimensional-affine-summary",
            "bounded-nested-loop-memoryphi-affine-symbolic-value-summary",
            piecewise_capability,
        }

        def piecewise_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get(
                    "schema"
                ) == "symcc-loop-memoryphi-byte-lane-induction-v10"
            )

        def piecewise_writer(transcript: dict) -> dict:
            return next(
                writer for writer in transcript["summary"]["writers"]
                if writer.get("stored_value", {}).get("kind")
                == "piecewise-affine-bitvector"
            )

        def piecewise_expression(transcript: dict) -> dict:
            return next(
                case["value_byte_expression"]
                for witness in transcript["witnesses"]
                for case in witness["last_write_cases"]
                if case.get("value_byte_expression", {}).get("kind")
                == "guard-specialized-byte"
            )

        try:
            source_piecewise_transcript = piecewise_transcript(artifact)
            source_piecewise_writer = piecewise_writer(
                source_piecewise_transcript
            )
            source_piecewise_expression = piecewise_expression(
                source_piecewise_transcript
            )
        except StopIteration as exc:
            raise AssertionError(
                "nested-loop MemoryPhi piecewise affine value summary is "
                "missing"
            ) from exc
        if not required_capabilities.issubset(capabilities):
            raise AssertionError(
                "nested-loop MemoryPhi piecewise affine capability chain is "
                "incomplete"
            )
        if (
            source_piecewise_transcript["summary"]["value_semantics"].get(
                "kind"
            )
            != (
                "guard-specialized-piecewise-affine-bitvector-byte-"
                "two-dimensional-last-write"
            )
            or source_piecewise_writer["stored_value"].get("semantics")
            != "guard-specialized-modulo-2^bits"
            or source_piecewise_expression.get("selected_arm")
            not in {"true", "false"}
            or type(source_piecewise_expression.get("guard_result")) is not bool
        ):
            raise AssertionError(
                "nested-loop MemoryPhi piecewise affine value contract is "
                "incomplete"
            )

        def expect_piecewise_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError(
                    "nested-loop MemoryPhi piecewise affine value tamper was "
                    "accepted"
                )

        piecewise_mutations: list[dict] = []
        for capability in required_capabilities:
            missing_capability = copy.deepcopy(artifact)
            missing_capability["lowering"]["capabilities"].remove(capability)
            piecewise_mutations.append(missing_capability)

        def mutate_piecewise(mutate) -> None:
            candidate = copy.deepcopy(artifact)
            mutate(piecewise_transcript(candidate))
            piecewise_mutations.append(candidate)

        for mutate in (
            lambda transcript: transcript.__setitem__(
                "schema", "symcc-loop-memoryphi-byte-lane-induction-v9"
            ),
            lambda transcript: transcript["summary"]["value_semantics"].__setitem__(
                "kind", "affine-bitvector-byte-two-dimensional-last-write"
            ),
            lambda transcript: piecewise_writer(transcript)[
                "stored_value"
            ]["guard"].__setitem__("predicate", "slt"),
            lambda transcript: piecewise_writer(transcript)[
                "stored_value"
            ]["guard"].__setitem__("induction", "unknown"),
            lambda transcript: piecewise_writer(transcript)[
                "stored_value"
            ]["guard"].__setitem__(
                "constant",
                piecewise_writer(transcript)["stored_value"]["guard"]
                ["constant"] + 1,
            ),
            lambda transcript: piecewise_writer(transcript)[
                "stored_value"
            ]["guard"].__setitem__(
                "constant_on_left",
                not piecewise_writer(transcript)["stored_value"]["guard"]
                ["constant_on_left"],
            ),
            lambda transcript: piecewise_writer(transcript)[
                "stored_value"
            ]["when_true"].__setitem__(
                "outer_scale",
                piecewise_writer(transcript)["stored_value"]["when_true"]
                ["outer_scale"] + 1,
            ),
            lambda transcript: piecewise_writer(transcript)[
                "stored_value"
            ]["when_false"].__setitem__(
                "input_scale",
                piecewise_writer(transcript)["stored_value"]["when_false"]
                ["input_scale"] + 1,
            ),
            lambda transcript: piecewise_expression(transcript).__setitem__(
                "guard_result",
                not piecewise_expression(transcript)["guard_result"],
            ),
            lambda transcript: piecewise_expression(transcript).__setitem__(
                "selected_arm",
                "false"
                if piecewise_expression(transcript)["selected_arm"] == "true"
                else "true",
            ),
            lambda transcript: piecewise_expression(transcript)["value"].__setitem__(
                "constant",
                piecewise_expression(transcript)["value"]["constant"] + 1,
            ),
        ):
            mutate_piecewise(mutate)

        changed_select = copy.deepcopy(artifact)
        changed_transcript = piecewise_transcript(changed_select)
        changed_stored_value = piecewise_writer(changed_transcript)[
            "stored_value"
        ]
        writer_block = changed_transcript["loops"]["inner"]["body"]
        select_name = changed_stored_value["variable"]["var"]
        for function in changed_select["functions"].values():
            values = function["blocks"].get(writer_block)
            if not isinstance(values, list):
                continue
            selection = next(
                item for item in values if item.get("dst") == select_name
            )
            selection["true"], selection["false"] = (
                selection["false"], selection["true"]
            )
            break
        piecewise_mutations.append(changed_select)

        changed_guard = copy.deepcopy(artifact)
        changed_transcript = piecewise_transcript(changed_guard)
        changed_stored_value = piecewise_writer(changed_transcript)[
            "stored_value"
        ]
        writer_block = changed_transcript["loops"]["inner"]["body"]
        guard_name = changed_stored_value["guard"]["variable"]["var"]
        for function in changed_guard["functions"].values():
            values = function["blocks"].get(writer_block)
            if not isinstance(values, list):
                continue
            guard = next(item for item in values if item.get("dst") == guard_name)
            guard["operator"] = "slt"
            break
        piecewise_mutations.append(changed_guard)

        missing_transcript = copy.deepcopy(artifact)
        for function in missing_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get(
                        "initialization_loop_memoryphi", {}
                    ).get("schema") == (
                        "symcc-loop-memoryphi-byte-lane-induction-v10"
                    ):
                        instruction.pop("initialization_loop_memoryphi")
        piecewise_mutations.append(missing_transcript)
        for mutation in piecewise_mutations:
            expect_piecewise_tamper_rejected(mutation)
    if args.expect_nested_loop_memoryphi_decision_dag_value_summary:
        capabilities = set(lowering.get("capabilities", ()))
        decision_capability = (
            "bounded-nested-loop-memoryphi-decision-dag-value-summary"
        )
        required_capabilities = {
            "bounded-loop-memoryphi-byte-lane-induction",
            "bounded-nested-loop-memoryphi-summary-composition",
            "bounded-nested-loop-memoryphi-last-write-value-summary",
            "bounded-nested-loop-memoryphi-two-dimensional-affine-summary",
            "bounded-nested-loop-memoryphi-affine-symbolic-value-summary",
            "bounded-nested-loop-memoryphi-piecewise-affine-value-summary",
            decision_capability,
        }

        def decision_transcript(program: dict) -> dict:
            return next(
                instruction["initialization_loop_memoryphi"]
                for function in program["functions"].values()
                for instructions in function["blocks"].values()
                for instruction in instructions
                if instruction.get("initialization_loop_memoryphi", {}).get(
                    "schema"
                ) == "symcc-loop-memoryphi-byte-lane-induction-v11"
            )

        def decision_writer(transcript: dict) -> dict:
            return next(
                writer for writer in transcript["summary"]["writers"]
                if writer.get("stored_value", {}).get("kind")
                == "affine-decision-dag-bitvector"
            )

        def decision_expression(transcript: dict) -> dict:
            return next(
                case["value_byte_expression"]
                for witness in transcript["witnesses"]
                for case in witness["last_write_cases"]
                if case.get("value_byte_expression", {}).get("kind")
                == "guard-specialized-decision-dag-byte"
                and len(case["value_byte_expression"].get("path", ())) >= 2
            )

        try:
            source_decision_transcript = decision_transcript(artifact)
            source_decision_writer = decision_writer(source_decision_transcript)
            source_decision_expression = decision_expression(
                source_decision_transcript
            )
        except StopIteration as exc:
            raise AssertionError(
                "nested-loop MemoryPhi decision DAG value summary is missing"
            ) from exc
        stored_value = source_decision_writer["stored_value"]
        nodes = stored_value.get("nodes", [])
        if (
            not required_capabilities.issubset(capabilities)
            or source_decision_transcript["summary"]["value_semantics"].get(
                "kind"
            ) != (
                "guard-specialized-affine-decision-dag-bitvector-byte-"
                "two-dimensional-last-write"
            )
            or stored_value.get("semantics")
            != "postorder-shared-guard-specialized-modulo-2^bits"
            or stored_value.get("root") != len(nodes) - 1
            or stored_value.get("depth", 0) < 2
            or source_decision_expression.get("root") != stored_value.get("root")
            or len(source_decision_expression.get("path", ())) < 2
        ):
            raise AssertionError(
                "nested-loop MemoryPhi decision DAG value contract is incomplete"
            )

        def expect_decision_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError(
                    "nested-loop MemoryPhi decision DAG value tamper was accepted"
                )

        decision_mutations: list[dict] = []
        for capability in required_capabilities:
            missing_capability = copy.deepcopy(artifact)
            missing_capability["lowering"]["capabilities"].remove(capability)
            decision_mutations.append(missing_capability)

        def mutate_decision(mutate) -> None:
            candidate = copy.deepcopy(artifact)
            mutate(decision_transcript(candidate))
            decision_mutations.append(candidate)

        for mutate in (
            lambda transcript: decision_writer(transcript)["stored_value"].__setitem__(
                "root", 0
            ),
            lambda transcript: decision_writer(transcript)["stored_value"].__setitem__(
                "depth", 1
            ),
            lambda transcript: decision_writer(transcript)["stored_value"]["nodes"][
                0
            ].__setitem__("id", 1),
            lambda transcript: next(
                node
                for node in decision_writer(transcript)["stored_value"]["nodes"]
                if node.get("kind") == "guard"
            ).__setitem__("when_true", 31),
            lambda transcript: next(
                node
                for node in decision_writer(transcript)["stored_value"]["nodes"]
                if node.get("kind") == "guard"
            )["guard"].__setitem__("predicate", "slt"),
            lambda transcript: next(
                node
                for node in decision_writer(transcript)["stored_value"]["nodes"]
                if node.get("kind") == "affine-leaf"
            )["value"].__setitem__("outer_scale", 63),
            lambda transcript: decision_expression(transcript)["path"][0].__setitem__(
                "guard_result",
                not decision_expression(transcript)["path"][0]["guard_result"],
            ),
            lambda transcript: decision_expression(transcript).__setitem__("leaf", 31),
        ):
            mutate_decision(mutate)

        changed_select = copy.deepcopy(artifact)
        changed_transcript = decision_transcript(changed_select)
        changed_value = decision_writer(changed_transcript)["stored_value"]
        writer_block = changed_transcript["loops"]["inner"]["body"]
        root_select = changed_value["variable"]["var"]
        for function in changed_select["functions"].values():
            values = function["blocks"].get(writer_block)
            if not isinstance(values, list):
                continue
            selection = next(
                item for item in values if item.get("dst") == root_select
            )
            selection["true"], selection["false"] = (
                selection["false"], selection["true"]
            )
            break
        decision_mutations.append(changed_select)

        missing_transcript = copy.deepcopy(artifact)
        for function in missing_transcript["functions"].values():
            for instructions in function["blocks"].values():
                for instruction in instructions:
                    if instruction.get(
                        "initialization_loop_memoryphi", {}
                    ).get("schema") == (
                        "symcc-loop-memoryphi-byte-lane-induction-v11"
                    ):
                        instruction.pop("initialization_loop_memoryphi")
        decision_mutations.append(missing_transcript)
        for mutation_index, mutation in enumerate(decision_mutations):
            try:
                expect_decision_tamper_rejected(mutation)
            except AssertionError as exc:
                raise AssertionError(
                    "nested-loop MemoryPhi decision DAG mutation "
                    f"{mutation_index} was accepted"
                ) from exc
    if args.expect_executable_loop_summary_transfer:
        capabilities = set(lowering.get("capabilities", ()))
        executable_capability = (
            "refinement-verified-nested-loop-memory-summary-transfer"
        )

        def transfer_instructions(program: dict) -> list[dict]:
            return [
                instruction
                for function in program.get("functions", {}).values()
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "loop_summary_transfer"
            ]

        transfers = transfer_instructions(artifact)
        if (
            executable_capability not in capabilities
            or len(transfers) != 1
            or transfers[0].get("schema")
            != "symcc-loop-summary-transfer-v1"
            or transfers[0].get("mode") != "explicit-opt-in"
            or transfers[0].get("proof", {}).get("effect")
            != "closed-memory-only"
            or transfers[0].get("proof", {}).get("memory_kind") != "stack"
            or transfers[0].get("proof", {}).get("live_outs") != 0
        ):
            raise AssertionError(
                "executable nested-loop summary transfer contract is incomplete"
            )

        def expect_transfer_tamper_rejected(tampered: dict) -> None:
            with tempfile.TemporaryDirectory() as tamper_tmp:
                executor = LiveContinuationExecutor(
                    LiveStateStore(tamper_tmp, page_size=64)
                )
                try:
                    executor.create(tampered)
                except ValueError:
                    return
                raise AssertionError(
                    "executable nested-loop summary transfer tamper was accepted"
                )

        transfer_mutations: list[dict] = []
        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(
            executable_capability
        )
        transfer_mutations.append(missing_capability)

        for field, value in (
            ("source_load", "missing_load"),
            ("fallback", transfers[0]["target"]),
            ("target", transfers[0]["fallback"]),
        ):
            candidate = copy.deepcopy(artifact)
            transfer_instructions(candidate)[0][field] = value
            transfer_mutations.append(candidate)

        for field, value in (
            ("effect", "unknown"),
            ("memory_kind", "heap"),
            ("live_outs", 1),
            ("store_count", int(transfers[0]["proof"]["store_count"]) + 1),
            ("loop_blocks", list(reversed(transfers[0]["proof"]["loop_blocks"]))),
        ):
            candidate = copy.deepcopy(artifact)
            transfer_instructions(candidate)[0]["proof"][field] = value
            transfer_mutations.append(candidate)

        mismatched_transcript = copy.deepcopy(artifact)
        transfer_instructions(mismatched_transcript)[0]["transcript"][
            "base"
        ] += 1
        transfer_mutations.append(mismatched_transcript)

        bad_cache = copy.deepcopy(artifact)
        transfer_instructions(bad_cache)[0]["_compiled"] = {
            "schema": "invalid-cache"
        }
        transfer_mutations.append(bad_cache)

        for mutation_index, mutation in enumerate(transfer_mutations):
            try:
                expect_transfer_tamper_rejected(mutation)
            except AssertionError as exc:
                raise AssertionError(
                    "executable nested-loop summary transfer mutation "
                    f"{mutation_index} was accepted"
                ) from exc

        # Reach the real preheader checkpoint one instruction at a time, then
        # force a late preparation error.  Expression interning may grow, but
        # the executable state itself must remain byte-for-byte unchanged.
        with tempfile.TemporaryDirectory() as transaction_tmp:
            transaction_executor = LiveContinuationExecutor(
                LiveStateStore(transaction_tmp, page_size=64),
                enable_loop_summary_transfer=True,
            )
            transaction_checkpoint = transaction_executor.create(
                artifact,
                input_bytes=bytes.fromhex(args.input_hex),
            )
            summary_state = None
            summary_program = None
            summary_instruction = None
            for _ in range(128):
                candidate_state = transaction_executor._load_state(
                    transaction_checkpoint
                )
                candidate_program = transaction_executor.store.get_program(
                    candidate_state.program_root
                )
                candidate_instruction = transaction_executor._instruction(
                    candidate_program, candidate_state.frames[-1]
                )
                if candidate_instruction.get("op") == "loop_summary_transfer":
                    summary_state = candidate_state
                    summary_program = candidate_program
                    summary_instruction = candidate_instruction
                    break
                one_step = transaction_executor.resume(
                    transaction_checkpoint,
                    max_steps=1,
                    max_states=1,
                )
                if len(one_step["frontier"]) != 1:
                    raise AssertionError(
                        "executable loop summary preheader is not uniquely reachable"
                    )
                transaction_checkpoint = one_step["frontier"][0]
            if (
                summary_state is None
                or summary_program is None
                or summary_instruction is None
            ):
                raise AssertionError(
                    "executable loop summary preheader was not reached"
                )
            before_state = copy.deepcopy(summary_state)
            invalid_instruction = copy.deepcopy(summary_instruction)
            invalid_instruction["_compiled"]["writes"][-1]["address"] = (
                int(invalid_instruction["_compiled"]["memory"]["address"])
                + int(invalid_instruction["_compiled"]["memory"]["size"])
            )
            try:
                transaction_executor._prepare_loop_summary_transfer(
                    summary_program,
                    summary_state,
                    invalid_instruction,
                    depth=len(summary_state.frames) - 1,
                )
            except ValueError:
                pass
            else:
                raise AssertionError(
                    "invalid executable loop summary preparation succeeded"
                )
            if summary_state != before_state:
                raise AssertionError(
                    "failed executable loop summary preparation mutated state"
                )
    if args.expect_alias:
        memory_instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("op") in {"load", "store"}
        ]
        alias_contracts = []
        for instruction in memory_instructions:
            if instruction.get("aliases"):
                alias_contracts.append(
                    {
                        "addresses": instruction["aliases"],
                        "alias_index": instruction.get("alias_index"),
                        "alias_index_bits": instruction.get("alias_index_bits"),
                        "alias_index_min": instruction.get("alias_index_min"),
                        "alias_index_max": instruction.get("alias_index_max"),
                    }
                )
            for case in instruction.get("alias_cases", ()):
                if case.get("alias_index") is not None:
                    alias_contracts.append(
                        {
                            "addresses": case.get("addresses", ()),
                            "alias_index": case.get("alias_index"),
                            "alias_index_bits": case.get("alias_index_bits"),
                            "alias_index_min": case.get("alias_index_min"),
                            "alias_index_max": case.get("alias_index_max"),
                        }
                    )
        if (
            "bounded-symbolic-alias" not in lowering.get("capabilities", ())
            or not alias_contracts
            or any(
                len(contract["addresses"]) > 256
                or len(set(contract["addresses"])) != len(contract["addresses"])
                or not contract.get("alias_index")
                or not 1 <= int(contract.get("alias_index_bits", 0)) <= 64
                or int(contract.get("alias_index_min", 1))
                > int(contract.get("alias_index_max", 0))
                for contract in alias_contracts
            )
        ):
            raise AssertionError("symbolic-alias lowering contract is incomplete")
        if args.expect_alias_index_range:
            minimum, maximum = (
                int(item) for item in args.expect_alias_index_range.split(":", 1)
            )
            if not any(
                int(contract["alias_index_min"]) == minimum
                and int(contract["alias_index_max"]) == maximum
                for contract in alias_contracts
            ):
                raise AssertionError("symbolic-alias index range does not match")
    if args.expect_pointer_union:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        union_memory = [
            instruction
            for instruction in instructions
            if instruction.get("op") in {"load", "store"}
            and instruction.get("alias_cases")
        ]
        union_objectsize = [
            instruction
            for instruction in instructions
            if instruction.get("op") == "select"
            and str(instruction.get("dst", "")).startswith("objectsize_case_")
            and instruction.get("condition")
        ]
        if (
            "bounded-pointer-union" not in lowering.get("capabilities", ())
            or not (union_memory or union_objectsize)
            or any(
                len(instruction["alias_cases"]) < 1
                or any(
                    not case.get("addresses") or not case.get("guards")
                    for case in instruction["alias_cases"]
                )
                for instruction in union_memory
            )
        ):
            raise AssertionError("pointer-union lowering contract is incomplete")
    if args.expect_cross_function_pointer:
        functions = artifact.get("functions", {})
        pointer_functions = [
            function
            for function in functions.values()
            if (function.get("pointer_params") or function.get("pointer_return_bits"))
        ]
        pointer_calls = [
            instruction
            for function in functions.values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if (
                instruction.get("op") in {"call", "indirect_call"}
                and (
                    instruction.get("pointer_args")
                    or instruction.get("pointer_result_bits")
                )
            )
        ]
        if (
            "bounded-cross-function-pointer" not in lowering.get("capabilities", ())
            or "caller-domain-pointer-certificate"
            not in lowering.get("capabilities", ())
            or not pointer_functions
            or not pointer_calls
            or any(
                any(
                    int(item.get("index", -1)) < 0
                    or not 1 <= int(item.get("bits", 0)) <= 64
                    for item in function.get("pointer_params", ())
                )
                or {
                    int(item.get("index", -1))
                    for item in function.get("pointer_domains", ())
                }
                != {
                    int(item.get("index", -1))
                    for item in function.get("pointer_params", ())
                }
                or (
                    function.get("pointer_return_bits") is not None
                    and (
                        not 1 <= int(function.get("pointer_return_bits", 0)) <= 64
                        or not function.get("pointer_return_domain")
                    )
                )
                for function in pointer_functions
            )
            or any(
                {
                    int(item.get("index", -1))
                    for item in instruction.get("pointer_domains", ())
                }
                != {
                    int(item.get("index", -1))
                    for item in instruction.get("pointer_args", ())
                }
                for instruction in pointer_calls
            )
            or any(
                instruction.get("pointer_result_bits")
                and not instruction.get("pointer_domain_dst")
                for instruction in pointer_calls
            )
        ):
            raise AssertionError(
                "cross-function pointer lowering contract is incomplete"
            )
    if args.expect_indirect_call:
        functions = artifact.get("functions", {})
        indirect_calls = [
            instruction
            for function in functions.values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("op") == "indirect_call"
        ]
        if (
            "bounded-indirect-call-dispatch" not in lowering.get("capabilities", ())
            or not indirect_calls
            or any(
                not instruction.get("target")
                or not 1 <= int(instruction.get("target_bits", 0)) <= 64
                or len(instruction.get("targets", ())) < 1
                or any(
                    target.get("function") not in functions
                    or int(target.get("id", 0))
                    != int(functions[target["function"]].get("function_id", -1))
                    for target in instruction.get("targets", ())
                )
                for instruction in indirect_calls
            )
        ):
            raise AssertionError("indirect-call lowering contract is incomplete")
    if args.expect_external_summary:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if (
            "bounded-external-effect-summary" not in lowering.get("capabilities", ())
            or (
                0
                < sum(
                    instruction.get("op") == "load"
                    and int(instruction.get("bits", 0)) == 8
                    for instruction in instructions
                )
                < 2
            )
            or (
                sum(
                    instruction.get("op") == "load"
                    and int(instruction.get("bits", 0)) == 8
                    for instruction in instructions
                )
                >= 2
                and not any(
                    instruction.get("op") == "select" for instruction in instructions
                )
            )
        ):
            raise AssertionError("external effect summary is incomplete")
    if args.expect_declarative_pure_external:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        models = [
            instruction
            for instruction in instructions
            if instruction.get("op") == "external_pure"
        ]
        if (
            "declarative-pure-external-summary" not in lowering.get("capabilities", ())
            or not models
            or any(
                not instruction.get("function")
                or not str(instruction.get("model", "")).startswith("pure-v1:")
                or len(instruction.get("args", ()))
                != len(instruction.get("arg_bits", ()))
                or not 1 <= int(instruction.get("bits", 0)) <= 64
                or not 1 <= int(instruction.get("site", 0)) < (1 << 64)
                for instruction in models
            )
        ):
            raise AssertionError("declarative pure external contract is incomplete")
    if args.expect_region_summary:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if "bounded-external-effect-summary" not in lowering.get(
            "capabilities", ()
        ) or not any(
            instruction.get("op") == "store" and int(instruction.get("bits", 0)) == 8
            for instruction in instructions
        ):
            raise AssertionError("external region summary is incomplete")
    if args.expect_string_summary:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        guarded_loads = [
            instruction
            for instruction in instructions
            if (
                instruction.get("op") == "load" and instruction.get("guard") is not None
            )
        ]
        if (
            "bounded-external-effect-summary" not in lowering.get("capabilities", ())
            or "guarded-memory-access" not in lowering.get("capabilities", ())
            or "bounded-nul-string-summary" not in lowering.get("capabilities", ())
            or not guarded_loads
            or any(
                int(instruction.get("bits", 0)) != 8 for instruction in guarded_loads
            )
        ):
            raise AssertionError("NUL-aware string summary is incomplete")
    if args.expect_pointer_search:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if (
            "bounded-external-effect-summary" not in lowering.get("capabilities", ())
            or "bounded-pointer-search-summary" not in lowering.get("capabilities", ())
            or (
                any(instruction.get("op") == "load" for instruction in instructions)
                and not any(
                    instruction.get("op") == "select"
                    and int(instruction.get("bits", 0)) in {32, 64}
                    for instruction in instructions
                )
            )
        ):
            raise AssertionError("pointer search summary is incomplete")
    if args.expect_string_copy:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        byte_stores = [
            instruction
            for instruction in instructions
            if (
                instruction.get("op") == "store"
                and int(instruction.get("bits", 0)) == 8
            )
        ]
        guarded_stores = [
            instruction
            for instruction in byte_stores
            if instruction.get("guard") is not None
        ]
        capabilities = lowering.get("capabilities", ())
        if (
            "bounded-external-effect-summary" not in capabilities
            or "bounded-string-copy-summary" not in capabilities
            or (guarded_stores and "guarded-memory-write" not in capabilities)
        ):
            raise AssertionError("bounded string-copy summary is incomplete")
    if args.expect_ub_guards:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        assumptions = [
            instruction
            for instruction in instructions
            if instruction.get("op") == "assume"
        ]
        if (
            "llvm-defined-value-guards" not in lowering.get("capabilities", ())
            or not assumptions
            or any(
                not isinstance(instruction.get("condition"), dict)
                for instruction in assumptions
            )
        ):
            raise AssertionError("LLVM defined-value guard contract is incomplete")
    if args.expect_pointer_memory:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        pointer_width = 64
        pointer_memory = [
            instruction
            for instruction in instructions
            if (
                instruction.get("op") in {"load", "store"}
                and int(instruction.get("bits", 0)) == pointer_width
                and int(instruction.get("bytes", 0)) == pointer_width // 8
            )
        ]
        if (
            "bounded-pointer-memory" not in lowering.get("capabilities", ())
            or not pointer_memory
        ):
            raise AssertionError("bounded pointer-memory contract is incomplete")
    if args.expect_function_pointer_memory:
        capabilities = lowering.get("capabilities", ())
        if (
            "bounded-pointer-memory" not in capabilities
            or "bounded-function-pointer-memory" not in capabilities
            or "bounded-indirect-call-dispatch" not in capabilities
        ):
            raise AssertionError(
                "bounded function-pointer memory contract is incomplete"
            )
    if args.expect_pointer_table and "bounded-pointer-table" not in lowering.get(
        "capabilities", ()
    ):
        raise AssertionError("bounded pointer-table contract is incomplete")
    if (
        args.expect_pointer_memory_merge
        and "bounded-pointer-memory-merge" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("bounded pointer-memory merge contract is incomplete")
    if args.expect_nounwind_invoke:
        invoke_calls = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if (
                (
                    instruction.get("op") in {"call", "indirect_call"}
                    and instruction.get("normal_target")
                )
                or (
                    instruction.get("op") == "external_pure"
                    and instruction.get("normal")
                )
            )
        ]
        if (
            "bounded-nounwind-invoke" not in lowering.get("capabilities", ())
            or not invoke_calls
        ):
            raise AssertionError("bounded nounwind-invoke contract is incomplete")
    if args.expect_cleanup_exception:
        exception_ops = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op") in {"throw_if", "throw", "exception_throw"}
        ]
        unwind_calls = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op") in {"call", "indirect_call"}
            and instruction.get("unwind_target")
        ]
        if "bounded-cleanup-exception-unwind" not in lowering.get(
            "capabilities", ()
        ) or not (exception_ops or unwind_calls):
            raise AssertionError("bounded cleanup-exception contract is incomplete")
        if args.expect_exception_ops and not exception_ops:
            raise AssertionError("cleanup-exception operations are missing")
        if args.expect_unwind_call and not unwind_calls:
            raise AssertionError("cleanup-exception unwind call is missing")
    if args.expect_typed_exception:
        typed_throws = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op") in {"throw_if", "exception_throw"}
            and instruction.get("type_id")
        ]
        typed_matches = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op") == "exception_match"
            and (instruction.get("types") or instruction.get("catch_all"))
        ]
        selectors = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op") == "exception_type"
        ]
        if (
            "bounded-typed-exception-matching" not in lowering.get("capabilities", ())
            or not typed_throws
            or not typed_matches
            or not (
                selectors
                or any(
                    instruction.get("op") == "exception_throw"
                    for instruction in typed_throws
                )
            )
        ):
            raise AssertionError("bounded typed-exception contract is incomplete")
    if args.expect_exception_lifecycle:
        lifecycle_operations = {
            instruction.get("op")
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op")
            in {
                "exception_token",
                "exception_begin_catch",
                "exception_end_catch",
                "exception_rethrow",
            }
        }
        required = {
            "exception_token",
            "exception_begin_catch",
            "exception_end_catch",
        }
        if "bounded-exception-catch-lifecycle" not in lowering.get(
            "capabilities", ()
        ) or not required.issubset(lifecycle_operations):
            raise AssertionError("bounded exception lifecycle contract is incomplete")
    if args.expect_scalar_catch_object:
        scalar_values = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op") == "exception_value"
        ]
        if (
            "bounded-trivial-scalar-catch-object"
            not in lowering.get("capabilities", ())
            or not scalar_values
        ):
            raise AssertionError("bounded scalar catch-object contract is incomplete")
    if args.expect_exception_object_arena:
        arena_operations = {
            instruction.get("op")
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op")
            in {
                "exception_alloc",
                "exception_throw",
                "exception_object_load",
            }
        }
        arena_objects = [
            memory_object
            for memory_object in artifact.get("memory_objects", ())
            if memory_object.get("allocation") == "bounded-exception-arena"
        ]
        if (
            "bounded-exception-object-arena" not in lowering.get("capabilities", ())
            or arena_operations
            != {
                "exception_alloc",
                "exception_throw",
                "exception_object_load",
            }
            or not arena_objects
            or any(
                memory_object.get("lifetime") != "exception-lifecycle"
                for memory_object in arena_objects
            )
        ):
            raise AssertionError(
                "bounded exception-object arena contract is incomplete"
            )
    if args.expect_exception_object_fields:
        object_loads = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op") == "exception_object_load"
        ]
        offsets = [instruction.get("offset") for instruction in object_loads]
        if (
            "bounded-exception-object-fields" not in lowering.get("capabilities", ())
            or len(object_loads) < 2
            or not any(
                not isinstance(offset, bool) and isinstance(offset, int) and offset > 0
                for offset in offsets
            )
            or len(set(offsets)) < 2
        ):
            raise AssertionError(
                "bounded exception-object field contract is incomplete"
            )
    if args.expect_nondeterministic_freeze:
        choices = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
            if instruction.get("op") == "nondet"
        ]
        if (
            "stable-nondeterministic-freeze" not in lowering.get("capabilities", ())
            or not choices
            or any(
                not instruction.get("site")
                or not 1 <= int(instruction.get("bits", 0)) <= 64
                for instruction in choices
            )
        ):
            raise AssertionError("nondeterministic freeze contract is incomplete")
    if (
        args.expect_acyclic_pointer_memory_ssa
        and "acyclic-pointer-memory-ssa" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("acyclic pointer-memory SSA contract is incomplete")
    if args.expect_scalar_summary:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if "bounded-scalar-external-summary" not in lowering.get(
            "capabilities", ()
        ) or not any(
            instruction.get("op") in {"binary", "select", "unary"}
            for instruction in instructions
        ):
            raise AssertionError("bounded scalar-summary contract is incomplete")
    if args.expect_bitcount_intrinsic:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if "bounded-bitcount-intrinsic" not in lowering.get(
            "capabilities", ()
        ) or not any(
            instruction.get("op") == "binary"
            and instruction.get("operator") in {"lshr", "and", "add"}
            for instruction in instructions
        ):
            raise AssertionError("bounded bit-count intrinsic contract is incomplete")
    if args.expect_deferred_poison_freeze:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if (
            "bounded-deferred-poison-freeze" not in lowering.get("capabilities", ())
            or "stable-nondeterministic-freeze" not in lowering.get("capabilities", ())
            or not any(
                instruction.get("op") == "nondet" for instruction in instructions
            )
            or not any(
                instruction.get("op") == "select" for instruction in instructions
            )
        ):
            raise AssertionError(
                "bounded deferred-poison freeze contract is incomplete"
            )
    if (
        args.expect_cyclic_pointer_memory_ssa
        and "bounded-cyclic-pointer-memory-ssa" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("bounded cyclic pointer-memory SSA contract is incomplete")
    if (
        args.expect_canonical_pointer_cell
        and "canonical-pointer-cell-identity" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("canonical pointer-cell identity contract is incomplete")
    if args.expect_bit_permutation_intrinsic:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if "bounded-bit-permutation-intrinsic" not in lowering.get(
            "capabilities", ()
        ) or not any(
            instruction.get("op") == "binary"
            and instruction.get("operator") in {"shl", "lshr", "or"}
            for instruction in instructions
        ):
            raise AssertionError(
                "bounded bit-permutation intrinsic contract is incomplete"
            )
    if args.expect_saturating_arithmetic_intrinsic:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if "bounded-saturating-arithmetic-intrinsic" not in lowering.get(
            "capabilities", ()
        ) or not any(instruction.get("op") == "select" for instruction in instructions):
            raise AssertionError("bounded saturating-arithmetic contract is incomplete")
    if args.expect_scalar_selection_intrinsic:
        instructions = [
            instruction
            for function in artifact.get("functions", {}).values()
            for block in function.get("blocks", {}).values()
            for instruction in block
        ]
        if "bounded-scalar-selection-intrinsic" not in lowering.get(
            "capabilities", ()
        ) or not any(instruction.get("op") == "select" for instruction in instructions):
            raise AssertionError("bounded scalar-selection contract is incomplete")
    if (
        args.expect_optimization_hint_intrinsic
        and "llvm-optimization-hint-identity" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("LLVM optimization-hint identity contract is incomplete")
    if (
        args.expect_objectsize_intrinsic
        and "bounded-objectsize-intrinsic" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("bounded objectsize intrinsic contract is incomplete")
    if (
        args.expect_dynamic_objectsize_intrinsic
        and "bounded-dynamic-objectsize-intrinsic"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "bounded dynamic objectsize intrinsic contract is incomplete"
        )
    if (
        args.expect_overflow_arithmetic_intrinsic
        and "bounded-overflow-arithmetic-intrinsic"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "bounded overflow-arithmetic intrinsic contract is incomplete"
        )
    if (
        args.expect_ssa_copy_intrinsic
        and "bounded-ssa-copy-intrinsic" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("bounded ssa.copy intrinsic contract is incomplete")
    if (
        args.expect_transitive_deferred_poison
        and "bounded-transitive-deferred-poison" not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "bounded transitive deferred-poison contract is incomplete"
        )
    if (
        args.expect_select_deferred_poison
        and "bounded-select-deferred-poison" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("bounded select deferred-poison contract is incomplete")
    if (
        args.expect_phi_deferred_poison
        and "bounded-phi-deferred-poison" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("bounded PHI deferred-poison contract is incomplete")
    if (
        args.expect_memory_deferred_poison
        and "bounded-memory-deferred-poison" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("bounded memory deferred-poison contract is incomplete")
    if (
        args.expect_canonical_memory_deferred_poison
        and "canonical-address-memory-deferred-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "canonical-address memory deferred-poison contract is incomplete"
        )
    if (
        args.expect_cross_block_memory_deferred_poison
        and "bounded-cross-block-memory-deferred-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "cross-block memory deferred-poison contract is incomplete"
        )
    if (
        args.expect_cross_function_deferred_poison
        and "bounded-cross-function-deferred-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("cross-function deferred-poison contract is incomplete")
    if (
        args.expect_cross_function_argument_poison
        and "bounded-cross-function-argument-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("cross-function argument poison contract is incomplete")
    if (
        args.expect_multicallsite_deferred_poison
        and "bounded-multicallsite-deferred-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("multi-callsite deferred-poison contract is incomplete")
    if (
        args.expect_symbolic_pointer_memory
        and "bounded-symbolic-pointer-memory" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("symbolic pointer-memory contract is incomplete")
    if (
        args.expect_pointer_initial_definition_merge
        and "pointer-initial-definition-merge" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("pointer initial-definition merge contract is incomplete")
    if (
        args.expect_transitive_call_deferred_poison
        and "bounded-transitive-call-deferred-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("transitive call deferred-poison contract is incomplete")
    if (
        args.expect_multiconsumer_deferred_poison
        and "bounded-multiconsumer-deferred-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("multi-consumer deferred-poison contract is incomplete")
    if (
        args.expect_multiaccess_memory_deferred_poison
        and "bounded-multiaccess-memory-deferred-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "multi-access memory deferred-poison contract is incomplete"
        )
    if (
        args.expect_branch_memory_deferred_poison
        and "bounded-branch-memory-deferred-poison"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("branch memory deferred-poison contract is incomplete")
    if (
        args.expect_memory_definedness_phi
        and "bounded-memory-definedness-phi" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("memory definedness PHI contract is incomplete")
    if (
        args.expect_byte_lane_memory_definedness
        and "bounded-byte-lane-memory-definedness"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("byte-lane memory definedness contract is incomplete")
    if args.expect_byte_lane_memory_definedness:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("byte_lane_memory_definedness", [])
        ]
        if (
            not contracts
            or any(
                int(contract.get("bytes", 0)) != len(contract.get("lanes", []))
                for contract in contracts
            )
            or any(
                len(
                    {
                        (
                            str(lane.get("source", "")),
                            str(lane.get("store", "")),
                        )
                        for lane in contract.get("lanes", [])
                    }
                )
                < 2
                for contract in contracts
            )
            or not any(
                lane.get("defined")
                for contract in contracts
                for lane in contract.get("lanes", [])
            )
        ):
            raise AssertionError("byte-lane memory definedness metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("byte_lane_memory_definedness", []):
                lanes = contract.get("lanes", [])
                if lanes:
                    lanes.pop()
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "byte-lane memory definedness tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "byte-lane memory definedness" not in str(exc):
                    raise AssertionError(
                        "byte-lane memory definedness tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("byte-lane memory definedness tamper was accepted")
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove("bounded-byte-lane-memory-definedness")
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "byte-lane memory definedness" not in str(exc):
                    raise AssertionError(
                        "byte-lane capability tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("byte-lane capability tamper was accepted")
    if args.expect_byte_lane_writer_graph:
        writer_graph_capability = "bounded-byte-lane-writer-graph"
        if writer_graph_capability not in lowering.get("capabilities", ()):
            raise AssertionError("byte-lane writer graph capability is missing")
        graph_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("byte_lane_memory_definedness", [])
            if contract.get("writer_graph") is True
        ]
        graph_stores = []
        for function in artifact.get("functions", {}).values():
            graph_store_ids = {
                str(lane.get("store", ""))
                for contract in function.get("byte_lane_memory_definedness", [])
                if contract.get("writer_graph") is True
                for lane in contract.get("lanes", [])
                if lane.get("source") == "store"
            }
            graph_stores.extend(
                instruction
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "store"
                and str(instruction.get("byte_lane_store", "")) in graph_store_ids
            )
        if (
            not graph_contracts
            or not graph_stores
            or any(
                store.get("byte_lane_defined")
                and not store.get("byte_lane_poison_source")
                for store in graph_stores
            )
        ):
            raise AssertionError("byte-lane writer graph contract is incomplete")

        def expect_writer_graph_rejection(
            candidate: dict[str, object],
            fragment: str,
        ) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(candidate)
                except ValueError as exc:
                    if fragment not in str(exc):
                        raise AssertionError(
                            "byte-lane writer graph tamper failed for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError("byte-lane writer graph tamper was accepted")

        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(writer_graph_capability)
        expect_writer_graph_rejection(missing_capability, "byte-lane writer graph")

        missing_contract = copy.deepcopy(artifact)
        for function in missing_contract.get("functions", {}).values():
            contracts = function.get("byte_lane_memory_definedness", [])
            if contracts:
                contracts[0].pop("writer_graph", None)
                break
        expect_writer_graph_rejection(missing_contract, "writer graph contract")

        shadowed = copy.deepcopy(artifact)
        changed = False
        for function in shadowed.get("functions", {}).values():
            stores = {
                str(instruction.get("byte_lane_store", "")): instruction
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "store"
                and instruction.get("byte_lane_store")
            }
            for contract in function.get("byte_lane_memory_definedness", []):
                block = function.get("blocks", {}).get(
                    str(contract.get("block", "")), []
                )
                load = next(
                    (
                        instruction
                        for instruction in block
                        if instruction.get("op") == "load"
                        and str(instruction.get("dst", ""))
                        == str(contract.get("load", ""))
                    ),
                    None,
                )
                if not isinstance(load, dict):
                    continue
                load_address = int(load["address"]["const"])
                for lane in contract.get("lanes", []):
                    address = load_address + int(lane.get("lane", -1))
                    for store_id, store in stores.items():
                        store_address = int(store["address"]["const"])
                        store_bytes = int(store.get("bytes", 0))
                        if (
                            store_id != str(lane.get("store", ""))
                            and store_address <= address < store_address + store_bytes
                        ):
                            lane["source"] = "store"
                            lane["store"] = store_id
                            lane["store_byte"] = address - store_address
                            lane["store_bytes"] = store_bytes
                            lane["defined"] = store.get("byte_lane_defined", "")
                            changed = True
                            break
                    if changed:
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "byte-lane writer graph shadowing tamper target is missing"
            )
        expect_writer_graph_rejection(shadowed, "last-writer edge")

        poison_drift = copy.deepcopy(artifact)
        changed = False
        for function in poison_drift.get("functions", {}).values():
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if instruction.get("byte_lane_poison_source"):
                        instruction["byte_lane_poison_source"] += "_drift"
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "byte-lane writer graph poison tamper target is missing"
            )
        expect_writer_graph_rejection(poison_drift, "poison transfer")
    if (
        args.expect_byte_lane_memory_definedness_phi
        and "bounded-byte-lane-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("byte-lane memory definedness PHI contract is incomplete")
    if args.expect_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("byte_lane_memory_definedness_phis", [])
        ]
        if (
            not contracts
            or any(len(contract.get("incoming", [])) < 2 for contract in contracts)
            or any(
                any(
                    len(endpoint.get("lanes", [])) != int(contract.get("bytes", 0))
                    for endpoint in contract.get("incoming", [])
                )
                for contract in contracts
            )
            or not any(
                lane.get("defined")
                for contract in contracts
                for endpoint in contract.get("incoming", [])
                for lane in endpoint.get("lanes", [])
            )
        ):
            raise AssertionError("byte-lane memory definedness PHI metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("byte_lane_memory_definedness_phis", []):
                incoming = contract.get("incoming", [])
                if incoming and incoming[0].get("lanes"):
                    incoming[0]["lanes"].pop()
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError("byte-lane PHI tamper target is missing")
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "byte-lane memory definedness PHI" not in str(exc):
                    raise AssertionError(
                        "byte-lane PHI tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("byte-lane PHI tamper was accepted")
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove("bounded-byte-lane-memory-definedness-phi")
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "byte-lane memory definedness PHI" not in str(exc):
                    raise AssertionError(
                        "byte-lane PHI capability tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("byte-lane PHI capability tamper was accepted")
    if args.expect_byte_lane_phi_writer_graph:
        graph_capability = "bounded-byte-lane-phi-writer-graph"
        if graph_capability not in lowering.get("capabilities", ()):
            raise AssertionError("byte-lane PHI writer graph capability is missing")
        graph_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("byte_lane_memory_definedness_phis", [])
            if contract.get("writer_graph") is True
        ]
        graph_stores = []
        for function in artifact.get("functions", {}).values():
            store_ids = {
                str(lane.get("store", ""))
                for contract in function.get("byte_lane_memory_definedness_phis", [])
                if contract.get("writer_graph") is True
                for endpoint in contract.get("incoming", [])
                for lane in endpoint.get("lanes", [])
                if lane.get("source") == "store"
            }
            graph_stores.extend(
                instruction
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "store"
                and str(instruction.get("byte_lane_store", "")) in store_ids
            )
        if (
            not graph_contracts
            or not graph_stores
            or any(
                store.get("byte_lane_defined")
                and not store.get("byte_lane_poison_source")
                for store in graph_stores
            )
        ):
            raise AssertionError("byte-lane PHI writer graph contract is incomplete")

        def expect_phi_graph_rejection(
            candidate: dict[str, object], fragment: str
        ) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(candidate)
                except ValueError as exc:
                    if fragment not in str(exc):
                        raise AssertionError(
                            "byte-lane PHI writer graph tamper failed "
                            "for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "byte-lane PHI writer graph tamper was accepted"
                    )

        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(graph_capability)
        expect_phi_graph_rejection(missing_capability, "writer graph")

        missing_marker = copy.deepcopy(artifact)
        changed = False
        for function in missing_marker.get("functions", {}).values():
            contracts = function.get("byte_lane_memory_definedness_phis", [])
            if contracts:
                contracts[0].pop("writer_graph", None)
                changed = True
                break
        if not changed:
            raise AssertionError("byte-lane PHI writer graph marker target is missing")
        expect_phi_graph_rejection(missing_marker, "writer graph contract")

        substituted = copy.deepcopy(artifact)
        changed = False
        for function in substituted.get("functions", {}).values():
            stores = {
                str(instruction.get("byte_lane_store", "")): instruction
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "store"
                and instruction.get("byte_lane_store")
            }
            for contract in function.get("byte_lane_memory_definedness_phis", []):
                block = function.get("blocks", {}).get(
                    str(contract.get("block", "")), []
                )
                load = next(
                    (
                        instruction
                        for instruction in block
                        if instruction.get("op") == "load"
                        and str(instruction.get("dst", ""))
                        == str(contract.get("load", ""))
                    ),
                    None,
                )
                if not isinstance(load, dict):
                    continue
                load_address = int(load["address"]["const"])
                for endpoint in contract.get("incoming", []):
                    for lane in endpoint.get("lanes", []):
                        if lane.get("source") != "store":
                            continue
                        address = load_address + int(lane.get("lane", -1))
                        for store_id, store in stores.items():
                            store_address = int(store["address"]["const"])
                            store_bytes = int(store.get("bytes", 0))
                            if (
                                store_id != str(lane.get("store", ""))
                                and store_address
                                <= address
                                < store_address + store_bytes
                            ):
                                lane.update(
                                    {
                                        "source": "store",
                                        "store": store_id,
                                        "store_byte": address - store_address,
                                        "store_bytes": store_bytes,
                                        "defined": store.get("byte_lane_defined", ""),
                                    }
                                )
                                changed = True
                                break
                        if changed:
                            break
                    if changed:
                        break
                if changed:
                    break
            if changed:
                break
        substitution_fragment = "last-writer edge"
        if not changed:
            for function in substituted.get("functions", {}).values():
                for contract in function.get("byte_lane_memory_definedness_phis", []):
                    for endpoint in contract.get("incoming", []):
                        for lane in endpoint.get("lanes", []):
                            if lane.get("source") == "store":
                                store_id = str(lane.get("store", ""))
                                for instructions in function.get("blocks", {}).values():
                                    for instruction in instructions:
                                        if (
                                            str(instruction.get("byte_lane_store", ""))
                                            != store_id
                                        ):
                                            continue
                                        instruction["address"]["const"] += 8
                                        changed = True
                                        substitution_fragment = "writer graph"
                                        break
                                    if changed:
                                        break
                                break
                        if changed:
                            break
                    if changed:
                        break
                if changed:
                    break
        if not changed:
            raise AssertionError("byte-lane PHI writer graph tamper target is missing")
        expect_phi_graph_rejection(substituted, substitution_fragment)

        poison_drift = copy.deepcopy(artifact)
        changed = False
        for function in poison_drift.get("functions", {}).values():
            graph_ids = {
                str(lane.get("store", ""))
                for contract in function.get("byte_lane_memory_definedness_phis", [])
                if contract.get("writer_graph") is True
                for endpoint in contract.get("incoming", [])
                for lane in endpoint.get("lanes", [])
                if lane.get("source") == "store"
            }
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if str(
                        instruction.get("byte_lane_store", "")
                    ) in graph_ids and instruction.get("byte_lane_poison_source"):
                        instruction["byte_lane_poison_source"] += "_drift"
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError("byte-lane PHI writer graph poison target is missing")
        expect_phi_graph_rejection(poison_drift, "poison transfer")
    cyclic_byte_lane_capability = "bounded-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_cyclic_byte_lane_memory_definedness_phi
        and cyclic_byte_lane_capability not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "cyclic byte-lane memory definedness PHI contract is incomplete"
        )
    if args.expect_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        if (
            not contracts
            or any(
                len(contract.get("incoming", [])) < 2
                or len(contract.get("lane_defined", []))
                != int(contract.get("bytes", 0))
                for contract in contracts
            )
            or not any(
                lane.get("source") == "carry"
                for contract in contracts
                for endpoint in contract.get("incoming", [])
                for lane in endpoint.get("lanes", [])
            )
            or not any(
                lane.get("defined")
                for contract in contracts
                for endpoint in contract.get("incoming", [])
                for lane in endpoint.get("lanes", [])
            )
        ):
            raise AssertionError(
                "cyclic byte-lane memory definedness PHI metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                for endpoint in contract.get("incoming", []):
                    for lane in endpoint.get("lanes", []):
                        if lane.get("source") == "carry":
                            lane["source"] = "initial"
                            changed = True
                            break
                    if changed:
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError("cyclic byte-lane PHI tamper target is missing")
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "cyclic byte-lane memory definedness PHI" not in str(exc):
                    raise AssertionError(
                        "cyclic byte-lane PHI tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("cyclic byte-lane PHI tamper was accepted")
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(cyclic_byte_lane_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "cyclic byte-lane memory definedness PHI" not in str(exc):
                    raise AssertionError(
                        "cyclic byte-lane PHI capability "
                        "tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "cyclic byte-lane PHI capability tamper was accepted"
                )
    if args.expect_cyclic_byte_lane_writer_graph:
        graph_capability = "bounded-cyclic-byte-lane-writer-graph"
        if graph_capability not in lowering.get("capabilities", ()):
            raise AssertionError("cyclic byte-lane writer graph capability is missing")
        graph_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("cyclic_byte_lane_memory_definedness_phis", [])
            if contract.get("writer_graph") is True
        ]
        graph_stores = []
        for function in artifact.get("functions", {}).values():
            store_ids = {
                str(lane.get("store", ""))
                for contract in function.get(
                    "cyclic_byte_lane_memory_definedness_phis", []
                )
                if contract.get("writer_graph") is True
                for endpoint in contract.get("incoming", [])
                for lane in endpoint.get("lanes", [])
                if lane.get("source") == "store"
            }
            graph_stores.extend(
                instruction
                for instructions in function.get("blocks", {}).values()
                for instruction in instructions
                if instruction.get("op") == "store"
                and str(instruction.get("byte_lane_store", "")) in store_ids
            )
        if (
            not graph_contracts
            or not graph_stores
            or any(
                store.get("byte_lane_defined")
                and not store.get("byte_lane_poison_source")
                for store in graph_stores
            )
        ):
            raise AssertionError("cyclic byte-lane writer graph contract is incomplete")

        def expect_cyclic_graph_rejection(
            candidate: dict[str, object], fragment: str
        ) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(candidate)
                except ValueError as exc:
                    if fragment not in str(exc):
                        raise AssertionError(
                            "cyclic byte-lane writer graph tamper failed "
                            "for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "cyclic byte-lane writer graph tamper was accepted"
                    )

        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(graph_capability)
        expect_cyclic_graph_rejection(missing_capability, "writer graph capability")

        missing_marker = copy.deepcopy(artifact)
        changed = False
        for function in missing_marker.get("functions", {}).values():
            contracts = function.get("cyclic_byte_lane_memory_definedness_phis", [])
            if contracts:
                contracts[0].pop("writer_graph", None)
                changed = True
                break
        if not changed:
            raise AssertionError(
                "cyclic byte-lane writer graph marker target is missing"
            )
        expect_cyclic_graph_rejection(missing_marker, "writer graph contract")

        address_drift = copy.deepcopy(artifact)
        changed = False
        for function in address_drift.get("functions", {}).values():
            graph_ids = {
                str(lane.get("store", ""))
                for contract in function.get(
                    "cyclic_byte_lane_memory_definedness_phis", []
                )
                if contract.get("writer_graph") is True
                for endpoint in contract.get("incoming", [])
                for lane in endpoint.get("lanes", [])
                if lane.get("source") == "store"
            }
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if str(instruction.get("byte_lane_store", "")) in graph_ids:
                        instruction["address"]["const"] += 8
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "cyclic byte-lane writer graph address target is missing"
            )
        expect_cyclic_graph_rejection(address_drift, "writer graph")

        poison_drift = copy.deepcopy(artifact)
        changed = False
        for function in poison_drift.get("functions", {}).values():
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if instruction.get("byte_lane_poison_source"):
                        instruction["byte_lane_poison_source"] += "_drift"
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "cyclic byte-lane writer graph poison target is missing"
            )
        expect_cyclic_graph_rejection(poison_drift, "poison transfer")

    if args.expect_conditional_cyclic_byte_lane_writer_graph:
        graph_capability = "bounded-conditional-cyclic-byte-lane-writer-graph"
        if graph_capability not in lowering.get("capabilities", ()):
            raise AssertionError(
                "conditional cyclic byte-lane writer graph capability is missing"
            )
        graph_keys = (
            "conditional_cyclic_byte_lane_memory_definedness_phis",
            "forwarded_conditional_cyclic_byte_lane_memory_definedness_phis",
        )
        graph_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for key in graph_keys
            for contract in function.get(key, [])
            if contract.get("writer_graph") is True
        ]
        graph_store_ids = {
            str(lane.get("store", ""))
            for contract in graph_contracts
            for transfer in contract.get("conditional_transfers", [])
            for lane in transfer.get("lanes", [])
            if lane.get("source") == "store"
        }
        graph_stores = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("op") == "store"
            and str(instruction.get("byte_lane_store", "")) in graph_store_ids
        ]
        if (
            not graph_contracts
            or not graph_store_ids
            or not graph_stores
            or any(
                store.get("byte_lane_defined")
                and not store.get("byte_lane_poison_source")
                for store in graph_stores
            )
        ):
            raise AssertionError(
                "conditional cyclic byte-lane writer graph contract is incomplete"
            )

        def expect_conditional_graph_rejection(
            candidate: dict[str, object], fragment: str
        ) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(candidate)
                except ValueError as exc:
                    if fragment not in str(exc):
                        raise AssertionError(
                            "conditional cyclic byte-lane writer graph "
                            "tamper failed for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "conditional cyclic byte-lane writer graph tamper was accepted"
                    )

        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(graph_capability)
        expect_conditional_graph_rejection(missing_capability, "writer graph")

        missing_marker = copy.deepcopy(artifact)
        changed = False
        for function in missing_marker.get("functions", {}).values():
            for key in graph_keys:
                contracts = function.get(key, [])
                if contracts:
                    contracts[0].pop("writer_graph", None)
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "conditional cyclic byte-lane writer graph marker target is missing"
            )
        expect_conditional_graph_rejection(missing_marker, "writer graph contract")

        address_drift = copy.deepcopy(artifact)
        changed = False
        for function in address_drift.get("functions", {}).values():
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if str(instruction.get("byte_lane_store", "")) in graph_store_ids:
                        instruction["address"]["const"] += 8
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "conditional cyclic byte-lane writer graph address target is missing"
            )
        expect_conditional_graph_rejection(address_drift, "writer graph")

        poison_drift = copy.deepcopy(artifact)
        changed = False
        for function in poison_drift.get("functions", {}).values():
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if str(
                        instruction.get("byte_lane_store", "")
                    ) in graph_store_ids and instruction.get("byte_lane_poison_source"):
                        instruction["byte_lane_poison_source"] += "_drift"
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "conditional cyclic byte-lane writer graph poison target is missing"
            )
        expect_conditional_graph_rejection(poison_drift, "poison transfer")

    conditional_cyclic_byte_lane_capability = (
        "bounded-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    if (
        args.expect_conditional_cyclic_byte_lane_memory_definedness_phi
        and conditional_cyclic_byte_lane_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "conditional cyclic byte-lane memory definedness PHI contract is incomplete"
        )
    if args.expect_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("conditional_transfers", [])
        ]
        if (
            not contracts
            or any(
                len(contract.get("incoming", [])) < 2
                or len(contract.get("lane_defined", []))
                != int(contract.get("bytes", 0))
                for contract in contracts
            )
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("store_arm")
                and transfer.get("carry_arm")
                and transfer.get("store_edge")
                and transfer.get("carry_edge")
                for transfer in transfers
            )
            or not any(
                lane.get("source") == "store" and lane.get("defined")
                for transfer in transfers
                for lane in transfer.get("lanes", [])
            )
            or not any(
                lane.get("source") == "carry"
                for transfer in transfers
                for lane in transfer.get("lanes", [])
            )
        ):
            raise AssertionError(
                "conditional cyclic byte-lane memory "
                "definedness PHI metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("conditional_transfers", [])
                if transfers:
                    transfer = transfers[0]
                    transfer["store_when_true"] = not bool(
                        transfer.get("store_when_true", False)
                    )
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "conditional cyclic byte-lane PHI tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "conditional cyclic byte-lane memory definedness PHI" not in str(
                    exc
                ):
                    raise AssertionError(
                        "conditional cyclic byte-lane "
                        "PHI tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "conditional cyclic byte-lane PHI tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        if "bounded-conditional-cyclic-byte-lane-writer-graph" in capabilities:
            capabilities.remove("bounded-conditional-cyclic-byte-lane-writer-graph")
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    contract.pop("writer_graph", None)
                for instructions in function.get("blocks", {}).values():
                    for instruction in instructions:
                        instruction.pop("byte_lane_poison_source", None)
        capabilities.remove(conditional_cyclic_byte_lane_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "conditional cyclic byte-lane memory definedness PHI" not in str(
                    exc
                ):
                    raise AssertionError(
                        "conditional cyclic byte-lane "
                        "PHI capability tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "conditional cyclic byte-lane PHI capability tamper was accepted"
                )
    forwarded_conditional_cyclic_capability = (
        "bounded-forwarded-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    if (
        args.expect_forwarded_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_conditional_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded conditional cyclic byte-lane "
            "memory definedness PHI contract is incomplete"
        )
    if args.expect_forwarded_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("conditional_transfers", [])
        ]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and (
                    transfer.get("store_successor") != transfer.get("store_arm")
                    or transfer.get("carry_successor") != transfer.get("carry_arm")
                )
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded conditional cyclic byte-lane "
                "memory definedness PHI metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "forwarded_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("conditional_transfers", [])
                if transfers:
                    transfers[0]["store_successor"] = transfers[0].get("store_arm")
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "forwarded conditional cyclic byte-lane PHI tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "conditional cyclic byte-lane memory definedness PHI" not in str(
                    exc
                ):
                    raise AssertionError(
                        "forwarded conditional cyclic "
                        "byte-lane PHI tamper failed for "
                        "the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded conditional cyclic byte-lane PHI tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        if "bounded-conditional-cyclic-byte-lane-writer-graph" in capabilities:
            capabilities.remove("bounded-conditional-cyclic-byte-lane-writer-graph")
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    contract.pop("writer_graph", None)
                for instructions in function.get("blocks", {}).values():
                    for instruction in instructions:
                        instruction.pop("byte_lane_poison_source", None)
        capabilities.remove(forwarded_conditional_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "conditional cyclic byte-lane memory definedness PHI" not in str(
                    exc
                ):
                    raise AssertionError(
                        "forwarded conditional cyclic "
                        "byte-lane PHI capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded conditional cyclic "
                    "byte-lane PHI capability tamper "
                    "was accepted"
                )
    if args.expect_multiarm_cyclic_byte_lane_writer_graph:
        graph_capability = "bounded-multiarm-cyclic-byte-lane-writer-graph"
        if graph_capability not in lowering.get("capabilities", ()):
            raise AssertionError(
                "multi-arm cyclic byte-lane writer graph capability is missing"
            )
        graph_keys = (
            "multiarm_conditional_cyclic_byte_lane_memory_definedness_phis",
            "forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis",
        )
        graph_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for key in graph_keys
            for contract in function.get(key, [])
            if contract.get("writer_graph") is True
        ]
        graph_store_ids = {
            str(lane.get("store", ""))
            for contract in graph_contracts
            for transfer in contract.get("multiarm_transfers", [])
            for arm in transfer.get("arms", [])
            for lane in arm.get("lanes", [])
            if lane.get("source") == "store"
        }
        graph_stores = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("op") == "store"
            and str(instruction.get("byte_lane_store", "")) in graph_store_ids
        ]
        if (
            not graph_contracts
            or len(graph_store_ids) != 2
            or len(graph_stores) != 2
            or any(
                store.get("byte_lane_defined")
                and not store.get("byte_lane_poison_source")
                for store in graph_stores
            )
        ):
            raise AssertionError(
                "multi-arm cyclic byte-lane writer graph contract is incomplete"
            )

        def expect_multiarm_graph_rejection(
            candidate: dict[str, object], fragment: str
        ) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(candidate)
                except ValueError as exc:
                    if fragment not in str(exc):
                        raise AssertionError(
                            "multi-arm cyclic byte-lane writer graph tamper "
                            "failed for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "multi-arm cyclic byte-lane writer graph tamper was accepted"
                    )

        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(graph_capability)
        expect_multiarm_graph_rejection(missing_capability, "writer graph")

        missing_marker = copy.deepcopy(artifact)
        changed = False
        for function in missing_marker.get("functions", {}).values():
            for key in graph_keys:
                contracts = function.get(key, [])
                if contracts:
                    contracts[0].pop("writer_graph", None)
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "multi-arm cyclic byte-lane writer graph marker target is missing"
            )
        expect_multiarm_graph_rejection(missing_marker, "writer graph contract")

        address_drift = copy.deepcopy(artifact)
        changed = False
        for function in address_drift.get("functions", {}).values():
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if str(instruction.get("byte_lane_store", "")) in graph_store_ids:
                        instruction["address"]["const"] += 8
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "multi-arm cyclic byte-lane writer graph address target is missing"
            )
        expect_multiarm_graph_rejection(address_drift, "writer graph")

        poison_drift = copy.deepcopy(artifact)
        changed = False
        for function in poison_drift.get("functions", {}).values():
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if str(
                        instruction.get("byte_lane_store", "")
                    ) in graph_store_ids and instruction.get("byte_lane_poison_source"):
                        instruction["byte_lane_poison_source"] += "_drift"
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "multi-arm cyclic byte-lane writer graph poison target is missing"
            )
        expect_multiarm_graph_rejection(poison_drift, "poison transfer")

    if args.expect_recursive_cyclic_byte_lane_writer_graph:
        graph_capability = "bounded-recursive-cyclic-byte-lane-writer-graph"
        if graph_capability not in lowering.get("capabilities", ()):
            raise AssertionError(
                "recursive cyclic byte-lane writer graph capability is missing"
            )
        graph_keys = (
            "recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
            "forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
        )
        graph_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for key in graph_keys
            for contract in function.get(key, [])
            if contract.get("writer_graph") is True
        ]
        graph_transfers = [
            transfer
            for contract in graph_contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        graph_store_ids = {
            str(lane.get("store", ""))
            for transfer in graph_transfers
            for leaf in transfer.get("leaves", [])
            for lane in leaf.get("lanes", [])
            if lane.get("source") == "store"
        }
        graph_stores = [
            instruction
            for function in artifact.get("functions", {}).values()
            for instructions in function.get("blocks", {}).values()
            for instruction in instructions
            if instruction.get("op") == "store"
            and str(instruction.get("byte_lane_store", "")) in graph_store_ids
        ]
        if (
            not graph_contracts
            or len(graph_transfers) != len(graph_contracts)
            or any(
                len(transfer.get("branches", [])) != 3
                or len(transfer.get("leaves", [])) != 4
                for transfer in graph_transfers
            )
            or len(graph_store_ids) != 3
            or len(graph_stores) != 3
            or any(
                store.get("byte_lane_defined")
                and not store.get("byte_lane_poison_source")
                for store in graph_stores
            )
        ):
            raise AssertionError(
                "recursive cyclic byte-lane writer graph contract is incomplete"
            )

        def expect_recursive_graph_rejection(
            candidate: dict[str, object], fragment: str
        ) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(candidate)
                except ValueError as exc:
                    if fragment not in str(exc):
                        raise AssertionError(
                            "recursive cyclic byte-lane writer graph tamper "
                            "failed for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "recursive cyclic byte-lane writer graph tamper was accepted"
                    )

        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(graph_capability)
        expect_recursive_graph_rejection(missing_capability, "writer graph")

        missing_marker = copy.deepcopy(artifact)
        changed = False
        for function in missing_marker.get("functions", {}).values():
            for key in graph_keys:
                contracts = function.get(key, [])
                if contracts:
                    contracts[0].pop("writer_graph", None)
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "recursive cyclic byte-lane writer graph marker target is missing"
            )
        expect_recursive_graph_rejection(missing_marker, "writer graph contract")

        address_drift = copy.deepcopy(artifact)
        changed = False
        for function in address_drift.get("functions", {}).values():
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if str(instruction.get("byte_lane_store", "")) in graph_store_ids:
                        instruction["address"]["const"] += 8
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "recursive cyclic byte-lane writer graph address target is missing"
            )
        expect_recursive_graph_rejection(address_drift, "writer graph")

        poison_drift = copy.deepcopy(artifact)
        changed = False
        for function in poison_drift.get("functions", {}).values():
            for instructions in function.get("blocks", {}).values():
                for instruction in instructions:
                    if str(
                        instruction.get("byte_lane_store", "")
                    ) in graph_store_ids and instruction.get("byte_lane_poison_source"):
                        instruction["byte_lane_poison_source"] += "_drift"
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "recursive cyclic byte-lane writer graph poison target is missing"
            )
        expect_recursive_graph_rejection(poison_drift, "poison transfer")

    multiarm_conditional_cyclic_capability = (
        "bounded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    if (
        args.expect_multiarm_conditional_cyclic_byte_lane_memory_definedness_phi
        and multiarm_conditional_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "multi-arm conditional cyclic byte-lane "
            "memory definedness PHI contract is incomplete"
        )
    if args.expect_multiarm_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "multiarm_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("multiarm_transfers", [])
        ]
        arms = [arm for transfer in transfers for arm in transfer.get("arms", [])]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or any(
                len(transfer.get("arms", [])) != 3
                or {str(arm.get("route", "")) for arm in transfer.get("arms", [])}
                != {
                    "root",
                    "inner_true",
                    "inner_false",
                }
                for transfer in transfers
            )
            or sum(
                any(lane.get("source") == "store" for lane in arm.get("lanes", []))
                for arm in arms
            )
            != 2 * len(contracts)
            or sum(
                all(lane.get("source") == "carry" for lane in arm.get("lanes", []))
                for arm in arms
            )
            != len(contracts)
        ):
            raise AssertionError(
                "multi-arm conditional cyclic byte-lane "
                "memory definedness PHI metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "multiarm_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("multiarm_transfers", [])
                if transfers and len(transfers[0].get("arms", [])) == 3:
                    transfers[0]["arms"][1]["route"] = "inner_false"
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "multi-arm conditional cyclic byte-lane PHI tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "multi-arm cyclic byte-lane memory definedness PHI" not in str(exc):
                    raise AssertionError(
                        "multi-arm conditional cyclic "
                        "byte-lane PHI tamper failed for "
                        "the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-arm conditional cyclic byte-lane PHI tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        if "bounded-multiarm-cyclic-byte-lane-writer-graph" in capabilities:
            capabilities.remove("bounded-multiarm-cyclic-byte-lane-writer-graph")
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "multiarm_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    contract.pop("writer_graph", None)
                for instructions in function.get("blocks", {}).values():
                    for instruction in instructions:
                        instruction.pop("byte_lane_poison_source", None)
        capabilities.remove(multiarm_conditional_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "multi-arm conditional cyclic "
                    "byte-lane memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "multi-arm conditional cyclic "
                        "byte-lane PHI capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-arm conditional cyclic "
                    "byte-lane PHI capability tamper "
                    "was accepted"
                )
    forwarded_multiarm_cyclic_capability = (
        "bounded-forwarded-multiarm-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    if (
        args.expect_forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_multiarm_cyclic_capability not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded multi-arm conditional cyclic byte-lane contract is incomplete"
        )
    if args.expect_forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("multiarm_transfers", [])
        ]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and any(
                    arm.get("successor") != arm.get("block")
                    for arm in transfer.get("arms", [])
                )
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded multi-arm conditional cyclic byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                for transfer in contract.get("multiarm_transfers", []):
                    for arm in transfer.get("arms", []):
                        if arm.get("successor") != arm.get("block"):
                            arm["successor"] = arm.get("block")
                            changed = True
                            break
                    if changed:
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "forwarded multi-arm cyclic byte-lane tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "multi-arm cyclic byte-lane memory definedness PHI" not in str(exc):
                    raise AssertionError(
                        "forwarded multi-arm cyclic "
                        "byte-lane tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded multi-arm cyclic byte-lane tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        if "bounded-multiarm-cyclic-byte-lane-writer-graph" in capabilities:
            capabilities.remove("bounded-multiarm-cyclic-byte-lane-writer-graph")
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_multiarm_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    contract.pop("writer_graph", None)
                for instructions in function.get("blocks", {}).values():
                    for instruction in instructions:
                        instruction.pop("byte_lane_poison_source", None)
        capabilities.remove(forwarded_multiarm_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded multi-arm conditional "
                    "cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded multi-arm cyclic "
                        "byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded multi-arm cyclic byte-lane "
                    "capability tamper was accepted"
                )
    recursive_cyclic_capability = (
        "bounded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    if (
        args.expect_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and recursive_cyclic_capability not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "recursive conditional cyclic byte-lane contract is incomplete"
        )
    if args.expect_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or any(
                len(transfer.get("leaves", [])) < 4
                or len(transfer.get("leaves", [])) > 8
                or len(transfer.get("branches", []))
                != len(transfer.get("leaves", [])) - 1
                or int(transfer.get("depth", 0)) < 2
                or int(transfer.get("depth", 0)) > 6
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "recursive conditional cyclic byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("recursive_transfers", [])
                if transfers:
                    transfers[0]["depth"] = int(transfers[0].get("depth", 0)) + 1
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError("recursive cyclic byte-lane tamper target is missing")
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "recursive cyclic byte-lane tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("recursive cyclic byte-lane tamper was accepted")
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        if "bounded-recursive-cyclic-byte-lane-writer-graph" in capabilities:
            capabilities.remove("bounded-recursive-cyclic-byte-lane-writer-graph")
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    contract.pop("writer_graph", None)
                for instructions in function.get("blocks", {}).values():
                    for instruction in instructions:
                        instruction.pop("byte_lane_poison_source", None)
        capabilities.remove(recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "recursive conditional cyclic "
                    "byte-lane memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "recursive cyclic byte-lane "
                        "capability tamper failed for "
                        "the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "recursive cyclic byte-lane capability tamper was accepted"
                )
    forwarded_recursive_cyclic_capability = "bounded-forwarded-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and any(
                    leaf.get("successor") != leaf.get("block")
                    for leaf in transfer.get("leaves", [])
                )
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded recursive cyclic byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                for transfer in contract.get("recursive_transfers", []):
                    for leaf in transfer.get("leaves", []):
                        if leaf.get("successor") != leaf.get("block"):
                            leaf["successor"] = leaf.get("block")
                            changed = True
                            break
                    if changed:
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "forwarded recursive cyclic byte-lane tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "forwarded recursive cyclic "
                        "byte-lane tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded recursive cyclic byte-lane tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        if "bounded-recursive-cyclic-byte-lane-writer-graph" in capabilities:
            capabilities.remove("bounded-recursive-cyclic-byte-lane-writer-graph")
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    contract.pop("writer_graph", None)
                for instructions in function.get("blocks", {}).values():
                    for instruction in instructions:
                        instruction.pop("byte_lane_poison_source", None)
        capabilities.remove(forwarded_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded recursive conditional "
                    "cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded recursive cyclic "
                        "byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded recursive cyclic byte-lane "
                    "capability tamper was accepted"
                )
    grouped_recursive_cyclic_capability = (
        "bounded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    if (
        args.expect_grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and grouped_recursive_cyclic_capability not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "grouped recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        repeated_sources = []
        for transfer in transfers:
            source_counts: dict[str, set[str]] = {}
            for leaf in transfer.get("leaves", []):
                for lane in leaf.get("lanes", []):
                    if lane.get("source") != "store":
                        continue
                    source_counts.setdefault(
                        str(lane.get("store", "")),
                        set(),
                    ).add(str(leaf.get("block", "")))
            repeated_sources.extend(
                source for source, leaves in source_counts.items() if len(leaves) > 1
            )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("grouped") is True
                and transfer.get("forwarded") is not True
                for transfer in transfers
            )
            or len(repeated_sources) != len(contracts)
        ):
            raise AssertionError(
                "grouped recursive cyclic byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("recursive_transfers", [])
                if transfers:
                    transfers[0]["grouped"] = False
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "grouped recursive cyclic byte-lane tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "grouped recursive cyclic "
                        "byte-lane tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "grouped recursive cyclic byte-lane tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(grouped_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "grouped recursive conditional "
                    "cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "grouped recursive cyclic "
                        "byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "grouped recursive cyclic byte-lane capability tamper was accepted"
                )
    forwarded_grouped_recursive_cyclic_capability = "bounded-forwarded-grouped-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_grouped_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded grouped recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_forwarded_grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and transfer.get("grouped") is True
                and transfer.get("repeated_source") is not True
                and transfer.get("composed_repeated_source") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded grouped recursive cyclic byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "forwarded_grouped_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("recursive_transfers", [])
                if transfers:
                    transfers[0]["forwarded"] = False
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "forwarded grouped recursive cyclic byte-lane tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "forwarded grouped recursive cyclic "
                        "byte-lane tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded grouped recursive cyclic byte-lane tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(forwarded_grouped_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded grouped recursive "
                    "conditional cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded grouped recursive cyclic "
                        "byte-lane capability tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded grouped recursive cyclic "
                    "byte-lane capability tamper was accepted"
                )
    repeated_source_recursive_cyclic_capability = "bounded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    forwarded_repeated_source_recursive_cyclic_capability = "bounded-forwarded-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_repeated_source_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded repeated-source recursive cyclic "
            "byte-lane contract is incomplete"
        )
    if args.expect_forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and transfer.get("repeated_source") is True
                and transfer.get("grouped") is not True
                and transfer.get("composed_repeated_source") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded repeated-source recursive cyclic "
                "byte-lane metadata is missing"
            )
        for marker in ("forwarded", "repeated_source"):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = False
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "forwarded repeated-source recursive "
                    "cyclic byte-lane tamper target is missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "forwarded repeated-source "
                            "recursive cyclic byte-lane "
                            "tamper failed for the wrong "
                            "reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "forwarded repeated-source recursive "
                        "cyclic byte-lane tamper was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(forwarded_repeated_source_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded repeated-source recursive "
                    "conditional cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded repeated-source recursive "
                        "cyclic byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded repeated-source recursive "
                    "cyclic byte-lane capability tamper "
                    "was accepted"
                )
    if (
        args.expect_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and repeated_source_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "repeated-source recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("repeated_source") is True
                and transfer.get("grouped") is not True
                and transfer.get("forwarded") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "repeated-source recursive cyclic byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("recursive_transfers", [])
                if transfers:
                    transfers[0]["repeated_source"] = False
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "repeated-source recursive cyclic byte-lane tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "repeated-source recursive cyclic "
                        "byte-lane tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "repeated-source recursive cyclic byte-lane tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(repeated_source_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "repeated-source recursive "
                    "conditional cyclic byte-lane "
                    "memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "repeated-source recursive cyclic "
                        "byte-lane capability tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "repeated-source recursive cyclic "
                    "byte-lane capability tamper was accepted"
                )
    composed_repeated_source_recursive_cyclic_capability = "bounded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    forwarded_composed_repeated_source_recursive_cyclic_capability = "bounded-forwarded-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_composed_repeated_source_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded composed repeated-source recursive "
            "cyclic byte-lane contract is incomplete"
        )
    if args.expect_forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        composed_leaf_count = sum(
            1
            for transfer in transfers
            for leaf in transfer.get("leaves", [])
            if len(
                {
                    str(lane.get("store", ""))
                    for lane in leaf.get("lanes", [])
                    if lane.get("source") == "store"
                }
            )
            == 2
        )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and transfer.get("composed_repeated_source") is True
                and transfer.get("repeated_source") is not True
                and transfer.get("multicarry") is not True
                for transfer in transfers
            )
            or composed_leaf_count < len(contracts)
        ):
            raise AssertionError(
                "forwarded composed repeated-source "
                "recursive cyclic byte-lane metadata is "
                "missing"
            )
        for marker in (
            "forwarded",
            "composed_repeated_source",
        ):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = False
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "forwarded composed repeated-source "
                    "recursive cyclic byte-lane tamper "
                    "target is missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "forwarded composed "
                            "repeated-source recursive "
                            "cyclic byte-lane tamper failed "
                            "for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "forwarded composed repeated-source "
                        "recursive cyclic byte-lane tamper "
                        "was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(
            forwarded_composed_repeated_source_recursive_cyclic_capability
        )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded composed repeated-source "
                    "recursive conditional cyclic "
                    "byte-lane memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded composed repeated-source "
                        "recursive cyclic byte-lane "
                        "capability tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded composed repeated-source "
                    "recursive cyclic byte-lane capability "
                    "tamper was accepted"
                )
    if (
        args.expect_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and composed_repeated_source_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "composed repeated-source recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        composed_leaf_count = 0
        for transfer in transfers:
            for leaf in transfer.get("leaves", []):
                stores = {
                    str(lane.get("store", ""))
                    for lane in leaf.get("lanes", [])
                    if lane.get("source") == "store"
                }
                if len(stores) == 2:
                    composed_leaf_count += 1
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("composed_repeated_source") is True
                and transfer.get("repeated_source") is not True
                and transfer.get("grouped") is not True
                and transfer.get("forwarded") is not True
                for transfer in transfers
            )
            or composed_leaf_count < len(contracts)
        ):
            raise AssertionError(
                "composed repeated-source recursive cyclic "
                "byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("recursive_transfers", [])
                if transfers:
                    transfers[0]["composed_repeated_source"] = False
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "composed repeated-source recursive cyclic "
                "byte-lane tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "composed repeated-source recursive "
                        "cyclic byte-lane tamper failed for "
                        "the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "composed repeated-source recursive "
                    "cyclic byte-lane tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(composed_repeated_source_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "composed repeated-source recursive "
                    "conditional cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "composed repeated-source recursive "
                        "cyclic byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "composed repeated-source recursive "
                    "cyclic byte-lane capability tamper "
                    "was accepted"
                )
    multicarry_composed_recursive_cyclic_capability = "bounded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    forwarded_multicarry_composed_recursive_cyclic_capability = "bounded-forwarded-multicarry-composed-repeated-source-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_multicarry_composed_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded multi-carry composed recursive "
            "cyclic byte-lane contract is incomplete"
        )
    if args.expect_forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        valid_counts = True
        for transfer in transfers:
            leaves = transfer.get("leaves", [])
            declared = int(transfer.get("carry_leaves", 0))
            actual = sum(
                1
                for leaf in leaves
                if leaf.get("lanes")
                and all(lane.get("source") == "carry" for lane in leaf.get("lanes", []))
            )
            valid_counts &= 2 <= declared <= 8 and actual == declared
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not valid_counts
            or not all(
                transfer.get("forwarded") is True
                and transfer.get("multicarry") is True
                and transfer.get("composed_repeated_source") is True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded multi-carry composed recursive "
                "cyclic byte-lane metadata is missing"
            )
        for marker in ("forwarded", "multicarry"):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = False
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "forwarded multi-carry composed "
                    "recursive cyclic byte-lane tamper "
                    "target is missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "forwarded multi-carry composed "
                            "recursive cyclic byte-lane "
                            "tamper failed for the wrong "
                            "reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "forwarded multi-carry composed "
                        "recursive cyclic byte-lane tamper "
                        "was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(forwarded_multicarry_composed_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded multi-carry composed "
                    "repeated-source recursive conditional "
                    "cyclic byte-lane memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded multi-carry composed "
                        "recursive cyclic byte-lane "
                        "capability tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded multi-carry composed "
                    "recursive cyclic byte-lane capability "
                    "tamper was accepted"
                )
    if (
        args.expect_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and multicarry_composed_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "multi-carry composed repeated-source recursive "
            "cyclic byte-lane contract is incomplete"
        )
    if args.expect_multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        valid_counts = True
        composed_leaf_count = 0
        for transfer in transfers:
            leaves = transfer.get("leaves", [])
            actual_carry_leaves = sum(
                1
                for leaf in leaves
                if leaf.get("lanes")
                and all(lane.get("source") == "carry" for lane in leaf.get("lanes", []))
            )
            try:
                declared_carry_leaves = int(transfer.get("carry_leaves", 0))
            except (TypeError, ValueError, OverflowError):
                valid_counts = False
                continue
            valid_counts &= (
                2 <= declared_carry_leaves <= 8
                and actual_carry_leaves == declared_carry_leaves
            )
            composed_leaf_count += sum(
                1
                for leaf in leaves
                if len(
                    {
                        str(lane.get("store", ""))
                        for lane in leaf.get("lanes", [])
                        if lane.get("source") == "store"
                    }
                )
                == 2
            )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not valid_counts
            or composed_leaf_count < len(contracts)
            or not all(
                transfer.get("multicarry") is True
                and transfer.get("composed_repeated_source") is True
                and transfer.get("repeated_source") is not True
                and transfer.get("grouped") is not True
                and transfer.get("forwarded") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "multi-carry composed repeated-source "
                "recursive cyclic byte-lane metadata is "
                "missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "multicarry_composed_repeated_source_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("recursive_transfers", [])
                if transfers:
                    transfers[0]["carry_leaves"] = (
                        int(transfers[0].get("carry_leaves", 0)) + 1
                    )
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "multi-carry composed repeated-source "
                "recursive cyclic byte-lane tamper target "
                "is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "multi-carry composed recursive "
                        "cyclic byte-lane count tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-carry composed recursive cyclic "
                    "byte-lane count tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(multicarry_composed_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "multi-carry composed repeated-source "
                    "recursive conditional cyclic byte-lane "
                    "memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "multi-carry composed recursive "
                        "cyclic byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-carry composed recursive cyclic "
                    "byte-lane capability tamper was accepted"
                )
    multigroup_recursive_cyclic_capability = "bounded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    trigroup_recursive_cyclic_capability = (
        "bounded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    )
    forwarded_trigroup_recursive_cyclic_capability = "bounded-forwarded-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_trigroup_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded three-group recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_forwarded_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        if (
            not contracts
            or len(transfers) != len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and transfer.get("triple_groups") is True
                and int(transfer.get("group_count", 0)) == 3
                and transfer.get("mixed_groups") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded three-group recursive cyclic byte-lane metadata is missing"
            )
        for marker in ("forwarded", "triple_groups"):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = False
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "forwarded three-group recursive cyclic "
                    "byte-lane tamper target is missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "forwarded three-group recursive "
                            "cyclic byte-lane tamper failed "
                            "for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "forwarded three-group recursive "
                        "cyclic byte-lane tamper was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(forwarded_trigroup_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded three-group recursive "
                    "conditional cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded three-group recursive "
                        "cyclic byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded three-group recursive cyclic "
                    "byte-lane capability tamper was accepted"
                )
    if (
        args.expect_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and trigroup_recursive_cyclic_capability not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "three-group recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        repeated_source_count = 0
        for transfer in transfers:
            source_leaves: dict[str, set[str]] = {}
            for leaf in transfer.get("leaves", []):
                for lane in leaf.get("lanes", []):
                    if lane.get("source") != "store":
                        continue
                    source_leaves.setdefault(
                        str(lane.get("store", "")),
                        set(),
                    ).add(str(leaf.get("block", "")))
            repeated_source_count += sum(
                1 for leaves in source_leaves.values() if len(leaves) >= 2
            )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or repeated_source_count != 3 * len(contracts)
            or not all(
                transfer.get("multiple_groups") is True
                and transfer.get("triple_groups") is True
                and int(transfer.get("group_count", 0)) == 3
                and transfer.get("forwarded") is not True
                and transfer.get("mixed_groups") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "three-group recursive cyclic byte-lane metadata is missing"
            )
        for marker in ("triple_groups", "group_count"):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = (
                            False if marker == "triple_groups" else 2
                        )
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "three-group recursive cyclic byte-lane tamper target is missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "three-group recursive cyclic "
                            "byte-lane tamper failed for the "
                            "wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "three-group recursive cyclic byte-lane tamper was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(trigroup_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "three-group recursive conditional "
                    "cyclic byte-lane memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "three-group recursive cyclic "
                        "byte-lane capability tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "three-group recursive cyclic byte-lane "
                    "capability tamper was accepted"
                )

    def check_composed_trigroup(*, forwarded: bool) -> None:
        expected = (
            args.expect_forwarded_composed_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
            if forwarded
            else args.expect_composed_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        )
        if not expected:
            return
        prefix = "forwarded_" if forwarded else ""
        capability = (
            "bounded-"
            + ("forwarded-" if forwarded else "")
            + "composed-trigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
        )
        artifact_key = (
            prefix
            + "composed_trigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis"
        )
        if capability not in lowering.get("capabilities", ()):
            raise AssertionError(
                "composed three-group recursive cyclic byte-lane contract is incomplete"
            )
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(artifact_key, [])
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        composed_leaf_count = sum(
            1
            for transfer in transfers
            for leaf in transfer.get("leaves", [])
            if len(
                {
                    str(lane.get("store", ""))
                    for lane in leaf.get("lanes", [])
                    if lane.get("source") == "store"
                }
            )
            == 2
        )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or composed_leaf_count != len(contracts)
            or not all(
                transfer.get("multiple_groups") is True
                and transfer.get("triple_groups") is True
                and transfer.get("mixed_groups") is True
                and transfer.get("composed_triple_groups") is True
                and transfer.get("double_composed_groups") is not True
                and int(transfer.get("group_count", 0)) == 3
                and int(transfer.get("composed_group_count", 0)) == 1
                and (
                    transfer.get("forwarded") is True
                    if forwarded
                    else transfer.get("forwarded") is not True
                )
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "composed three-group recursive cyclic byte-lane metadata is missing"
            )
        markers = [
            ("composed_triple_groups", False),
            ("composed_group_count", 2),
        ]
        if forwarded:
            markers.append(("forwarded", False))
        for marker, replacement in markers:
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(artifact_key, []):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = replacement
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "composed three-group recursive cyclic "
                    "byte-lane tamper target is missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "composed three-group recursive "
                            "cyclic byte-lane tamper failed "
                            "for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "composed three-group recursive "
                        "cyclic byte-lane tamper was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                expected_error = (
                    "forwarded composed three-group"
                    if forwarded
                    else "composed three-group"
                )
                if expected_error not in str(exc):
                    raise AssertionError(
                        "composed three-group recursive "
                        "cyclic byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "composed three-group recursive cyclic "
                    "byte-lane capability tamper was accepted"
                )

    check_composed_trigroup(forwarded=False)
    check_composed_trigroup(forwarded=True)
    forwarded_multigroup_recursive_cyclic_capability = "bounded-forwarded-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_multigroup_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded multi-group recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_forwarded_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        repeated_source_count = 0
        for transfer in transfers:
            source_leaves: dict[str, set[str]] = {}
            for leaf in transfer.get("leaves", []):
                for lane in leaf.get("lanes", []):
                    if lane.get("source") != "store":
                        continue
                    source_leaves.setdefault(
                        str(lane.get("store", "")),
                        set(),
                    ).add(str(leaf.get("block", "")))
            repeated_source_count += sum(
                1 for leaves in source_leaves.values() if len(leaves) >= 2
            )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or repeated_source_count != 2 * len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and transfer.get("multiple_groups") is True
                and int(transfer.get("group_count", 0)) == 2
                and transfer.get("mixed_groups") is not True
                and transfer.get("composed_repeated_source") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded multi-group recursive cyclic byte-lane metadata is missing"
            )
        for marker in ("forwarded", "multiple_groups"):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = False
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "forwarded multi-group recursive cyclic "
                    "byte-lane tamper target is missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "forwarded multi-group recursive "
                            "cyclic byte-lane tamper failed "
                            "for the wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "forwarded multi-group recursive "
                        "cyclic byte-lane tamper was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(forwarded_multigroup_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded multi-group recursive "
                    "conditional cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded multi-group recursive "
                        "cyclic byte-lane capability tamper "
                        "failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded multi-group recursive cyclic "
                    "byte-lane capability tamper was accepted"
                )
    if (
        args.expect_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and multigroup_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "multi-group recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        repeated_source_count = 0
        for transfer in transfers:
            source_leaves: dict[str, set[str]] = {}
            for leaf in transfer.get("leaves", []):
                for lane in leaf.get("lanes", []):
                    if lane.get("source") != "store":
                        continue
                    source_leaves.setdefault(
                        str(lane.get("store", "")),
                        set(),
                    ).add(str(leaf.get("block", "")))
            repeated_source_count += sum(
                1 for leaves in source_leaves.values() if len(leaves) >= 2
            )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or repeated_source_count != 2 * len(contracts)
            or not all(
                transfer.get("multiple_groups") is True
                and int(transfer.get("group_count", 0)) == 2
                and transfer.get("multicarry") is not True
                and transfer.get("composed_repeated_source") is not True
                and transfer.get("repeated_source") is not True
                and transfer.get("grouped") is not True
                and transfer.get("forwarded") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "multi-group recursive cyclic byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("recursive_transfers", [])
                if transfers:
                    transfers[0]["group_count"] = 1
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "multi-group recursive cyclic byte-lane tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "multi-group recursive cyclic "
                        "byte-lane count tamper failed for "
                        "the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-group recursive cyclic byte-lane count tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(multigroup_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "multi-group recursive conditional "
                    "cyclic byte-lane memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "multi-group recursive cyclic "
                        "byte-lane capability tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-group recursive cyclic byte-lane "
                    "capability tamper was accepted"
                )
    mixed_multigroup_recursive_cyclic_capability = "bounded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    double_composed_multigroup_recursive_cyclic_capability = "bounded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    forwarded_double_composed_multigroup_recursive_cyclic_capability = "bounded-forwarded-double-composed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_double_composed_multigroup_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded double-composed multi-group "
            "recursive cyclic byte-lane contract is "
            "incomplete"
        )
    if args.expect_forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        composed_leaf_count = sum(
            1
            for transfer in transfers
            for leaf in transfer.get("leaves", [])
            if len(
                {
                    str(lane.get("store", ""))
                    for lane in leaf.get("lanes", [])
                    if lane.get("source") == "store"
                }
            )
            == 2
        )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or composed_leaf_count != 2 * len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and transfer.get("double_composed_groups") is True
                and int(transfer.get("composed_group_count", 0)) == 2
                and int(transfer.get("group_count", 0)) == 2
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded double-composed multi-group "
                "recursive cyclic byte-lane metadata is "
                "missing"
            )
        for marker in (
            "forwarded",
            "double_composed_groups",
        ):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = False
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "forwarded double-composed multi-group "
                    "recursive cyclic byte-lane tamper "
                    "target is missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "forwarded double-composed "
                            "multi-group recursive cyclic "
                            "byte-lane tamper failed for the "
                            "wrong reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "forwarded double-composed "
                        "multi-group recursive cyclic "
                        "byte-lane tamper was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(
            forwarded_double_composed_multigroup_recursive_cyclic_capability
        )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded double-composed multi-group "
                    "recursive conditional cyclic byte-lane "
                    "memory definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded double-composed "
                        "multi-group recursive cyclic "
                        "byte-lane capability tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded double-composed multi-group "
                    "recursive cyclic byte-lane capability "
                    "tamper was accepted"
                )
    if (
        args.expect_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and double_composed_multigroup_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "double-composed multi-group recursive cyclic "
            "byte-lane contract is incomplete"
        )
    if args.expect_double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        composed_leaf_count = sum(
            1
            for transfer in transfers
            for leaf in transfer.get("leaves", [])
            if len(
                {
                    str(lane.get("store", ""))
                    for lane in leaf.get("lanes", [])
                    if lane.get("source") == "store"
                }
            )
            == 2
        )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or composed_leaf_count != 2 * len(contracts)
            or not all(
                transfer.get("multiple_groups") is True
                and int(transfer.get("group_count", 0)) == 2
                and transfer.get("mixed_groups") is True
                and transfer.get("double_composed_groups") is True
                and int(transfer.get("composed_group_count", 0)) == 2
                and transfer.get("forwarded") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "double-composed multi-group recursive "
                "cyclic byte-lane metadata is missing"
            )
        for marker in (
            "double_composed_groups",
            "composed_group_count",
        ):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "double_composed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = (
                            False if marker == "double_composed_groups" else 1
                        )
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "double-composed multi-group recursive "
                    "cyclic byte-lane tamper target is "
                    "missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "double-composed multi-group "
                            "recursive cyclic byte-lane "
                            "tamper failed for the wrong "
                            "reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "double-composed multi-group "
                        "recursive cyclic byte-lane tamper "
                        "was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(double_composed_multigroup_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "double-composed multi-group recursive "
                    "conditional cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "double-composed multi-group "
                        "recursive cyclic byte-lane "
                        "capability tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "double-composed multi-group recursive "
                    "cyclic byte-lane capability tamper "
                    "was accepted"
                )
    forwarded_mixed_multigroup_recursive_cyclic_capability = "bounded-forwarded-mixed-multigroup-recursive-conditional-cyclic-byte-lane-memory-definedness-phi"
    if (
        args.expect_forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and forwarded_mixed_multigroup_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded mixed multi-group recursive cyclic "
            "byte-lane contract is incomplete"
        )
    if args.expect_forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        composed_leaf_count = sum(
            1
            for transfer in transfers
            for leaf in transfer.get("leaves", [])
            if len(
                {
                    str(lane.get("store", ""))
                    for lane in leaf.get("lanes", [])
                    if lane.get("source") == "store"
                }
            )
            == 2
        )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or composed_leaf_count != len(contracts)
            or not all(
                transfer.get("forwarded") is True
                and transfer.get("multiple_groups") is True
                and int(transfer.get("group_count", 0)) == 2
                and transfer.get("mixed_groups") is True
                and int(transfer.get("composed_group_count", 0)) == 1
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "forwarded mixed multi-group recursive "
                "cyclic byte-lane metadata is missing"
            )
        for marker in ("forwarded", "mixed_groups"):
            tampered = copy.deepcopy(artifact)
            changed = False
            for function in tampered.get("functions", {}).values():
                for contract in function.get(
                    "forwarded_mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                    [],
                ):
                    marker_transfers = contract.get("recursive_transfers", [])
                    if marker_transfers:
                        marker_transfers[0][marker] = False
                        changed = True
                        break
                if changed:
                    break
            if not changed:
                raise AssertionError(
                    "forwarded mixed multi-group recursive "
                    "cyclic byte-lane tamper target is "
                    "missing"
                )
            with tempfile.TemporaryDirectory() as tmp:
                executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
                try:
                    executor.create(tampered)
                except ValueError as exc:
                    if "recursive cyclic byte-lane" not in str(exc):
                        raise AssertionError(
                            "forwarded mixed multi-group "
                            "recursive cyclic byte-lane "
                            "tamper failed for the wrong "
                            "reason"
                        ) from exc
                else:
                    raise AssertionError(
                        "forwarded mixed multi-group "
                        "recursive cyclic byte-lane tamper "
                        "was accepted"
                    )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(forwarded_mixed_multigroup_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "forwarded mixed multi-group recursive "
                    "conditional cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "forwarded mixed multi-group "
                        "recursive cyclic byte-lane "
                        "capability tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "forwarded mixed multi-group recursive "
                    "cyclic byte-lane capability tamper was "
                    "accepted"
                )
    if (
        args.expect_mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi
        and mixed_multigroup_recursive_cyclic_capability
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "mixed multi-group recursive cyclic byte-lane contract is incomplete"
        )
    if args.expect_mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phi:
        contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get(
                "mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            )
        ]
        transfers = [
            transfer
            for contract in contracts
            for transfer in contract.get("recursive_transfers", [])
        ]
        composed_leaf_count = 0
        repeated_source_count = 0
        for transfer in transfers:
            source_leaves: dict[str, set[str]] = {}
            for leaf in transfer.get("leaves", []):
                leaf_stores = {
                    str(lane.get("store", ""))
                    for lane in leaf.get("lanes", [])
                    if lane.get("source") == "store"
                }
                composed_leaf_count += 1 if len(leaf_stores) == 2 else 0
                for store in leaf_stores:
                    source_leaves.setdefault(store, set()).add(
                        str(leaf.get("block", ""))
                    )
            repeated_source_count += sum(
                1 for leaves in source_leaves.values() if len(leaves) >= 2
            )
        if (
            not contracts
            or len(transfers) != len(contracts)
            or composed_leaf_count != len(contracts)
            or repeated_source_count != 2 * len(contracts)
            or not all(
                transfer.get("multiple_groups") is True
                and transfer.get("mixed_groups") is True
                and int(transfer.get("group_count", 0)) == 2
                and int(transfer.get("composed_group_count", 0)) == 1
                and transfer.get("multicarry") is not True
                and transfer.get("composed_repeated_source") is not True
                and transfer.get("forwarded") is not True
                for transfer in transfers
            )
        ):
            raise AssertionError(
                "mixed multi-group recursive cyclic byte-lane metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get(
                "mixed_multigroup_recursive_conditional_cyclic_byte_lane_memory_definedness_phis",
                [],
            ):
                transfers = contract.get("recursive_transfers", [])
                if transfers:
                    transfers[0]["composed_group_count"] = 2
                    changed = True
                    break
            if changed:
                break
        if not changed:
            raise AssertionError(
                "mixed multi-group recursive cyclic byte-lane tamper target is missing"
            )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive cyclic byte-lane" not in str(exc):
                    raise AssertionError(
                        "mixed multi-group recursive cyclic "
                        "byte-lane count tamper failed for "
                        "the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "mixed multi-group recursive cyclic "
                    "byte-lane count tamper was accepted"
                )
        tampered = copy.deepcopy(artifact)
        capabilities = tampered.get("lowering", {}).get("capabilities", [])
        capabilities.remove(mixed_multigroup_recursive_cyclic_capability)
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if (
                    "mixed multi-group recursive "
                    "conditional cyclic byte-lane memory "
                    "definedness PHI" not in str(exc)
                ):
                    raise AssertionError(
                        "mixed multi-group recursive cyclic "
                        "byte-lane capability tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "mixed multi-group recursive cyclic "
                    "byte-lane capability tamper was accepted"
                )
    if (
        args.expect_multicell_memory_definedness_phi
        and "bounded-multicell-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("multi-cell memory definedness PHI contract is incomplete")
    if (
        args.expect_multicell_alias_graph
        and "bounded-multicell-alias-graph" not in lowering.get("capabilities", ())
    ):
        raise AssertionError("multi-cell alias graph contract is incomplete")
    if (
        args.expect_identified_object_multicell_memory_definedness_phi
        and "bounded-identified-object-multicell-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "identified-object multi-cell memory definedness PHI contract is incomplete"
        )
    if (
        args.expect_fixed_heap_object_multicell_memory_definedness_phi
        and "bounded-fixed-heap-object-multicell-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "fixed-heap-object multi-cell memory definedness PHI contract is incomplete"
        )
    if (
        args.expect_finite_pointer_domain_multicell_memory_definedness_phi
        and "bounded-finite-pointer-domain-multicell-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "finite-pointer-domain multi-cell memory "
            "definedness PHI contract is incomplete"
        )
    if (
        args.expect_guard_correlated_pointer_domain_multicell_memory_definedness_phi
        and "bounded-guard-correlated-pointer-domain-multicell-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "guard-correlated pointer-domain multi-cell "
            "memory definedness PHI contract is incomplete"
        )
    if (
        args.expect_symbolic_index_interval_multicell_memory_definedness_phi
        and "bounded-symbolic-index-interval-multicell-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "symbolic-index interval multi-cell "
            "memory definedness PHI contract is incomplete"
        )
    if (
        args.expect_phi_correlated_pointer_domain_multicell_memory_definedness_phi
        and "bounded-phi-correlated-pointer-domain-multicell-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "PHI-correlated pointer-domain multi-cell "
            "memory definedness PHI contract is incomplete"
        )
    if (
        args.expect_interprocedural_memory_definedness_phi
        and "bounded-interprocedural-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "interprocedural memory definedness PHI contract is incomplete"
        )
    if (
        args.expect_shared_phi_edge_discriminator
        and "bounded-shared-phi-edge-discriminator"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("shared PHI edge discriminator contract is incomplete")
    if (
        args.expect_multilevel_memory_definedness_phi
        and "bounded-multilevel-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("multilevel memory definedness PHI contract is incomplete")
    if (
        args.expect_cyclic_memory_definedness_phi
        and "bounded-cyclic-memory-definedness-phi"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("cyclic memory definedness PHI contract is incomplete")
    if (
        args.expect_cyclic_memory_definedness_carry
        and "bounded-cyclic-memory-definedness-carry"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("cyclic memory definedness carry contract is incomplete")
    if (
        args.expect_conditional_memory_definedness_carry
        and "bounded-conditional-memory-definedness-carry"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "conditional memory definedness carry contract is incomplete"
        )
    if (
        args.expect_forwarded_conditional_memory_definedness_carry
        and "bounded-forwarded-conditional-memory-definedness-carry"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "forwarded conditional memory definedness carry contract is incomplete"
        )
    if (
        args.expect_multiarm_conditional_memory_definedness_carry
        and "bounded-multiarm-conditional-memory-definedness-carry"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "multiarm conditional memory definedness carry contract is incomplete"
        )
    if (
        args.expect_equivalent_defined_store_memory_carry
        and "bounded-equivalent-defined-store-memory-carry"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "equivalent defined-store memory carry contract is incomplete"
        )
    if (
        args.expect_shared_poison_store_memory_carry
        and "bounded-shared-poison-store-memory-carry"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("shared poison-store memory carry contract is incomplete")
    if (
        args.expect_nested_conditional_memory_definedness_carry
        and "bounded-nested-conditional-memory-definedness-carry"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "nested conditional memory definedness carry contract is incomplete"
        )
    if (
        args.expect_recursive_memory_definedness_condition_tree
        and "bounded-recursive-memory-definedness-condition-tree"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "recursive memory definedness condition-tree contract is incomplete"
        )
    if (
        args.expect_grouped_recursive_memory_definedness_condition_tree
        and "bounded-grouped-recursive-memory-definedness-condition-tree"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "grouped recursive memory definedness condition-tree contract is incomplete"
        )
    if (
        args.expect_repeated_source_recursive_memory_definedness_condition_tree
        and "bounded-repeated-source-recursive-memory-definedness-condition-tree"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "repeated-source recursive memory definedness "
            "condition-tree contract is incomplete"
        )
    if (
        args.expect_multicarry_recursive_memory_definedness_condition_tree
        and "bounded-multicarry-recursive-memory-definedness-condition-tree"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "multi-carry recursive memory definedness "
            "condition-tree contract is incomplete"
        )
    if (
        args.expect_initial_memory_definedness_merge
        and "bounded-initial-memory-definedness-merge"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError("initial memory definedness merge contract is incomplete")
    if (
        args.expect_initial_subobject_definedness_merge
        and "bounded-initial-subobject-definedness-merge"
        not in lowering.get("capabilities", ())
    ):
        raise AssertionError(
            "initial subobject definedness merge contract is incomplete"
        )
    if args.expect_initial_subobject_definedness_merge:
        subobject_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("initial_subobject") is True
        ]
        if not subobject_contracts:
            raise AssertionError("initial subobject definedness metadata is missing")
        tampered = copy.deepcopy(artifact)
        removed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("initial_subobject") is True:
                    contract.pop("initial", None)
                    removed = True
                    break
            if removed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "initial subobject contract" not in str(exc):
                    raise AssertionError(
                        "initial subobject tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("initial subobject tamper was accepted")
    if args.expect_cyclic_memory_definedness_carry:
        carry_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("carry") is True
        ]
        if not carry_contracts:
            raise AssertionError("cyclic memory carry metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("carry") is True:
                    contract.pop("cyclic", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "cyclic memory carry tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("cyclic memory carry tamper was accepted")
    if args.expect_conditional_memory_definedness_carry:
        conditional_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("conditional_carry") is True
        ]
        if not conditional_contracts:
            raise AssertionError("conditional memory carry metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("conditional_carry") is True:
                    contract.pop("conditional_carry", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "conditional memory carry tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("conditional memory carry tamper was accepted")
    if args.expect_forwarded_conditional_memory_definedness_carry:
        forwarded_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("forwarded_conditional_carry") is True
        ]
        if not forwarded_contracts:
            raise AssertionError("forwarded conditional carry metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("forwarded_conditional_carry") is True:
                    contract.pop("conditional_carry", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "forwarded conditional carry tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("forwarded conditional carry tamper was accepted")
    if args.expect_multiarm_conditional_memory_definedness_carry:
        multiarm_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("multiarm_conditional_carry") is True
        ]
        if not multiarm_contracts:
            raise AssertionError("multiarm conditional carry metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("multiarm_conditional_carry") is True:
                    contract.pop("conditional_carry", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "multiarm conditional carry tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("multiarm conditional carry tamper was accepted")
    if args.expect_equivalent_defined_store_memory_carry:
        equivalent_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("equivalent_defined_stores") is True
        ]
        if not equivalent_contracts:
            raise AssertionError("equivalent defined-store metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("equivalent_defined_stores") is True:
                    contract.pop("multiarm_conditional_carry", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "equivalent defined-store tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("equivalent defined-store tamper was accepted")
    if args.expect_shared_poison_store_memory_carry:
        shared_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("shared_poison_stores") is True
        ]
        if not shared_contracts:
            raise AssertionError("shared poison-store metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("shared_poison_stores") is True:
                    contract.pop("multiarm_conditional_carry", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "shared poison-store tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("shared poison-store tamper was accepted")
    if args.expect_nested_conditional_memory_definedness_carry:
        nested_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("nested_conditional_carry") is True
        ]
        if not nested_contracts:
            raise AssertionError("nested conditional carry metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("nested_conditional_carry") is True:
                    contract.pop("multiarm_conditional_carry", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "nested conditional carry tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("nested conditional carry tamper was accepted")
    if args.expect_recursive_memory_definedness_condition_tree:
        recursive_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("recursive_conditional_carry") is True
        ]
        if not recursive_contracts or any(
            int(contract.get("condition_tree_depth", 0)) < 3
            or int(contract.get("condition_tree_leaves", 0)) < 4
            for contract in recursive_contracts
        ):
            raise AssertionError("recursive condition-tree metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                for endpoint in contract.get("incoming", []):
                    if endpoint.get("recursive_conditional_carry") is True:
                        endpoint["condition_tree_depth"] = (
                            int(endpoint.get("condition_tree_depth", 0)) + 1
                        )
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        if not changed:
            raise AssertionError("recursive condition-tree endpoint is missing")
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive condition-tree" not in str(exc):
                    raise AssertionError(
                        "recursive condition-tree tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("recursive condition-tree tamper was accepted")
    if args.expect_grouped_recursive_memory_definedness_condition_tree:
        grouped_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("grouped_recursive_conditional_carry") is True
        ]
        if not grouped_contracts:
            raise AssertionError("grouped recursive condition-tree metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("grouped_recursive_conditional_carry") is True:
                    contract.pop("recursive_conditional_carry", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "grouped recursive condition-tree "
                        "tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "grouped recursive condition-tree tamper was accepted"
                )
    if args.expect_repeated_source_recursive_memory_definedness_condition_tree:
        repeated_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("repeated_source_recursive_conditional_carry") is True
        ]
        if not repeated_contracts:
            raise AssertionError(
                "repeated-source recursive condition-tree metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("repeated_source_recursive_conditional_carry") is True:
                    contract.pop("recursive_conditional_carry", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "carry contract" not in str(exc):
                    raise AssertionError(
                        "repeated-source recursive "
                        "condition-tree tamper failed for "
                        "the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "repeated-source recursive condition-tree tamper was accepted"
                )
    if args.expect_multicarry_recursive_memory_definedness_condition_tree:
        multicarry_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("multicarry_recursive_conditional_carry") is True
        ]
        if not multicarry_contracts or any(
            int(contract.get("condition_tree_carry_leaves", 0)) < 2
            for contract in multicarry_contracts
        ):
            raise AssertionError(
                "multi-carry recursive condition-tree metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                for endpoint in contract.get("incoming", []):
                    if endpoint.get("multicarry_recursive_conditional_carry") is True:
                        endpoint["condition_tree_carry_leaves"] = (
                            int(endpoint.get("condition_tree_carry_leaves", 0)) + 1
                        )
                        changed = True
                        break
                if changed:
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "recursive condition-tree" not in str(exc):
                    raise AssertionError(
                        "multi-carry recursive "
                        "condition-tree tamper failed for "
                        "the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-carry recursive condition-tree tamper was accepted"
                )
    if args.expect_multicell_memory_definedness_phi:
        multicell_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("multicell") is True
        ]
        if (
            len(multicell_contracts) < 2
            or len({str(contract.get("block", "")) for contract in multicell_contracts})
            != 1
        ):
            raise AssertionError("multi-cell memory definedness metadata is missing")
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("multicell") is True:
                    contract.pop("multicell", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "multi-cell contract" not in str(
                    exc
                ) and "multi-cell alias graph" not in str(exc):
                    raise AssertionError(
                        "multi-cell memory definedness "
                        "tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-cell memory definedness tamper was accepted"
                )
    if args.expect_multicell_alias_graph:
        graph_functions = [
            function
            for function in artifact.get("functions", {}).values()
            if any(
                contract.get("alias_graph_neighbors")
                for contract in function.get("memory_defined_phis", [])
            )
        ]
        if len(graph_functions) != 1:
            raise AssertionError("multi-cell alias graph metadata is missing")
        graph_function = graph_functions[0]
        contracts = {
            str(contract.get("load", "")): contract
            for contract in graph_function.get("memory_defined_phis", [])
            if contract.get("alias_graph_neighbors")
        }
        edges = {
            tuple(sorted((load, str(neighbor))))
            for load, contract in contracts.items()
            for neighbor in contract.get("alias_graph_neighbors", [])
        }
        if not edges or any(
            neighbor not in contracts
            or load not in contracts[neighbor].get("alias_graph_neighbors", [])
            for load, contract in contracts.items()
            for neighbor in contract.get("alias_graph_neighbors", [])
        ):
            raise AssertionError("multi-cell alias graph is not symmetric")

        missing_capability = copy.deepcopy(artifact)
        missing_capabilities = missing_capability["lowering"]["capabilities"]
        missing_capabilities.remove("bounded-multicell-alias-graph")
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(missing_capability)
            except ValueError as exc:
                if "multi-cell alias graph" not in str(exc):
                    raise AssertionError(
                        "alias graph capability tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "multi-cell alias graph without capability was accepted"
                )

        asymmetric = copy.deepcopy(artifact)
        changed = False
        for function in asymmetric.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                neighbors = contract.get("alias_graph_neighbors", [])
                if neighbors:
                    neighbors.pop()
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(asymmetric)
            except ValueError as exc:
                if "multi-cell alias graph" not in str(
                    exc
                ) and "PHI-correlated pointer-domain" not in str(exc):
                    raise AssertionError(
                        "asymmetric alias graph tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("asymmetric multi-cell alias graph was accepted")

        left_load, right_load = next(iter(edges))
        overlap = copy.deepcopy(artifact)
        overlap_function = next(
            function
            for function in overlap.get("functions", {}).values()
            if any(
                str(contract.get("load", "")) == left_load
                for contract in function.get("memory_defined_phis", [])
            )
        )
        overlap_contracts = {
            str(contract.get("load", "")): contract
            for contract in overlap_function.get("memory_defined_phis", [])
        }

        def load_instruction(load_name: str) -> dict[str, object]:
            contract = overlap_contracts[load_name]
            instructions = overlap_function["blocks"][contract["block"]]
            return next(
                instruction
                for instruction in instructions
                if instruction.get("op") == "load"
                and str(instruction.get("dst", "")) == load_name
            )

        def first_address(instruction: dict[str, object]) -> int:
            cases = instruction.get("alias_cases")
            if isinstance(cases, list) and cases:
                return int(cases[0]["addresses"][0])
            aliases = instruction.get("aliases")
            if isinstance(aliases, list) and aliases:
                return int(aliases[0])
            return int(instruction["address"]["const"])

        def first_index(instruction: dict[str, object]) -> int | None:
            cases = instruction.get("alias_cases")
            if isinstance(cases, list) and cases:
                values = cases[0].get("alias_index_values")
                if isinstance(values, list) and values:
                    return int(values[0])
            values = instruction.get("alias_index_values")
            if isinstance(values, list) and values:
                return int(values[0])
            return None

        def replace_first_index(
            container: dict[str, object], value: int | None
        ) -> None:
            values = container.get("alias_index_values")
            if value is None or not isinstance(values, list) or not values:
                return
            values[0] = value
            container["alias_index_min"] = min(values)
            container["alias_index_max"] = max(values)

        left_instruction = load_instruction(left_load)
        right_instruction = load_instruction(right_load)
        conflicting_address = first_address(left_instruction)
        conflicting_index = first_index(left_instruction)
        right_cases = right_instruction.get("alias_cases")
        if isinstance(right_cases, list) and right_cases:
            right_cases[0]["addresses"][0] = conflicting_address
            right_cases[0]["guards"] = []
            replace_first_index(right_cases[0], conflicting_index)
        elif isinstance(right_instruction.get("aliases"), list):
            right_instruction["aliases"][0] = conflicting_address
            replace_first_index(right_instruction, conflicting_index)
        else:
            right_instruction["address"]["const"] = conflicting_address
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(overlap)
            except ValueError as exc:
                if "feasible overlap" not in str(exc):
                    raise AssertionError(
                        "overlapping alias graph tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("overlapping multi-cell alias graph was accepted")
    if args.expect_shared_phi_edge_discriminator:
        discriminator_functions = [
            function
            for function in artifact.get("functions", {}).values()
            if function.get("phi_edge_discriminators")
        ]
        if len(discriminator_functions) != 1:
            raise AssertionError("shared PHI edge discriminator metadata is missing")
        discriminator_function = discriminator_functions[0]
        discriminators = discriminator_function["phi_edge_discriminators"]
        if not isinstance(discriminators, list) or not discriminators:
            raise AssertionError("shared PHI edge discriminator metadata is empty")

        missing_capability = copy.deepcopy(artifact)
        missing_capability["lowering"]["capabilities"].remove(
            "bounded-shared-phi-edge-discriminator"
        )
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(missing_capability)
            except ValueError as exc:
                if "shared PHI edge discriminator" not in str(exc):
                    raise AssertionError(
                        "shared PHI capability tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "shared PHI contract without capability was accepted"
                )

        changed_assignment = copy.deepcopy(artifact)
        changed_function = next(
            function
            for function in changed_assignment.get("functions", {}).values()
            if function.get("phi_edge_discriminators")
        )
        discriminator = changed_function["phi_edge_discriminators"][0]
        endpoint = discriminator["incoming"][0]
        endpoint["value"] = int(endpoint["value"]) + 1
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(changed_assignment)
            except ValueError as exc:
                if "shared PHI edge discriminator" not in str(exc):
                    raise AssertionError(
                        "shared PHI assignment tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("drifted shared PHI assignment was accepted")
    if args.expect_identified_object_multicell_memory_definedness_phi:
        identified_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("identified_objects") is True
        ]
        if (
            len(identified_contracts) < 2
            or any(
                contract.get("multicell") is not True
                for contract in identified_contracts
            )
            or len(
                {str(contract.get("block", "")) for contract in identified_contracts}
            )
            != 1
        ):
            raise AssertionError(
                "identified-object multi-cell memory definedness metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("identified_objects") is True:
                    contract.pop("identified_objects", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "identified-object multi-cell contract" not in str(exc):
                    raise AssertionError(
                        "identified-object multi-cell "
                        "memory definedness tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "identified-object multi-cell memory "
                    "definedness tamper was accepted"
                )
    if args.expect_fixed_heap_object_multicell_memory_definedness_phi:
        heap_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("fixed_heap_objects") is True
        ]
        if (
            len(heap_contracts) < 2
            or any(contract.get("multicell") is not True for contract in heap_contracts)
            or len({str(contract.get("block", "")) for contract in heap_contracts}) != 1
        ):
            raise AssertionError(
                "fixed-heap-object multi-cell memory definedness metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("fixed_heap_objects") is True:
                    contract.pop("fixed_heap_objects", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "fixed-heap-object multi-cell contract" not in str(exc):
                    raise AssertionError(
                        "fixed-heap-object multi-cell "
                        "memory definedness tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "fixed-heap-object multi-cell memory "
                    "definedness tamper was accepted"
                )
    if args.expect_finite_pointer_domain_multicell_memory_definedness_phi:
        domain_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("finite_pointer_domains") is True
        ]
        if (
            len(domain_contracts) < 2
            or any(
                contract.get("multicell") is not True for contract in domain_contracts
            )
            or len({str(contract.get("block", "")) for contract in domain_contracts})
            != 1
        ):
            raise AssertionError(
                "finite-pointer-domain multi-cell memory "
                "definedness metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("finite_pointer_domains") is True:
                    contract.pop("finite_pointer_domains", None)
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "finite-pointer-domain multi-cell contract" not in str(exc):
                    raise AssertionError(
                        "finite-pointer-domain multi-cell "
                        "memory definedness tamper failed "
                        "for the wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "finite-pointer-domain multi-cell memory "
                    "definedness tamper was accepted"
                )
    if args.expect_guard_correlated_pointer_domain_multicell_memory_definedness_phi:
        correlated_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("guard_correlated_pointer_domains") is True
        ]
        if (
            len(correlated_contracts) < 2
            or any(
                contract.get("multicell") is not True
                for contract in correlated_contracts
            )
            or len(
                {str(contract.get("block", "")) for contract in correlated_contracts}
            )
            != 1
        ):
            raise AssertionError(
                "guard-correlated pointer-domain multi-cell metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("guard_correlated_pointer_domains") is True:
                    contract.pop(
                        "guard_correlated_pointer_domains",
                        None,
                    )
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "guard-correlated pointer-domain multi-cell contract" not in str(
                    exc
                ):
                    raise AssertionError(
                        "guard-correlated pointer-domain "
                        "multi-cell tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "guard-correlated pointer-domain multi-cell tamper was accepted"
                )
    if args.expect_phi_correlated_pointer_domain_multicell_memory_definedness_phi:
        phi_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("phi_correlated_pointer_domains") is True
        ]
        if (
            len(phi_contracts) < 2
            or any(contract.get("multicell") is not True for contract in phi_contracts)
            or len({str(contract.get("block", "")) for contract in phi_contracts}) != 1
        ):
            raise AssertionError(
                "PHI-correlated pointer-domain multi-cell metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("phi_correlated_pointer_domains") is True:
                    contract.pop(
                        "phi_correlated_pointer_domains",
                        None,
                    )
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "PHI-correlated pointer-domain multi-cell contract" not in str(exc):
                    raise AssertionError(
                        "PHI-correlated pointer-domain "
                        "multi-cell tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "PHI-correlated pointer-domain multi-cell tamper was accepted"
                )
    if args.expect_symbolic_index_interval_multicell_memory_definedness_phi:
        interval_contracts = [
            contract
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
            if contract.get("symbolic_index_intervals") is True
        ]
        if (
            len(interval_contracts) < 2
            or any(
                contract.get("multicell") is not True for contract in interval_contracts
            )
            or len({str(contract.get("block", "")) for contract in interval_contracts})
            != 1
        ):
            raise AssertionError(
                "symbolic-index interval multi-cell metadata is missing"
            )
        tampered = copy.deepcopy(artifact)
        changed = False
        for function in tampered.get("functions", {}).values():
            for contract in function.get("memory_defined_phis", []):
                if contract.get("symbolic_index_intervals") is True:
                    contract.pop(
                        "symbolic_index_intervals",
                        None,
                    )
                    changed = True
                    break
            if changed:
                break
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "symbolic-index interval multi-cell contract" not in str(exc):
                    raise AssertionError(
                        "symbolic-index interval "
                        "multi-cell tamper failed for the "
                        "wrong reason"
                    ) from exc
            else:
                raise AssertionError(
                    "symbolic-index interval multi-cell tamper was accepted"
                )
    if args.expect_memory_definedness_phi:
        contracts = [
            (function, contract)
            for function in artifact.get("functions", {}).values()
            for contract in function.get("memory_defined_phis", [])
        ]
        if not contracts:
            raise AssertionError("memory definedness PHI metadata is missing")
        tampered = copy.deepcopy(artifact)
        tampered_functions = tampered["functions"]
        removed = False
        for function in tampered_functions.values():
            for contract in function.get("memory_defined_phis", []):
                defined = str(contract.get("defined", ""))
                incoming = contract.get("incoming", [])
                if not incoming:
                    continue
                for endpoint in incoming:
                    block_name = str(endpoint.get("block", ""))
                    block = function.get("blocks", {}).get(block_name, [])
                    for index, instruction in enumerate(block):
                        if (
                            instruction.get("op") in {"unary", "select"}
                            and str(instruction.get("dst", "")) == defined
                            and int(instruction.get("bits", 0)) == 1
                        ):
                            del block[index]
                            removed = True
                            break
                    if removed:
                        break
                if removed:
                    break
            if removed:
                break
        if not removed:
            raise AssertionError("memory definedness PHI endpoint is missing")
        with tempfile.TemporaryDirectory() as tmp:
            executor = LiveContinuationExecutor(LiveStateStore(tmp, page_size=64))
            try:
                executor.create(tampered)
            except ValueError as exc:
                if "memory definedness PHI" not in str(exc):
                    raise AssertionError(
                        "memory PHI tamper failed for the wrong reason"
                    ) from exc
            else:
                raise AssertionError("memory PHI tamper was accepted")
    if args.validate_only:
        with tempfile.TemporaryDirectory() as tmp:
            LiveContinuationExecutor(
                LiveStateStore(tmp, page_size=64),
                enable_loop_summary_transfer=args.enable_loop_summary_transfer,
            ).create(artifact, input_bytes=bytes.fromhex(args.input_hex))
        return 0
    expected = sorted(int(item) for item in args.expect_values.split(",") if item)
    with tempfile.TemporaryDirectory() as tmp:
        executor = LiveContinuationExecutor(
            LiveStateStore(tmp, page_size=64),
            enable_loop_summary_transfer=args.enable_loop_summary_transfer,
        )
        checkpoint = executor.create(
            artifact, input_bytes=bytes.fromhex(args.input_hex)
        )
        if args.expect_runtime_error:
            try:
                executor.resume(checkpoint, max_steps=512, max_states=32)
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                if args.expect_runtime_error not in str(exc):
                    raise AssertionError(
                        "unexpected runtime error: " + str(exc)
                    ) from exc
                return 0
            raise AssertionError("continuation execution unexpectedly succeeded")
        result = executor.resume(checkpoint, max_steps=512, max_states=32)
    values = sorted(
        int(row["value"]) for row in result["halted"] if row.get("status") == "returned"
    )
    if values != expected:
        raise AssertionError(f"expected {expected}, got {values}: {result}")
    if args.expect_zero_forks and result.get("forks") != 0:
        raise AssertionError(
            f"expected zero continuation forks, got {result.get('forks')}"
        )
    if args.expect_loop_summary_transfer_applied:
        if (
            not result.get("loop_summary_transfer_enabled")
            or result.get("loop_summary_transfers_applied", 0) < 1
            or result.get("loop_summary_transfer_fallbacks") != 0
        ):
            raise AssertionError(
                "expected one enabled executable loop summary transfer: "
                f"{result}"
            )
    if args.expect_loop_summary_transfer_fallback:
        if (
            result.get("loop_summary_transfer_enabled")
            or result.get("loop_summary_transfers_applied") != 0
            or result.get("loop_summary_transfer_fallbacks", 0) < 1
        ):
            raise AssertionError(
                "expected the executable loop summary fallback path: "
                f"{result}"
            )
    if (
        args.expect_max_steps is not None
        and int(result.get("steps", 0)) > args.expect_max_steps
    ):
        raise AssertionError(
            f"expected at most {args.expect_max_steps} steps, "
            f"got {result.get('steps')}"
        )
    if args.expect_infeasible and not any(
        row.get("status") == "infeasible" for row in result["halted"]
    ):
        raise AssertionError(f"expected an infeasible state, got: {result}")
    if args.expect_unhandled_exception and not any(
        row.get("status") == "unhandled-exception" for row in result["halted"]
    ):
        raise AssertionError(f"expected an unhandled exception, got: {result}")
    if len(expected) > 1 and result["forks"] < 1:
        raise AssertionError("lowered symbolic branch did not fork")
    if (
        len(expected) > 1
        and result["feasibility_solver"] == "symcc-query-solver"
        and result["feasibility_checks"] < 1
    ):
        raise AssertionError("lowered branches bypassed feasibility checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
