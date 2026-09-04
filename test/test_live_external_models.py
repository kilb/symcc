# RUN: python3 %s

import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "util"))

from distributed_state import LiveStateStore  # noqa: E402
from live_continuation import LiveContinuationExecutor  # noqa: E402


CAPABILITY = "declarative-pure-external-summary"


def external_instruction(
    model,
    arguments,
    widths,
    bits,
    *,
    site="1",
    destination="result",
    normal=None,
):
    instruction = {
        "op": "external_pure",
        "function": "modeled",
        "model": model,
        "args": list(arguments),
        "arg_bits": list(widths),
        "dst": destination,
        "bits": bits,
        "site": site,
    }
    if normal is not None:
        instruction["normal"] = normal
    return instruction


def program_for(instruction, *, extra_blocks=None, capability=True):
    lowering = {"capabilities": [CAPABILITY]} if capability else {}
    blocks = {
        "entry": [instruction, {"op": "halt", "value": {"var": "result"}}],
    }
    if extra_blocks:
        blocks.update(copy.deepcopy(extra_blocks))
    return {
        "schema": "symcc-live-program-v1",
        "entry": "main",
        "input_size": 0,
        "lowering": lowering,
        "functions": {
            "main": {"entry": "entry", "blocks": blocks},
        },
    }


class DeclarativePureExternalTests(unittest.TestCase):
    def run_value(self, instruction):
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            root = executor.create(program_for(instruction))
            result = executor.resume(root, max_steps=8)
            self.assertEqual(len(result["halted"]), 1)
            return result["halted"][0]["value"]

    def test_all_model_families_execute_as_bounded_bitvectors(self):
        cases = [
            ("pure-v1:constant:255", [], [], 8, 255),
            ("pure-v1:identity:0", [{"const": 7, "bits": 8}], [8], 8, 7),
            ("pure-v1:unary:neg:0", [{"const": 2, "bits": 8}], [8], 8, 254),
            ("pure-v1:unary:bitnot:0", [{"const": 15, "bits": 8}], [8], 8, 240),
            (
                "pure-v1:binary:add:0:1",
                [{"const": 250, "bits": 8}, {"const": 10, "bits": 8}],
                [8, 8],
                8,
                4,
            ),
            (
                "pure-v1:binary:slt:0:1",
                [{"const": 255, "bits": 8}, {"const": 1, "bits": 8}],
                [8, 8],
                1,
                1,
            ),
            (
                "pure-v1:select:0:1:2",
                [
                    {"const": 0, "bits": 1},
                    {"const": 11, "bits": 8},
                    {"const": 22, "bits": 8},
                ],
                [1, 8, 8],
                8,
                22,
            ),
        ]
        for index, (model, arguments, widths, bits, expected) in enumerate(cases):
            with self.subTest(model=model):
                instruction = external_instruction(
                    model, arguments, widths, bits, site=str(index + 1)
                )
                self.assertEqual(self.run_value(instruction), expected)

    def test_symbolic_model_participates_in_feasibility_forking(self):
        external = external_instruction(
            "pure-v1:binary:eq:0:1",
            [{"var": "byte"}, {"const": 7, "bits": 8}],
            [8, 8],
            1,
        )
        program = program_for(external)
        program["input_size"] = 1
        program["functions"]["main"]["blocks"] = {
            "entry": [
                {"op": "input", "dst": "byte", "offset": 0},
                external,
                {
                    "op": "branch",
                    "condition": {"var": "result"},
                    "true": "yes",
                    "false": "no",
                    "site": "2",
                },
            ],
            "yes": [{"op": "halt", "value": 1}],
            "no": [{"op": "halt", "value": 2}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            root = executor.create(program, input_bytes=b"\x07")
            result = executor.resume(root, max_steps=16, max_states=4)
            self.assertEqual(result["forks"], 1)
            self.assertEqual(
                sorted(item["value"] for item in result["halted"]), [1, 2]
            )

    def test_nounwind_invoke_model_routes_only_to_normal_target(self):
        instruction = external_instruction(
            "pure-v1:binary:add:0:1",
            [{"const": 2, "bits": 32}, {"const": 3, "bits": 32}],
            [32, 32],
            32,
            normal="normal",
        )
        program = program_for(
            instruction,
            extra_blocks={
                "normal": [{"op": "halt", "value": {"var": "result"}}],
                "unwind": [{"op": "halt", "value": 99}],
            },
        )
        program["functions"]["main"]["blocks"]["entry"] = [instruction]
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            result = executor.resume(executor.create(program), max_steps=4)
            self.assertEqual(result["halted"][0]["value"], 5)

    def test_modeled_expression_survives_checkpoint_resume(self):
        instruction = external_instruction(
            "pure-v1:binary:xor:0:1",
            [{"const": 170, "bits": 8}, {"const": 15, "bits": 8}],
            [8, 8],
            8,
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = LiveStateStore(temporary)
            with LiveContinuationExecutor(store) as first_executor:
                paused = first_executor.resume(
                    first_executor.create(program_for(instruction)),
                    max_steps=1,
                )
            self.assertEqual(len(paused["frontier"]), 1)
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json,sys;"
                        "from distributed_state import LiveStateStore;"
                        "from live_continuation import LiveContinuationExecutor;"
                        "store=LiveStateStore(sys.argv[1]);"
                        "executor=LiveContinuationExecutor(store);"
                        "result=executor.resume(sys.argv[2],max_steps=2);"
                        "executor.close();"
                        "print(json.dumps(result['halted'][0]['value']))"
                    ),
                    temporary,
                    paused["frontier"][0],
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PYTHONPATH": str(ROOT / "util"),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(child.stdout.strip(), "165")

    def test_capability_closure_is_bidirectional(self):
        instruction = external_instruction(
            "pure-v1:constant:1", [], [], 8
        )
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            with self.assertRaisesRegex(ValueError, "capability is missing"):
                executor.create(program_for(instruction, capability=False))
            unused = program_for({"op": "halt", "value": 0})
            with self.assertRaisesRegex(ValueError, "has no contract"):
                executor.create(unused)

    def test_invalid_models_sites_and_shapes_fail_closed(self):
        base = external_instruction(
            "pure-v1:identity:0", [{"const": 1, "bits": 8}], [8], 8
        )
        mutations = [
            ("model", "pure-v1:identity:00", "model is invalid"),
            ("model", "pure-v1:binary:udiv:0:0", "model is invalid"),
            ("model", "pure-v1:identity:1", "model is invalid"),
            ("site", 1, "site is invalid"),
            ("site", "0", "site is invalid"),
            ("bits", True, "result width is invalid"),
            ("bits", 8.0, "result width is invalid"),
            ("arg_bits", [True], "width is invalid"),
            ("arg_bits", [8.0], "width is invalid"),
            ("function", "bad/name", "instruction is invalid"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            for field, value, message in mutations:
                instruction = copy.deepcopy(base)
                instruction[field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                    ValueError, message
                ):
                    executor.create(program_for(instruction))
            instruction = copy.deepcopy(base)
            instruction["unknown"] = 1
            with self.assertRaisesRegex(ValueError, "instruction is invalid"):
                executor.create(program_for(instruction))

            ambiguous_operands = [
                {"var": "value", "const": 1},
                {"var": 1},
                {"var": "bad/name"},
                {"const": 1, "bits": 8, "extra": 0},
                {"const": 1.0, "bits": 8},
                {"const": 1, "bits": 16},
                True,
            ]
            for operand in ambiguous_operands:
                instruction = copy.deepcopy(base)
                instruction["args"] = [operand]
                with self.subTest(operand=operand), self.assertRaisesRegex(
                    ValueError, "operand is invalid"
                ):
                    executor.create(program_for(instruction))

            invoke = copy.deepcopy(base)
            invoke["normal"] = "normal"
            program = program_for(
                invoke,
                extra_blocks={"normal": [{"op": "halt", "value": 0}]},
            )
            with self.assertRaisesRegex(ValueError, "normal target"):
                executor.create(program)

    def test_abi_and_runtime_operand_widths_are_checked(self):
        invalid_abi = external_instruction(
            "pure-v1:identity:0", [{"const": 1, "bits": 8}], [8], 16
        )
        forged_runtime_width = external_instruction(
            "pure-v1:identity:0", [{"var": "value"}], [16], 16
        )
        forged_program = program_for(forged_runtime_width)
        forged_program["functions"]["main"]["blocks"]["entry"].insert(
            0, {"op": "const", "dst": "value", "value": 1, "bits": 8}
        )
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            with self.assertRaisesRegex(ValueError, "does not match its integer ABI"):
                executor.create(program_for(invalid_abi))
            root = executor.create(forged_program)
            with self.assertRaisesRegex(ValueError, "operand width mismatch"):
                executor.resume(root, max_steps=4)

    def test_duplicate_stable_site_is_rejected(self):
        first = external_instruction("pure-v1:constant:1", [], [], 8)
        second = external_instruction(
            "pure-v1:constant:2", [], [], 8, destination="second"
        )
        program = program_for(first)
        program["functions"]["main"]["blocks"]["entry"] = [
            first,
            second,
            {"op": "halt", "value": 0},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            executor = LiveContinuationExecutor(LiveStateStore(temporary))
            with self.assertRaisesRegex(ValueError, "site is invalid"):
                executor.create(program)


if __name__ == "__main__":
    unittest.main()
