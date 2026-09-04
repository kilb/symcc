#!/usr/bin/env python3
"""Verify bounded post-update loop-break exit manifests."""

import argparse
import itertools
import json
import sys
from pathlib import Path


SCHEMA = "symcc-ifss-loop-exit-manifest-v1"
EXIT_SEMANTICS = "post-update-equality-break-v1"
MULTI_EXIT_SEMANTICS = "post-update-priority-equality-break-v2"
MULTI_SUMMARY_SCHEMA = "bounded-multi-break-loop-exit-v2"
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
    require(number <= maximum, f"{field} exceeds its bound")
    return number


def bounded_integer(value, minimum, maximum, field):
    require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{field} must be an integer",
    )
    require(minimum <= value <= maximum, f"{field} is out of range")
    return value


def identity(value, field):
    require(
        isinstance(value, str)
        and value
        and len(value.encode("utf-8")) <= 4096,
        f"{field} is invalid",
    )
    return value


def mix_byte(value, byte):
    return ((value ^ byte) * FNV_PRIME) & UINT64_MASK


def mix_text(value, text):
    for byte in text.encode("utf-8"):
        value = mix_byte(value, byte)
    return mix_byte(value, 0xFF)


def mix_integer(value, number):
    for shift in range(0, 64, 8):
        value = mix_byte(value, (number >> shift) & 0xFF)
    return value


def verify_single_record(record):
    require(isinstance(record, dict), "record must be an object")
    require(record.get("schema") == SCHEMA, "unknown schema")
    require(
        record.get("exit_semantics") == EXIT_SEMANTICS,
        "unknown exit semantics",
    )
    module = identity(record.get("module"), "module")
    function = identity(record.get("function"), "function")
    site_fields = (
        "header_site",
        "header_branch_site",
        "break_condition_site",
        "normal_exit_site",
        "break_exit_site",
    )
    sites = [
        canonical_unsigned(record.get(field), UINT64_MASK, field)
        for field in site_fields
    ]
    require(sites[3] != sites[4], "normal and break exits coincide")
    maximum_trip_count = bounded_integer(
        record.get("maximum_trip_count"),
        0,
        8,
        "maximum_trip_count",
    )
    state_count = bounded_integer(
        record.get("state_count"), 1, 8, "state_count"
    )
    trip_identity = identity(record.get("trip_identity"), "trip_identity")
    break_identity = identity(
        record.get("break_value_identity"), "break_value_identity"
    )
    induction_phi = canonical_unsigned(
        record.get("induction_phi_site"),
        UINT64_MASK,
        "induction_phi_site",
    )
    induction_update = canonical_unsigned(
        record.get("induction_update_site"),
        UINT64_MASK,
        "induction_update_site",
    )
    require(
        induction_phi != induction_update,
        "induction sites are not distinct",
    )
    recurrence_kind = record.get("recurrence_kind")
    require(
        recurrence_kind in ("independent-add-v1", "upper-triangular-v1"),
        "unknown recurrence kind",
    )
    recurrence_fingerprint_text = record.get("recurrence_fingerprint")
    if recurrence_kind == "upper-triangular-v1":
        require(state_count <= 4, "triangular state count exceeds bound")
        recurrence_fingerprint = canonical_unsigned(
            recurrence_fingerprint_text,
            UINT64_MASK,
            "recurrence_fingerprint",
        )
    else:
        require(
            recurrence_fingerprint_text == "",
            "independent recurrence has a matrix fingerprint",
        )
        recurrence_fingerprint = None

    states = record.get("states")
    require(
        isinstance(states, list) and len(states) == state_count,
        "state count mismatch",
    )
    parsed_states = []
    phi_sites = set()
    update_sites = set()
    steps = []
    for ordinal, state in enumerate(states):
        require(isinstance(state, dict), "state must be an object")
        require(state.get("ordinal") == ordinal, "state ordinal mismatch")
        phi_site = canonical_unsigned(
            state.get("phi_site"), UINT64_MASK, "phi_site"
        )
        update_site = canonical_unsigned(
            state.get("update_site"), UINT64_MASK, "update_site"
        )
        require(phi_site not in phi_sites, "duplicate state PHI site")
        require(update_site not in update_sites, "duplicate update site")
        phi_sites.add(phi_site)
        update_sites.add(update_site)
        bits = bounded_integer(state.get("bits"), 1, 4096, "state bits")
        initial = identity(
            state.get("initial_identity"), "initial_identity"
        )
        require(
            state.get("normal_liveout_site") == state.get("phi_site"),
            "normal live-out does not name the state PHI",
        )
        require(
            state.get("break_liveout_site") == state.get("update_site"),
            "break live-out does not name the update",
        )
        if recurrence_kind == "independent-add-v1":
            step = canonical_unsigned(
                state.get("step"), (1 << bits) - 1, "state step"
            )
            steps.append(state.get("step"))
        else:
            require(state.get("step") == "", "triangular state has a step")
            step = None
        parsed_states.append(
            (phi_site, update_site, bits, initial, step)
        )

    table = record.get("execution_table")
    side = maximum_trip_count + 1
    require(
        isinstance(table, list) and len(table) == side * side,
        "execution table size mismatch",
    )
    parsed_table = []
    cursor = 0
    for trip in range(side):
        for break_at in range(side):
            item = table[cursor]
            cursor += 1
            require(isinstance(item, dict), "table row must be an object")
            require(item.get("trip") == trip, "table trip mismatch")
            require(
                item.get("break_at") == break_at,
                "table break_at mismatch",
            )
            taken = break_at < trip
            require(
                item.get("break_taken") is taken,
                "table break decision mismatch",
            )
            require(
                item.get("executions")
                == (break_at + 1 if taken else trip),
                "table execution count mismatch",
            )
            parsed_table.append((trip, break_at, taken, item["executions"]))

    fingerprint = FNV_OFFSET
    fingerprint = mix_text(fingerprint, SCHEMA)
    fingerprint = mix_text(fingerprint, module)
    fingerprint = mix_text(fingerprint, function)
    for site in sites:
        fingerprint = mix_integer(fingerprint, site)
    fingerprint = mix_integer(fingerprint, maximum_trip_count)
    fingerprint = mix_integer(fingerprint, state_count)
    fingerprint = mix_text(fingerprint, trip_identity)
    fingerprint = mix_text(fingerprint, break_identity)
    fingerprint = mix_integer(fingerprint, induction_phi)
    fingerprint = mix_integer(fingerprint, induction_update)
    fingerprint = mix_text(fingerprint, recurrence_kind)
    if recurrence_fingerprint is not None:
        fingerprint = mix_integer(fingerprint, recurrence_fingerprint)
    else:
        for step in steps:
            fingerprint = mix_text(fingerprint, step)
    for phi_site, update_site, bits, initial, _ in parsed_states:
        fingerprint = mix_integer(fingerprint, phi_site)
        fingerprint = mix_integer(fingerprint, update_site)
        fingerprint = mix_integer(fingerprint, bits)
        fingerprint = mix_text(fingerprint, initial)
    for trip, break_at, taken, executions in parsed_table:
        fingerprint = mix_integer(fingerprint, trip)
        fingerprint = mix_integer(fingerprint, break_at)
        fingerprint = mix_integer(fingerprint, int(taken))
        fingerprint = mix_integer(fingerprint, executions)
    claimed = canonical_unsigned(
        record.get("proof_fingerprint"),
        UINT64_MASK,
        "proof_fingerprint",
    )
    require(claimed == fingerprint, "proof fingerprint mismatch")

def verify_multi_record(record):
    require(isinstance(record, dict), "record must be an object")
    require(record.get("schema") == SCHEMA, "unknown schema")
    require(
        record.get("exit_semantics") == MULTI_EXIT_SEMANTICS,
        "unknown multi-break exit semantics",
    )
    module = identity(record.get("module"), "module")
    function = identity(record.get("function"), "function")
    header_site = canonical_unsigned(
        record.get("header_site"), UINT64_MASK, "header_site"
    )
    header_branch_site = canonical_unsigned(
        record.get("header_branch_site"),
        UINT64_MASK,
        "header_branch_site",
    )
    normal_exit_site = canonical_unsigned(
        record.get("normal_exit_site"),
        UINT64_MASK,
        "normal_exit_site",
    )
    maximum_trip_count = bounded_integer(
        record.get("maximum_trip_count"),
        0,
        8,
        "maximum_trip_count",
    )
    state_count = bounded_integer(
        record.get("state_count"), 1, 8, "state_count"
    )
    break_count = bounded_integer(
        record.get("break_count"), 2, 3, "break_count"
    )
    breaks = record.get("breaks")
    require(
        isinstance(breaks, list) and len(breaks) == break_count,
        "break count mismatch",
    )
    parsed_breaks = []
    condition_sites = set()
    exit_sites = {normal_exit_site}
    for ordinal, loop_break in enumerate(breaks):
        require(isinstance(loop_break, dict), "break must be an object")
        require(
            loop_break.get("ordinal") == ordinal,
            "break ordinal mismatch",
        )
        condition_site = canonical_unsigned(
            loop_break.get("condition_site"),
            UINT64_MASK,
            "condition_site",
        )
        exit_site = canonical_unsigned(
            loop_break.get("exit_site"), UINT64_MASK, "exit_site"
        )
        require(
            condition_site not in condition_sites,
            "duplicate break condition site",
        )
        require(exit_site not in exit_sites, "duplicate break exit site")
        condition_sites.add(condition_site)
        exit_sites.add(exit_site)
        value_identity = identity(
            loop_break.get("value_identity"), "value_identity"
        )
        parsed_breaks.append(
            (ordinal, condition_site, exit_site, value_identity)
        )

    trip_identity = identity(record.get("trip_identity"), "trip_identity")
    induction_phi = canonical_unsigned(
        record.get("induction_phi_site"),
        UINT64_MASK,
        "induction_phi_site",
    )
    induction_update = canonical_unsigned(
        record.get("induction_update_site"),
        UINT64_MASK,
        "induction_update_site",
    )
    require(
        induction_phi != induction_update,
        "induction sites are not distinct",
    )
    recurrence_kind = record.get("recurrence_kind")
    require(
        recurrence_kind in ("independent-add-v1", "upper-triangular-v1"),
        "unknown recurrence kind",
    )
    recurrence_fingerprint_text = record.get("recurrence_fingerprint")
    if recurrence_kind == "upper-triangular-v1":
        require(state_count <= 4, "triangular state count exceeds bound")
        recurrence_fingerprint = canonical_unsigned(
            recurrence_fingerprint_text,
            UINT64_MASK,
            "recurrence_fingerprint",
        )
    else:
        require(
            recurrence_fingerprint_text == "",
            "independent recurrence has a matrix fingerprint",
        )
        recurrence_fingerprint = None

    states = record.get("states")
    require(
        isinstance(states, list) and len(states) == state_count,
        "state count mismatch",
    )
    parsed_states = []
    phi_sites = set()
    update_sites = set()
    steps = []
    for ordinal, state in enumerate(states):
        require(isinstance(state, dict), "state must be an object")
        require(state.get("ordinal") == ordinal, "state ordinal mismatch")
        phi_site = canonical_unsigned(
            state.get("phi_site"), UINT64_MASK, "phi_site"
        )
        update_site = canonical_unsigned(
            state.get("update_site"), UINT64_MASK, "update_site"
        )
        require(phi_site not in phi_sites, "duplicate state PHI site")
        require(update_site not in update_sites, "duplicate update site")
        phi_sites.add(phi_site)
        update_sites.add(update_site)
        bits = bounded_integer(state.get("bits"), 1, 4096, "state bits")
        initial = identity(
            state.get("initial_identity"), "initial_identity"
        )
        require(
            state.get("normal_liveout_site") == state.get("phi_site"),
            "normal live-out does not name the state PHI",
        )
        break_liveouts = state.get("break_liveout_sites")
        require(
            isinstance(break_liveouts, list)
            and len(break_liveouts) == break_count
            and all(
                value == state.get("update_site")
                for value in break_liveouts
            ),
            "break live-outs do not name the update",
        )
        if recurrence_kind == "independent-add-v1":
            step = canonical_unsigned(
                state.get("step"), (1 << bits) - 1, "state step"
            )
            steps.append(state.get("step"))
        else:
            require(state.get("step") == "", "triangular state has a step")
            step = None
        parsed_states.append(
            (phi_site, update_site, bits, initial, step)
        )

    table = record.get("execution_table")
    side = maximum_trip_count + 1
    expected_rows = side ** (break_count + 1)
    require(
        isinstance(table, list) and len(table) == expected_rows,
        "multi-break execution table size mismatch",
    )
    parsed_table = []
    cursor = 0
    domain = range(side)
    for trip in domain:
        for break_values in itertools.product(domain, repeat=break_count):
            item = table[cursor]
            cursor += 1
            require(isinstance(item, dict), "table row must be an object")
            require(item.get("trip") == trip, "table trip mismatch")
            require(
                item.get("break_at") == list(break_values),
                "table break vector mismatch",
            )
            eligible = [
                (value, ordinal)
                for ordinal, value in enumerate(break_values)
                if value < trip
            ]
            winner = min(eligible)[1] if eligible else -1
            executions = min(eligible)[0] + 1 if eligible else trip
            require(
                item.get("break_taken") is (winner >= 0),
                "table break decision mismatch",
            )
            require(item.get("winner") == winner, "table winner mismatch")
            require(
                item.get("executions") == executions,
                "table execution count mismatch",
            )
            parsed_table.append(
                (trip, break_values, winner, executions)
            )

    fingerprint = FNV_OFFSET
    fingerprint = mix_text(fingerprint, SCHEMA)
    fingerprint = mix_text(fingerprint, MULTI_SUMMARY_SCHEMA)
    fingerprint = mix_text(fingerprint, module)
    fingerprint = mix_text(fingerprint, function)
    fingerprint = mix_integer(fingerprint, header_site)
    fingerprint = mix_integer(fingerprint, header_branch_site)
    fingerprint = mix_integer(fingerprint, normal_exit_site)
    fingerprint = mix_integer(fingerprint, maximum_trip_count)
    fingerprint = mix_integer(fingerprint, state_count)
    fingerprint = mix_integer(fingerprint, break_count)
    for ordinal, condition_site, exit_site, value_identity in parsed_breaks:
        fingerprint = mix_integer(fingerprint, ordinal)
        fingerprint = mix_integer(fingerprint, condition_site)
        fingerprint = mix_integer(fingerprint, exit_site)
        fingerprint = mix_text(fingerprint, value_identity)
    fingerprint = mix_text(fingerprint, trip_identity)
    fingerprint = mix_integer(fingerprint, induction_phi)
    fingerprint = mix_integer(fingerprint, induction_update)
    fingerprint = mix_text(fingerprint, recurrence_kind)
    if recurrence_fingerprint is not None:
        fingerprint = mix_integer(fingerprint, recurrence_fingerprint)
    else:
        for step in steps:
            fingerprint = mix_text(fingerprint, step)
    for phi_site, update_site, bits, initial, _ in parsed_states:
        fingerprint = mix_integer(fingerprint, phi_site)
        fingerprint = mix_integer(fingerprint, update_site)
        fingerprint = mix_integer(fingerprint, bits)
        fingerprint = mix_text(fingerprint, initial)
    for trip, break_values, winner, executions in parsed_table:
        fingerprint = mix_integer(fingerprint, trip)
        for value in break_values:
            fingerprint = mix_integer(fingerprint, value)
        fingerprint = mix_integer(fingerprint, int(winner >= 0))
        fingerprint = mix_integer(fingerprint, winner + 1)
        fingerprint = mix_integer(fingerprint, executions)
    claimed = canonical_unsigned(
        record.get("proof_fingerprint"),
        UINT64_MASK,
        "proof_fingerprint",
    )
    require(claimed == fingerprint, "proof fingerprint mismatch")


def verify_record(record):
    require(isinstance(record, dict), "record must be an object")
    semantics = record.get("exit_semantics")
    if semantics == EXIT_SEMANTICS:
        return verify_single_record(record)
    if semantics == MULTI_EXIT_SEMANTICS:
        return verify_multi_record(record)
    raise VerificationError("unknown exit semantics")


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
    print(f"verified {count} IFSS loop exit record(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
