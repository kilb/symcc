#!/usr/bin/env python3
"""Verify proof-carrying IFSS switch-tree JSONL manifests."""

import argparse
import json
import sys
from pathlib import Path


SCHEMA = "symcc-ifss-switch-tree-v1"
MASK64 = (1 << 64) - 1
FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211
MAX64 = MASK64


class VerificationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def unsigned_text(value, name, maximum=MAX64):
    require(isinstance(value, str) and value, f"{name} is not text")
    require(value.isascii() and value.isdigit(), f"{name} is not decimal")
    require(value == str(int(value)), f"{name} is not canonical")
    parsed = int(value)
    require(parsed >= 0, f"{name} is negative")
    if maximum is not None:
        require(parsed <= maximum, f"{name} is outside uint64")
    return parsed


def integer(value, name, minimum=0):
    require(isinstance(value, int) and not isinstance(value, bool),
            f"{name} is not an integer")
    require(value >= minimum, f"{name} is below its bound")
    return value


def mix_byte(hash_value, byte):
    return ((hash_value ^ byte) * FNV_PRIME) & MASK64


def mix_text(hash_value, value):
    for byte in value.encode("utf-8"):
        hash_value = mix_byte(hash_value, byte)
    return mix_byte(hash_value, 0xFF)


def mix_integer(hash_value, value):
    require(0 <= value <= MAX64, "fingerprint integer is outside uint64")
    for shift in range(0, 64, 8):
        hash_value = mix_byte(hash_value, (value >> shift) & 0xFF)
    return hash_value


def saturating_add(*values):
    result = 0
    for value in values:
        if result > MAX64 - value:
            return MAX64
        result += value
    return result


def optimal_splits(weights):
    count = len(weights)
    costs = [[0] * (count + 1) for _ in range(count)]
    splits = [[0] * (count + 1) for _ in range(count)]
    for length in range(2, count + 1):
        for begin in range(0, count - length + 1):
            end = begin + length
            interval_weight = 0
            for index in range(begin, end):
                interval_weight = saturating_add(
                    interval_weight, weights[index])
            best = MAX64
            best_split = begin + 1
            for middle in range(begin + 1, end):
                candidate = saturating_add(
                    interval_weight,
                    costs[begin][middle],
                    costs[middle][end])
                if candidate < best:
                    best = candidate
                    best_split = middle
            costs[begin][end] = best
            splits[begin][end] = best_split
    total = 0
    for weight in weights:
        total = saturating_add(total, weight)
    return splits, saturating_add(costs[0][count], total)


def verify_record(record):
    require(isinstance(record, dict), "record is not an object")
    require(record.get("schema") == SCHEMA, "unknown schema")
    site = unsigned_text(record.get("site"), "site")
    requested = record.get("requested_mode")
    effective = record.get("effective_mode")
    require(requested in {"linear", "balanced", "profile"},
            "invalid requested mode")
    profile_valid = record.get("profile_valid")
    require(isinstance(profile_valid, bool), "profile_valid is not boolean")
    expected_effective = (
        "profile" if requested == "profile" and profile_valid
        else "balanced" if requested == "profile"
        else requested)
    require(effective == expected_effective, "effective mode mismatch")

    cases = record.get("cases")
    destinations = record.get("destinations")
    nodes = record.get("nodes")
    require(isinstance(cases, list) and cases, "cases are missing")
    require(isinstance(destinations, list) and len(destinations) >= 2,
            "destinations are missing")
    require(isinstance(nodes, list), "nodes are missing")
    case_count = len(cases)
    logical_edges = integer(
        record.get("logical_edges"), "logical_edges", 2)
    unique_destinations = integer(
        record.get("unique_destinations"), "unique_destinations", 2)
    lowered_edges = integer(
        record.get("lowered_edges"), "lowered_edges", 2)
    require(logical_edges == case_count + 1, "logical edge count mismatch")
    require(unique_destinations == len(destinations),
            "unique destination count mismatch")
    require(record.get("shared_destinations") ==
            (logical_edges != unique_destinations),
            "shared destination flag mismatch")

    case_by_ordinal = {}
    seen_values = set()
    for expected_ordinal, case in enumerate(cases):
        require(isinstance(case, dict), "case is not an object")
        ordinal = integer(case.get("ordinal"), "case ordinal")
        require(ordinal == expected_ordinal, "case ordinal is not canonical")
        value = unsigned_text(
            case.get("value"), "case value", maximum=None)
        require(value not in seen_values, "duplicate case value")
        seen_values.add(value)
        destination = integer(
            case.get("destination"), "case destination")
        require(destination < unique_destinations,
                "case destination is outside the table")
        case_by_ordinal[ordinal] = {
            "ordinal": ordinal,
            "text": case["value"],
            "value": value,
            "destination": destination,
            "weight_text": case.get("weight"),
        }

    default_destinations = []
    original_total = 0
    lowered_total = 0
    for expected_ordinal, destination in enumerate(destinations):
        require(isinstance(destination, dict),
                "destination is not an object")
        ordinal = integer(
            destination.get("ordinal"), "destination ordinal")
        require(ordinal == expected_ordinal,
                "destination ordinal is not canonical")
        original = integer(
            destination.get("original_multiplicity"),
            "original multiplicity", 1)
        lowered = integer(
            destination.get("lowered_multiplicity"),
            "lowered multiplicity", 1)
        original_total += original
        lowered_total += lowered
        is_default = destination.get("is_default")
        require(isinstance(is_default, bool),
                "default marker is not boolean")
        if is_default:
            default_destinations.append(ordinal)
    require(len(default_destinations) == 1,
            "manifest does not identify one default destination")
    default_destination = default_destinations[0]
    require(original_total == logical_edges,
            "original multiplicity total mismatch")
    require(lowered_total == lowered_edges,
            "lowered multiplicity total mismatch")

    expected_original = [0] * unique_destinations
    expected_original[default_destination] += 1
    for case in case_by_ordinal.values():
        expected_original[case["destination"]] += 1
    expected_lowered = [0] * unique_destinations
    for case in case_by_ordinal.values():
        expected_lowered[case["destination"]] += 1
    if effective == "linear":
        expected_lowered[default_destination] += 1
    else:
        expected_lowered[default_destination] += case_count
    for ordinal, destination in enumerate(destinations):
        require(destination["original_multiplicity"] ==
                expected_original[ordinal],
                "per-destination original multiplicity mismatch")
        require(destination["lowered_multiplicity"] ==
                expected_lowered[ordinal],
                "per-destination lowered multiplicity mismatch")

    sorted_cases = sorted(
        case_by_ordinal.values(), key=lambda item: item["value"])
    splits = None
    profile_fingerprint = None
    if profile_valid:
        source = record.get("profile_source")
        require(source in {"external-stable-site", "llvm-branch-weights"},
                "invalid profile source")
        weights = []
        for case in sorted_cases:
            weight = unsigned_text(case["weight_text"], "case weight")
            require(weight > 0, "case weight is zero")
            weights.append(weight)
        default_weight = unsigned_text(
            record.get("default_weight"), "default weight")
        require(default_weight > 0, "default weight is zero")
        splits, objective = optimal_splits(weights)
        require(unsigned_text(
            record.get("objective_cost"), "objective cost") == objective,
            "objective cost mismatch")
        profile_fingerprint = mix_integer(FNV_OFFSET, site)
        profile_fingerprint = mix_text(profile_fingerprint, source)
        for case, weight in zip(sorted_cases, weights):
            profile_fingerprint = mix_text(
                profile_fingerprint, case["text"])
            profile_fingerprint = mix_integer(
                profile_fingerprint, weight)
        profile_fingerprint = mix_text(profile_fingerprint, "default")
        profile_fingerprint = mix_integer(
            profile_fingerprint, default_weight)
        require(unsigned_text(
            record.get("profile_fingerprint"), "profile fingerprint") ==
            profile_fingerprint, "profile fingerprint mismatch")
        require(record.get("fallback_reason") == "",
                "valid profile has a fallback reason")
    else:
        require(record.get("profile_source") == "none",
                "invalid profile has a source")
        require(record.get("profile_fingerprint") == "" and
                record.get("default_weight") == "" and
                record.get("objective_cost") == "",
                "invalid profile exposes profile proof fields")
        for case in case_by_ordinal.values():
            require(case["weight_text"] == "",
                    "invalid profile exposes case weight")
        if requested == "profile":
            require(isinstance(record.get("fallback_reason"), str) and
                    record["fallback_reason"],
                    "profile fallback reason is missing")
        else:
            require(record.get("fallback_reason") == "",
                    "non-profile mode has a fallback reason")

    expected_schema = (
        "bounded-switch-chain-v1" if effective == "linear"
        else "bounded-switch-profile-tree-v1"
        if effective == "profile"
        else "bounded-switch-tree-v1")
    require(record.get("node_schema") == expected_schema,
            "node schema mismatch")

    tree_fingerprint = mix_integer(FNV_OFFSET, site)
    tree_fingerprint = mix_text(tree_fingerprint, effective)
    if profile_valid:
        tree_fingerprint = mix_integer(
            tree_fingerprint, profile_fingerprint)
    tree_fingerprint = mix_integer(tree_fingerprint, logical_edges)
    tree_fingerprint = mix_integer(tree_fingerprint, lowered_edges)
    tree_fingerprint = mix_integer(
        tree_fingerprint, unique_destinations)
    tree_fingerprint = mix_integer(
        tree_fingerprint, default_destination)
    for destination in destinations:
        tree_fingerprint = mix_integer(
            tree_fingerprint, destination["original_multiplicity"])
        tree_fingerprint = mix_integer(
            tree_fingerprint, destination["lowered_multiplicity"])

    cursor = 0
    if effective == "linear":
        require(len(nodes) == case_count, "linear node count mismatch")
        for case in cases:
            node = nodes[cursor]
            require(isinstance(node, dict), "linear node is not an object")
            require(node.get("id") == cursor and
                    node.get("kind") == "equal",
                    "linear node identity mismatch")
            require(node.get("case_ordinal") == case["ordinal"] and
                    node.get("value") == case["value"] and
                    node.get("destination") == case["destination"],
                    "linear node payload mismatch")
            tree_fingerprint = mix_text(
                tree_fingerprint, case["value"])
            tree_fingerprint = mix_integer(
                tree_fingerprint, case["ordinal"])
            tree_fingerprint = mix_integer(
                tree_fingerprint, case["destination"])
            cursor += 1
    else:
        require(len(nodes) == 2 * case_count - 1,
                "range node count mismatch")

        def visit(begin, end, depth):
            nonlocal cursor, tree_fingerprint
            require(cursor < len(nodes), "range tree ended early")
            node = nodes[cursor]
            require(isinstance(node, dict), "range node is not an object")
            require(node.get("id") == cursor and
                    node.get("begin") == begin and
                    node.get("end") == end and
                    node.get("depth") == depth,
                    "range node coordinates mismatch")
            cursor += 1
            if end - begin == 1:
                case = sorted_cases[begin]
                ordinal = case["ordinal"]
                require(node.get("kind") == "equal" and
                        node.get("case_ordinal") == ordinal and
                        node.get("value") == case["text"] and
                        node.get("destination") == case["destination"],
                        "range leaf payload mismatch")
                tree_fingerprint = mix_text(
                    tree_fingerprint, case["text"])
                tree_fingerprint = mix_integer(
                    tree_fingerprint, ordinal)
                tree_fingerprint = mix_integer(
                    tree_fingerprint, case["destination"])
                return
            middle = (
                splits[begin][end] if profile_valid
                else begin + (end - begin) // 2)
            require(node.get("kind") == "unsigned-upper" and
                    node.get("split") == middle and
                    node.get("value") ==
                    sorted_cases[middle - 1]["text"],
                    "range split payload mismatch")
            tree_fingerprint = mix_integer(tree_fingerprint, begin)
            tree_fingerprint = mix_integer(tree_fingerprint, end)
            tree_fingerprint = mix_integer(tree_fingerprint, middle)
            visit(begin, middle, depth + 1)
            visit(middle, end, depth + 1)

        visit(0, case_count, 0)
        require(cursor == len(nodes), "range tree has trailing nodes")

    require(unsigned_text(
        record.get("tree_fingerprint"), "tree fingerprint") ==
        tree_fingerprint, "tree fingerprint mismatch")
    return True


def verify_path(path):
    count = 0
    with Path(path).open("r", encoding="utf-8") as manifest:
        for line_number, line in enumerate(manifest, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                verify_record(record)
            except (json.JSONDecodeError, VerificationError) as error:
                raise VerificationError(
                    f"{path}:{line_number}: {error}") from error
            count += 1
    require(count > 0, f"{path}: manifest is empty")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", nargs="+")
    arguments = parser.parse_args(argv)
    verified = 0
    try:
        for path in arguments.manifest:
            verified += verify_path(path)
    except (OSError, VerificationError) as error:
        print(f"verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified {verified} IFSS switch manifest record(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
