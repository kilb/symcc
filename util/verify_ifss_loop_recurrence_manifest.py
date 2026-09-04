#!/usr/bin/env python3
"""Verify bounded upper-triangular IFSS loop recurrence manifests."""

import argparse
import json
import sys
from pathlib import Path


MANIFEST_SCHEMA = "symcc-ifss-loop-recurrence-manifest-v1"
RECURRENCE_SCHEMA = "bounded-upper-triangular-loop-summary-v1"
FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211
UINT64_MASK = (1 << 64) - 1


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def canonical_unsigned(value, maximum, field):
    require(isinstance(value, str), f"{field} must be a string")
    require(
        value == "0" or (value and value[0] != "0" and value.isdigit()),
        f"{field} is not canonical unsigned decimal",
    )
    number = int(value)
    require(number <= maximum, f"{field} exceeds its bit width")
    return number


def bounded_integer(value, minimum, maximum, field):
    require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{field} must be an integer",
    )
    require(minimum <= value <= maximum, f"{field} is out of range")
    return value


def mix_byte(value, byte):
    return ((value ^ byte) * FNV_PRIME) & UINT64_MASK


def mix_text(value, text):
    require(isinstance(text, str), "fingerprinted text must be a string")
    for byte in text.encode("utf-8"):
        value = mix_byte(value, byte)
    return mix_byte(value, 0xFF)


def mix_integer(value, number):
    for shift in range(0, 64, 8):
        value = mix_byte(value, (number >> shift) & 0xFF)
    return value


def parse_matrix(value, count, maximum, field):
    require(
        isinstance(value, list) and len(value) == count,
        f"{field} row count mismatch",
    )
    result = []
    for row_index, row in enumerate(value):
        require(
            isinstance(row, list) and len(row) == count,
            f"{field}[{row_index}] column count mismatch",
        )
        result.append(
            [
                canonical_unsigned(
                    item,
                    maximum,
                    f"{field}[{row_index}][{column}]",
                )
                for column, item in enumerate(row)
            ]
        )
    return result


def multiply(left, right, modulus):
    count = len(left)
    return [
        [
            sum(
                left[row][middle] * right[middle][column]
                for middle in range(count)
            )
            % modulus
            for column in range(count)
        ]
        for row in range(count)
    ]


def advance_offset(matrix, current, offset, modulus):
    return [
        (
            offset[row]
            + sum(
                matrix[row][column] * current[column]
                for column in range(len(matrix))
            )
        )
        % modulus
        for row in range(len(matrix))
    ]


def verify_record(record):
    require(isinstance(record, dict), "record must be an object")
    require(record.get("schema") == MANIFEST_SCHEMA, "unknown schema")
    require(
        record.get("recurrence_schema") == RECURRENCE_SCHEMA,
        "unknown recurrence schema",
    )
    module = record.get("module")
    function = record.get("function")
    require(isinstance(module, str) and module, "module must be non-empty")
    require(
        isinstance(function, str) and function,
        "function must be non-empty",
    )
    header_site = canonical_unsigned(
        record.get("header_site"), UINT64_MASK, "header_site"
    )
    branch_site = canonical_unsigned(
        record.get("branch_site"), UINT64_MASK, "branch_site"
    )
    maximum_trip_count = bounded_integer(
        record.get("maximum_trip_count"),
        0,
        8,
        "maximum_trip_count",
    )
    state_count = bounded_integer(
        record.get("state_count"), 2, 4, "state_count"
    )
    state_bits = bounded_integer(
        record.get("state_bits"), 1, 4096, "state_bits"
    )
    modulus = 1 << state_bits
    maximum_value = modulus - 1

    states = record.get("states")
    require(
        isinstance(states, list) and len(states) == state_count,
        "state count mismatch",
    )
    matrix = []
    offset = []
    state_fields = []
    phi_sites = set()
    update_sites = set()
    has_cross_state = False
    for ordinal, state in enumerate(states):
        require(isinstance(state, dict), "state must be an object")
        require(state.get("ordinal") == ordinal, "state ordinal mismatch")
        phi_site = canonical_unsigned(
            state.get("phi_site"), UINT64_MASK, "phi_site"
        )
        update_site = canonical_unsigned(
            state.get("update_site"), UINT64_MASK, "update_site"
        )
        require(phi_site not in phi_sites, "duplicate phi site")
        require(update_site not in update_sites, "duplicate update site")
        phi_sites.add(phi_site)
        update_sites.add(update_site)
        initial_identity = state.get("initial_identity")
        require(
            isinstance(initial_identity, str)
            and initial_identity
            and len(initial_identity.encode("utf-8")) <= 4096,
            "invalid initial identity",
        )
        coefficients = state.get("coefficients")
        require(
            isinstance(coefficients, list)
            and len(coefficients) == state_count,
            "coefficient count mismatch",
        )
        row = [
            canonical_unsigned(
                item, maximum_value, f"state[{ordinal}].coefficient"
            )
            for item in coefficients
        ]
        require(row[ordinal] == 1, "diagonal coefficient is not one")
        require(
            all(row[column] == 0 for column in range(ordinal)),
            "matrix is not upper triangular",
        )
        has_cross_state |= any(
            row[column] != 0
            for column in range(ordinal + 1, state_count)
        )
        state_offset = canonical_unsigned(
            state.get("offset"), maximum_value, "state offset"
        )
        matrix.append(row)
        offset.append(state_offset)
        state_fields.append(
            (
                phi_site,
                update_site,
                initial_identity,
                coefficients,
                state.get("offset"),
            )
        )
    require(has_cross_state, "recurrence has no cross-state term")

    powers = record.get("powers")
    require(
        isinstance(powers, list)
        and len(powers) == maximum_trip_count + 1,
        "power count mismatch",
    )
    current_matrix = [
        [int(row == column) for column in range(state_count)]
        for row in range(state_count)
    ]
    current_offset = [0] * state_count
    for trip, power in enumerate(powers):
        require(isinstance(power, dict), "power must be an object")
        require(power.get("trip") == trip, "power trip mismatch")
        actual_matrix = parse_matrix(
            power.get("matrix"),
            state_count,
            maximum_value,
            f"power[{trip}].matrix",
        )
        actual_offset_values = power.get("offset")
        require(
            isinstance(actual_offset_values, list)
            and len(actual_offset_values) == state_count,
            "power offset count mismatch",
        )
        actual_offset = [
            canonical_unsigned(
                item, maximum_value, f"power[{trip}].offset"
            )
            for item in actual_offset_values
        ]
        require(
            actual_matrix == current_matrix,
            f"power[{trip}] matrix mismatch",
        )
        require(
            actual_offset == current_offset,
            f"power[{trip}] offset mismatch",
        )
        current_offset = advance_offset(
            matrix, current_offset, offset, modulus
        )
        current_matrix = multiply(matrix, current_matrix, modulus)

    fingerprint = FNV_OFFSET
    fingerprint = mix_text(fingerprint, RECURRENCE_SCHEMA)
    fingerprint = mix_text(fingerprint, module)
    fingerprint = mix_text(fingerprint, function)
    fingerprint = mix_integer(fingerprint, header_site)
    fingerprint = mix_integer(fingerprint, branch_site)
    fingerprint = mix_integer(fingerprint, maximum_trip_count)
    fingerprint = mix_integer(fingerprint, state_bits)
    fingerprint = mix_integer(fingerprint, state_count)
    for phi_site, update_site, initial, coefficients, state_offset in (
        state_fields
    ):
        fingerprint = mix_integer(fingerprint, phi_site)
        fingerprint = mix_integer(fingerprint, update_site)
        fingerprint = mix_text(fingerprint, initial)
        for coefficient in coefficients:
            fingerprint = mix_text(fingerprint, coefficient)
        fingerprint = mix_text(fingerprint, state_offset)
    claimed = canonical_unsigned(
        record.get("proof_fingerprint"),
        UINT64_MASK,
        "proof_fingerprint",
    )
    require(claimed == fingerprint, "proof fingerprint mismatch")


def verify_path(path):
    count = 0
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                verify_record(json.loads(line))
            except (json.JSONDecodeError, VerificationError) as error:
                raise VerificationError(
                    f"{path}:{line_number}: {error}"
                ) from error
            count += 1
    require(count > 0, "manifest contains no records")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    arguments = parser.parse_args(argv)
    try:
        count = verify_path(arguments.manifest)
    except (OSError, UnicodeError, VerificationError) as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified {count} IFSS loop recurrence record(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
