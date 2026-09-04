#!/usr/bin/env python3
"""Inspect and verify content-addressed SymCC live-state checkpoints."""

from __future__ import annotations

import argparse
import json
import os
import shlex
from typing import Any

from distributed_state import LiveStateStore
from live_continuation import LiveContinuationExecutor
from live_state_frontier import PersistentLiveStateFrontier
from live_state_search import LiveStateSearchPolicy
from llvm_to_continuation import lower_llvm_to_program


def _bundle_mapping(bundle: Any) -> dict[str, Any]:
    return {
        "schema": "symcc-live-continuation-bundle-v1",
        "checkpoint_id": bundle.checkpoint_id,
        "descriptor": bundle.descriptor.to_mapping(),
        "solver_frames": [
            list(frame) for frame in bundle.solver_frames
        ],
        "symbolic_store": [
            list(entry) for entry in bundle.symbolic_store
        ],
        "memory": {
            "size": bundle.memory_size,
            "page_size": bundle.memory_page_size,
            "pages": [list(entry) for entry in bundle.memory_pages],
        },
        "graph_verification": {
            "unique_objects": bundle.graph_object_count,
            "canonical_bytes": bundle.graph_canonical_bytes,
        },
        "verified": True,
        "native_resume_supported": False,
        "continuation_ir_resume_supported": (
            bundle.descriptor.engine == "symcc-continuation-ir"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("store", help="LiveStateStore root directory")
    parser.add_argument(
        "--page-size", type=int, default=4096,
        help="Page size used when the store was created",
    )
    parser.add_argument(
        "--max-graph-objects", type=int, default=262_144,
        help="Maximum unique objects admitted by one checkpoint restore",
    )
    parser.add_argument(
        "--max-graph-bytes", type=int, default=256 * 1024 * 1024,
        help="Maximum canonical bytes admitted by one checkpoint restore",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect", help="Verify and materialize a checkpoint bundle"
    )
    inspect_parser.add_argument("checkpoint_id")
    diff_parser = subparsers.add_parser(
        "memory-diff", help="List changed COW pages between two roots"
    )
    diff_parser.add_argument("left_root")
    diff_parser.add_argument("right_root")
    run_parser = subparsers.add_parser(
        "run-program", help="Create and execute a continuation IR program"
    )
    run_parser.add_argument("program")
    run_parser.add_argument("--input-hex", default="")
    run_parser.add_argument("--max-steps", type=int, default=1000)
    run_parser.add_argument("--max-states", type=int, default=64)
    llvm_parser = subparsers.add_parser(
        "run-llvm",
        help="Lower an LLVM/C module and execute its continuation IR",
    )
    llvm_parser.add_argument("source")
    llvm_parser.add_argument("--entry", default="main")
    llvm_parser.add_argument("--program-output", default="")
    llvm_parser.add_argument("--plugin", default="")
    llvm_parser.add_argument("--compiler-args", default="")
    llvm_parser.add_argument("--input-hex", default="")
    llvm_parser.add_argument("--max-steps", type=int, default=1000)
    llvm_parser.add_argument("--max-states", type=int, default=64)
    resume_parser = subparsers.add_parser(
        "resume", help="Execute an existing continuation IR checkpoint"
    )
    resume_parser.add_argument("checkpoint_id")
    resume_parser.add_argument("--max-steps", type=int, default=1000)
    resume_parser.add_argument("--max-states", type=int, default=64)
    persistent_parser = subparsers.add_parser(
        "resume-persistent",
        help="Execute a crash-recoverable, lease-fenced checkpoint frontier",
    )
    persistent_parser.add_argument("checkpoint_id")
    persistent_parser.add_argument("frontier")
    persistent_parser.add_argument("--owner", required=True)
    persistent_parser.add_argument("--worker", type=int, default=0)
    persistent_parser.add_argument("--max-claims", type=int, default=64)
    persistent_parser.add_argument("--max-steps", type=int, default=1000)
    persistent_parser.add_argument("--max-states", type=int, default=64)
    persistent_parser.add_argument(
        "--frontier-max-states", type=int, default=100_000,
    )
    persistent_parser.add_argument(
        "--candidate-window", type=int, default=4096,
    )
    persistent_parser.add_argument("--lease-ttl", type=float, default=300.0)
    persistent_parser.add_argument(
        "--conflict-retries", type=int, default=64,
    )
    frontier_parser = subparsers.add_parser(
        "frontier-inspect",
        help="Verify and inspect a persistent live-state frontier",
    )
    frontier_parser.add_argument("frontier")
    frontier_parser.add_argument(
        "--frontier-max-states", type=int, default=100_000,
    )
    args = parser.parse_args()

    store = LiveStateStore(
        args.store,
        page_size=args.page_size,
        max_graph_objects=args.max_graph_objects,
        max_graph_bytes=args.max_graph_bytes,
    )
    if args.command == "inspect":
        result = _bundle_mapping(
            store.restore_continuation(args.checkpoint_id)
        )
    elif args.command == "memory-diff":
        changed = store.memory_diff(args.left_root, args.right_root)
        result = {
            "schema": "symcc-live-memory-diff-v1",
            "left_root": args.left_root,
            "right_root": args.right_root,
            "changed_pages": list(changed),
            "changed_page_count": len(changed),
            "verified": True,
        }
    elif args.command == "frontier-inspect":
        snapshot = PersistentLiveStateFrontier(
            args.frontier,
            max_states=args.frontier_max_states,
        ).snapshot()
        result = {
            **snapshot.telemetry(),
            "ready_checkpoints": list(snapshot.ready),
            "leases": [
                {
                    "checkpoint": lease.checkpoint_id,
                    "owner": lease.owner,
                    "worker": lease.worker,
                    "expires": lease.expires,
                    "claim_generation": lease.claim_generation,
                }
                for lease in snapshot.leases
            ],
            "done_checkpoints": list(snapshot.done),
            "state_search": LiveStateSearchPolicy.from_snapshot(
                snapshot.search
            ).telemetry(),
            "verified": True,
        }
    elif args.command in {"run-program", "run-llvm"}:
        lowering = None
        if args.command == "run-llvm":
            program_path = (
                args.program_output
                or os.path.join(args.store, "lowered_program.json")
            )
            lowering = lower_llvm_to_program(
                args.source,
                program_path,
                entry=args.entry,
                plugin=args.plugin,
                compiler_args=shlex.split(args.compiler_args),
            )
            if lowering["status"] != "lowered":
                raise ValueError(
                    "LLVM continuation lowering rejected the module: "
                    + "; ".join(lowering.get("diagnostics", ()))
                )
        else:
            program_path = args.program
        with open(program_path, encoding="utf-8") as stream:
            program = json.load(stream)
        try:
            input_bytes = bytes.fromhex(args.input_hex)
        except ValueError as exc:
            raise ValueError("--input-hex is not valid hexadecimal") from exc
        with LiveContinuationExecutor(store) as executor:
            checkpoint = executor.create(program, input_bytes=input_bytes)
            result = executor.resume(
                checkpoint,
                max_steps=args.max_steps,
                max_states=args.max_states,
            )
        if lowering is not None:
            result["lowering"] = lowering
    elif args.command == "resume":
        with LiveContinuationExecutor(store) as executor:
            result = executor.resume(
                args.checkpoint_id,
                max_steps=args.max_steps,
                max_states=args.max_states,
            )
    else:
        executor = LiveContinuationExecutor(store)
        try:
            result = executor.resume_persistent(
                args.checkpoint_id,
                args.frontier,
                owner=args.owner,
                worker=args.worker,
                max_claims=args.max_claims,
                max_steps_per_claim=args.max_steps,
                max_states_per_claim=args.max_states,
                frontier_max_states=args.frontier_max_states,
                candidate_window=args.candidate_window,
                lease_ttl=args.lease_ttl,
                conflict_retries=args.conflict_retries,
            )
        finally:
            executor.close()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
