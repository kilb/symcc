# RUN: python3 %s

import copy
import itertools
import json
import subprocess
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from condpor_interpreter import (  # noqa: E402
    CONDPOR_INTERPRETER_SCHEMA,
    CONDPOR_PROGRAM_SCHEMA,
    explore_condpor_program,
    validate_condpor_program,
    verify_condpor_interpreter_certificate,
)


def program(threads, *, memory=None, width=8):
    return {
        "schema": CONDPOR_PROGRAM_SCHEMA,
        "bit_width": width,
        "memory": {"x": 0} if memory is None else memory,
        "threads": threads,
    }


def explicit_sc_graphs(subject):
    """Independent concrete SC interleaving oracle for a test-only IR subset."""
    threads = copy.deepcopy(subject["threads"])
    for instructions in threads.values():
        labels = {
            instruction["name"]: pc
            for pc, instruction in enumerate(instructions)
            if instruction["op"] == "label"
        }
        for instruction in instructions:
            if instruction["op"] == "branch":
                instruction["then"] = labels[instruction["then"]]
                instruction["else"] = labels[instruction["else"]]
            elif instruction["op"] == "jump":
                instruction["target"] = labels[instruction["target"]]

    def prepare(state, tid):
        while True:
            pc = state["pc"][tid]
            instructions = threads[tid]
            if pc >= len(instructions):
                state["done"][tid] = True
                return None
            instruction = instructions[pc]
            op = instruction["op"]
            if op in {"read", "write", "branch"}:
                return instruction
            if op in {"label", "nop"}:
                state["pc"][tid] += 1
            elif op == "jump":
                state["pc"][tid] = instruction["target"]
            elif op == "halt":
                state["done"][tid] = True
                return None
            else:
                raise AssertionError(f"oracle does not support {op}")

    def signature(state):
        return (
            tuple(sorted(state["events"].items())),
            tuple(sorted(state["rf"].items())),
            tuple((obj, tuple(order)) for obj, order in sorted(state["co"].items())),
            tuple(sorted(state["outcomes"].items())),
        )

    initial = {
        "pc": {tid: 0 for tid in threads},
        "index": {tid: 0 for tid in threads},
        "locals": {tid: {} for tid in threads},
        "done": {tid: False for tid in threads},
        "memory": dict(subject["memory"]),
        "source": {obj: f"init:{obj}" for obj in subject["memory"]},
        "co": {obj: [f"init:{obj}"] for obj in subject["memory"]},
        "rf": {},
        "outcomes": {},
        "events": {},
    }
    completed = set()

    def walk(state):
        options = []
        for tid in sorted(threads, key=int):
            if state["done"][tid]:
                continue
            candidate = copy.deepcopy(state)
            instruction = prepare(candidate, tid)
            if instruction is not None:
                options.append((tid, candidate, instruction))
        if not options:
            completed.add(signature(state))
            return
        for tid, candidate, instruction in options:
            pc = candidate["pc"][tid]
            index = candidate["index"][tid]
            event_id = f"t{tid}:e{index}"
            op = instruction["op"]
            kind = {"read": "R", "write": "W", "branch": "C"}[op]
            obj = instruction.get("object", op)
            candidate["events"][event_id] = (pc, kind, op, obj)
            candidate["index"][tid] += 1
            if op == "read":
                obj = instruction["object"]
                candidate["locals"][tid][instruction["dst"]] = candidate["memory"][obj]
                candidate["rf"][event_id] = candidate["source"][obj]
                candidate["pc"][tid] += 1
            elif op == "write":
                obj = instruction["object"]
                raw_value = instruction["value"]
                value = (
                    candidate["locals"][tid][raw_value]
                    if isinstance(raw_value, str)
                    else raw_value
                )
                candidate["memory"][obj] = value
                candidate["source"][obj] = event_id
                candidate["co"][obj].append(event_id)
                candidate["pc"][tid] += 1
            else:
                left, right = instruction["condition"]["args"]
                left_value = (
                    candidate["locals"][tid][left] if isinstance(left, str) else left
                )
                right_value = (
                    candidate["locals"][tid][right] if isinstance(right, str) else right
                )
                outcome = left_value == right_value
                candidate["outcomes"][event_id] = outcome
                candidate["pc"][tid] = instruction["then" if outcome else "else"]
            walk(candidate)

    walk(initial)
    return completed


class CondporInterpreterTests(unittest.TestCase):
    def test_backward_revisit_regenerates_control_dependent_path(self):
        subject = program(
            {
                "0": [
                    {"op": "read", "object": "x", "dst": "r"},
                    {
                        "op": "branch",
                        "condition": {"op": "eq", "args": ["r", 1]},
                        "then": "failure",
                        "else": "done",
                    },
                    {"op": "label", "name": "failure"},
                    {"op": "assert", "condition": False},
                    {"op": "label", "name": "done"},
                    {"op": "halt"},
                ],
                "1": [
                    {"op": "write", "object": "x", "value": 1},
                    {"op": "halt"},
                ],
            }
        )

        certificate = explore_condpor_program(subject)

        self.assertEqual(certificate["schema"], CONDPOR_INTERPRETER_SCHEMA)
        self.assertEqual(certificate["status"], "complete")
        self.assertTrue(certificate["bounded_exhaustive"])
        self.assertEqual(len(certificate["executions"]), 1)
        self.assertEqual(len(certificate["errors"]), 1)
        self.assertEqual(certificate["statistics"]["accepted_backward_revisits"], 1)
        revisit = next(
            row
            for row in certificate["backward_revisits"]
            if row["status"] == "accepted"
        )
        self.assertEqual(revisit["read"], "t0:e0")
        self.assertEqual(revisit["write"], "t1:e0")
        self.assertEqual(revisit["deleted_events"], ["t0:e1"])

        normal_read = certificate["executions"][0]["events"][0]
        self.assertEqual(normal_read["read_from"], "init:x")
        failure = certificate["errors"][0]
        self.assertEqual(failure["error"]["kind"], "assertion_failure")
        failure_events = {row["id"]: row for row in failure["events"]}
        self.assertEqual(failure_events["t0:e0"]["read_from"], "t1:e0")
        self.assertTrue(failure_events["t0:e1"]["outcome"])
        self.assertEqual(
            [row["id"] for row in failure["events"]],
            ["t0:e0", "t1:e0", "t0:e1", "t0:e2"],
        )
        self.assertTrue(verify_condpor_interpreter_certificate(certificate))

    def test_read_from_enumerates_initial_and_current_writes(self):
        certificate = explore_condpor_program(
            program(
                {
                    "0": [
                        {"op": "write", "object": "x", "value": 5},
                        {"op": "halt"},
                    ],
                    "1": [
                        {"op": "read", "object": "x", "dst": "r"},
                        {"op": "halt"},
                    ],
                }
            )
        )

        self.assertEqual(len(certificate["executions"]), 2)
        sources = {
            next(row for row in execution["events"] if row["kind"] == "R")["read_from"]
            for execution in certificate["executions"]
        }
        self.assertEqual(sources, {"init:x", "t0:e0"})
        self.assertEqual(certificate["statistics"]["pruned_by_reason"], {})

    def test_write_addition_enumerates_total_coherence_orders(self):
        certificate = explore_condpor_program(
            program(
                {
                    str(tid): [
                        {"op": "write", "object": "x", "value": tid + 1},
                        {"op": "halt"},
                    ]
                    for tid in range(3)
                }
            )
        )

        self.assertEqual(len(certificate["executions"]), 6)
        orders = {
            tuple(execution["graph"]["co"]["x"])
            for execution in certificate["executions"]
        }
        self.assertEqual(len(orders), 6)
        self.assertTrue(all(order[0] == "init:x" for order in orders))

    def test_z3_prunes_unsat_outcome_and_canonicalizes_model(self):
        subject = program(
            {
                "0": [
                    {"op": "symbol", "dst": "a"},
                    {
                        "op": "assume",
                        "condition": {"op": "eq", "args": ["a", 7]},
                    },
                    {
                        "op": "branch",
                        "condition": {"op": "eq", "args": ["a", 7]},
                        "then": "done",
                        "else": "impossible",
                    },
                    {"op": "label", "name": "impossible"},
                    {"op": "assert", "condition": False},
                    {"op": "label", "name": "done"},
                    {"op": "halt"},
                ],
            }
        )

        certificate = explore_condpor_program(subject)

        self.assertEqual(len(certificate["executions"]), 1)
        self.assertEqual(len(certificate["errors"]), 0)
        self.assertGreaterEqual(
            certificate["statistics"]["pruned_by_reason"]["path_unsat"], 1
        )
        self.assertEqual(
            certificate["executions"][0]["canonical_unsigned_model"],
            {"sym_t0_e0": 7},
        )

    def test_assertion_failure_has_replayable_minimal_witness(self):
        certificate = explore_condpor_program(
            program(
                {
                    "0": [
                        {"op": "symbol", "dst": "a"},
                        {
                            "op": "assert",
                            "condition": {"op": "eq", "args": ["a", 42]},
                        },
                        {"op": "halt"},
                    ],
                }
            )
        )

        self.assertEqual(len(certificate["executions"]), 1)
        self.assertEqual(len(certificate["errors"]), 1)
        self.assertEqual(
            certificate["executions"][0]["canonical_unsigned_model"],
            {"sym_t0_e0": 42},
        )
        self.assertEqual(
            certificate["errors"][0]["canonical_unsigned_model"],
            {"sym_t0_e0": 0},
        )

    def test_sc_cycle_is_rejected_during_read_from_exploration(self):
        certificate = explore_condpor_program(
            program(
                {
                    "0": [
                        {"op": "write", "object": "x", "value": 1},
                        {"op": "read", "object": "x", "dst": "r"},
                        {"op": "halt"},
                    ],
                }
            )
        )

        self.assertEqual(len(certificate["executions"]), 1)
        read = next(
            row for row in certificate["executions"][0]["events"] if row["kind"] == "R"
        )
        self.assertEqual(read["read_from"], "t0:e0")
        self.assertEqual(certificate["statistics"]["pruned_by_reason"]["sc_cycle"], 1)

    def test_straight_line_search_matches_independent_sc_oracle(self):
        certificate = explore_condpor_program(
            program(
                {
                    "0": [
                        {"op": "write", "object": "x", "value": 1},
                        {"op": "read", "object": "x", "dst": "a"},
                        {"op": "halt"},
                    ],
                    "1": [
                        {"op": "write", "object": "x", "value": 2},
                        {"op": "read", "object": "x", "dst": "b"},
                        {"op": "halt"},
                    ],
                }
            )
        )

        writes = ("t0:e0", "t1:e0")
        reads = ("t0:e1", "t1:e1")
        nodes = {"init:x", *writes, *reads}

        def acyclic(edges):
            successors = {node: set() for node in nodes}
            indegree = {node: 0 for node in nodes}
            for source, target in edges:
                if target not in successors[source]:
                    successors[source].add(target)
                    indegree[target] += 1
            ready = [node for node in nodes if indegree[node] == 0]
            visited = 0
            while ready:
                source = ready.pop()
                visited += 1
                for target in successors[source]:
                    indegree[target] -= 1
                    if indegree[target] == 0:
                        ready.append(target)
            return visited == len(nodes)

        expected = set()
        for write_order in itertools.permutations(writes):
            coherence = ("init:x", *write_order)
            positions = {node: index for index, node in enumerate(coherence)}
            for sources in itertools.product(coherence, repeat=2):
                edges = {
                    ("t0:e0", "t0:e1"),
                    ("t1:e0", "t1:e1"),
                    *zip(coherence, coherence[1:]),
                    *((source, read) for source, read in zip(sources, reads)),
                }
                for source, read in zip(sources, reads):
                    edges.update(
                        (read, later) for later in coherence[positions[source] + 1 :]
                    )
                if acyclic(edges):
                    expected.add((coherence, tuple(zip(reads, sources))))

        actual = {
            (
                tuple(execution["graph"]["co"]["x"]),
                tuple(sorted(execution["graph"]["rf"].items())),
            )
            for execution in certificate["executions"]
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 4)

    def test_path_regeneration_matches_explicit_sc_interleaving_oracle(self):
        subject = program(
            {
                "0": [
                    {"op": "read", "object": "x", "dst": "r"},
                    {
                        "op": "branch",
                        "condition": {"op": "eq", "args": ["r", 1]},
                        "then": "one",
                        "else": "two",
                    },
                    {"op": "label", "name": "one"},
                    {"op": "write", "object": "y", "value": 1},
                    {"op": "jump", "target": "end"},
                    {"op": "label", "name": "two"},
                    {"op": "write", "object": "y", "value": 2},
                    {"op": "label", "name": "end"},
                    {"op": "read", "object": "y", "dst": "z"},
                    {"op": "halt"},
                ],
                "1": [
                    {"op": "write", "object": "x", "value": 1},
                    {"op": "read", "object": "y", "dst": "q"},
                    {
                        "op": "branch",
                        "condition": {"op": "eq", "args": ["q", 2]},
                        "then": "yes",
                        "else": "no",
                    },
                    {"op": "label", "name": "yes"},
                    {"op": "nop"},
                    {"op": "label", "name": "no"},
                    {"op": "halt"},
                ],
            },
            memory={"x": 0, "y": 0},
        )
        certificate = explore_condpor_program(subject)

        actual = set()
        for execution in certificate["executions"]:
            graph = execution["graph"]
            events = {
                event["id"]: (
                    event["pc"],
                    event["kind"],
                    event["op"],
                    event["object"],
                )
                for event in graph["events"]
            }
            actual.add(
                (
                    tuple(sorted(events.items())),
                    tuple(sorted(graph["rf"].items())),
                    tuple(
                        (obj, tuple(order))
                        for obj, order in sorted(graph["co"].items())
                    ),
                    tuple(sorted(graph["outcomes"].items())),
                )
            )
        expected = explicit_sc_graphs(subject)

        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 4)

    def test_certificate_tampering_and_repeatability(self):
        subject = program(
            {
                "0": [
                    {"op": "symbol", "dst": "a"},
                    {"op": "symbol", "dst": "b"},
                    {"op": "halt"},
                ],
            }
        )
        first = explore_condpor_program(subject)
        second = explore_condpor_program(subject)
        self.assertEqual(first, second)

        tampered = json.loads(json.dumps(first))
        tampered["executions"][0]["canonical_unsigned_model"]["sym_t0_e0"] = 1
        self.assertFalse(verify_condpor_interpreter_certificate(tampered))

        resealed = copy.deepcopy(tampered)
        unsigned = {
            key: value for key, value in resealed.items() if key != "certificate_sha256"
        }
        resealed["certificate_sha256"] = (
            __import__("hashlib")
            .sha256(
                json.dumps(
                    unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode()
            )
            .hexdigest()
        )
        self.assertFalse(verify_condpor_interpreter_certificate(resealed))

    def test_explicit_bounds_prevent_false_completeness_claim(self):
        hidden_loop = program(
            {
                "0": [
                    {"op": "label", "name": "again"},
                    {"op": "jump", "target": "again"},
                ],
            }
        )
        certificate = explore_condpor_program(hidden_loop, max_internal_steps=5)

        self.assertEqual(certificate["status"], "truncated")
        self.assertFalse(certificate["bounded_exhaustive"])
        self.assertEqual(certificate["bound_reasons"], ["max_internal_steps"])
        self.assertFalse(
            certificate["claim_scope"]["bounded_completeness_when_status_complete"]
        )

        event_bound = explore_condpor_program(
            program(
                {
                    "0": [
                        {"op": "symbol", "dst": "a"},
                        {"op": "symbol", "dst": "b"},
                        {"op": "halt"},
                    ],
                }
            ),
            max_events=1,
        )
        self.assertEqual(event_bound["bound_reasons"], ["max_events"])

    def test_strict_program_validation_rejects_malformed_ir(self):
        malformed = program(
            {
                "0": [
                    {
                        "op": "branch",
                        "condition": True,
                        "then": "missing",
                        "else": 0,
                    },
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "unknown label"):
            validate_condpor_program(malformed)

        unknown_field = program(
            {
                "0": [{"op": "halt", "surprise": True}],
            }
        )
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            validate_condpor_program(unknown_field)

        wrong_width = program({"0": [{"op": "halt"}]}, width=65)
        with self.assertRaisesRegex(ValueError, "bit_width"):
            validate_condpor_program(wrong_width)

        undefined_local = program(
            {
                "0": [{"op": "set", "dst": "a", "value": "missing"}],
            }
        )
        with self.assertRaisesRegex(ValueError, "undefined local"):
            explore_condpor_program(undefined_local)

    def test_cli_explore_verify_and_truncated_exit_contract(self):
        import tempfile

        subject = program(
            {
                "0": [
                    {"op": "symbol", "dst": "a"},
                    {"op": "symbol", "dst": "b"},
                    {"op": "halt"},
                ],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "program.json"
            artifact = root / "certificate.json"
            source.write_text(json.dumps(subject), encoding="utf-8")
            explore = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_condpor.py"),
                    "explore",
                    str(source),
                    "--output",
                    str(artifact),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(explore.returncode, 0, explore.stderr)
            self.assertEqual(
                json.loads(explore.stdout), json.loads(artifact.read_text())
            )
            verify = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_condpor.py"),
                    "verify",
                    str(artifact),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertEqual(json.loads(verify.stdout), {"valid": True})

            truncated = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "util" / "symcc_condpor.py"),
                    "explore",
                    str(source),
                    "--max-events",
                    "1",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(truncated.returncode, 3, truncated.stderr)
            self.assertEqual(json.loads(truncated.stdout)["status"], "truncated")


if __name__ == "__main__":
    unittest.main()
