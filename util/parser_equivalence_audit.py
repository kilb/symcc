#!/usr/bin/env python3
"""Exhaustively compare two validated parser oracles on a bounded byte domain."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator, Mapping

from cross_parser_oracle import (
    ParserFailure,
    _command_digest,
    _parse_command,
    compare,
)
from verified_proposals import VerifiedProposalManager


AUDIT_SCHEMA = "symcc-parser-bounded-equivalence-v1"
AUDIT_PROOF = "exhaustive-shortlex-byte-domain-v1"
MAX_CASES = 65536
MAX_COUNTEREXAMPLES = 256


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _domain_size(alphabet_size: int, max_length: int) -> int:
    return sum(alphabet_size ** length for length in range(max_length + 1))


def _candidates(
    alphabet: tuple[int, ...],
    max_length: int,
) -> Iterator[bytes]:
    yield b""
    for length in range(1, max_length + 1):
        for values in itertools.product(alphabet, repeat=length):
            yield bytes(values)


def _atomic_write(path: str, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_canonical_bytes(value))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _normalize_alphabet(alphabet_hex: str) -> tuple[int, ...]:
    if (
        not isinstance(alphabet_hex, str) or
        not 2 <= len(alphabet_hex) <= 32 or
        len(alphabet_hex) % 2
    ):
        raise ValueError("alphabet must encode 1..16 bytes")
    try:
        values = tuple(bytes.fromhex(alphabet_hex))
    except ValueError as error:
        raise ValueError("alphabet is not hexadecimal") from error
    if len(set(values)) != len(values):
        raise ValueError("alphabet bytes must be unique")
    return tuple(sorted(values))


def verify_artifact(artifact: Mapping[str, Any]) -> bool:
    if (
        not isinstance(artifact, Mapping) or
        artifact.get("schema") != AUDIT_SCHEMA or
        artifact.get("proof") != AUDIT_PROOF or
        not isinstance(artifact.get("complete"), bool) or
        not isinstance(artifact.get("equivalent"), bool) or
        not isinstance(artifact.get("alphabet_hex"), str) or
        not isinstance(artifact.get("counterexamples"), list)
    ):
        return False
    supplied = str(artifact.get("artifact_sha256", ""))
    core = dict(artifact)
    core.pop("artifact_sha256", None)
    if hashlib.sha256(_canonical_bytes(core)).hexdigest() != supplied:
        return False
    try:
        alphabet = _normalize_alphabet(str(artifact["alphabet_hex"]))
        numeric_fields = (
            "max_length",
            "cases",
            "both_accept",
            "primary_only",
            "secondary_only",
            "both_reject",
            "mismatches",
            "elapsed_us",
            "counterexample_limit",
        )
        if any(
            isinstance(artifact.get(key), bool) or
            not isinstance(artifact.get(key), int)
            for key in numeric_fields
        ):
            return False
        max_length = int(artifact["max_length"])
        cases = int(artifact["cases"])
        both_accept = int(artifact["both_accept"])
        primary_only = int(artifact["primary_only"])
        secondary_only = int(artifact["secondary_only"])
        both_reject = int(artifact["both_reject"])
        mismatches = int(artifact["mismatches"])
        elapsed_us = int(artifact["elapsed_us"])
        counterexample_limit = int(artifact["counterexample_limit"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if (
        not 0 <= max_length <= 64 or
        artifact["alphabet_hex"] != bytes(alphabet).hex() or
        not 1 <= cases <= MAX_CASES or
        cases != _domain_size(len(alphabet), max_length) or
        any(value < 0 for value in (
            both_accept,
            primary_only,
            secondary_only,
            both_reject,
            mismatches,
            elapsed_us,
        )) or
        cases != both_accept + primary_only + secondary_only + both_reject or
        mismatches != primary_only + secondary_only or
        bool(artifact["equivalent"]) != (mismatches == 0) or
        not bool(artifact["complete"]) or
        not 0 <= counterexample_limit <= MAX_COUNTEREXAMPLES or
        len(artifact["counterexamples"]) != min(
            mismatches, counterexample_limit) or
        not isinstance(
            artifact.get("counterexamples_truncated"), bool) or
        bool(artifact.get("counterexamples_truncated")) != (
            mismatches > counterexample_limit)
    ):
        return False
    digests = (
        "primary_command_sha256",
        "secondary_command_sha256",
        "transcript_sha256",
    )
    if any(
        not isinstance(artifact.get(key), str) or
        len(str(artifact[key])) != 64 or
        any(character not in "0123456789abcdef"
            for character in str(artifact[key]))
        for key in digests
    ) or artifact["primary_command_sha256"] == artifact[
            "secondary_command_sha256"]:
        return False
    normalized_examples: list[tuple[int, bytes]] = []
    for item in artifact["counterexamples"]:
        if (
            not isinstance(item, dict) or
            set(item) != {
                "input_hex",
                "length",
                "outcome",
                "primary_trace_sha256",
                "secondary_trace_sha256",
            } or
            item.get("outcome") not in {
                "primary_only", "secondary_only"} or
            isinstance(item.get("length"), bool) or
            not isinstance(item.get("length"), int)
        ):
            return False
        try:
            candidate = bytes.fromhex(str(item["input_hex"]))
        except ValueError:
            return False
        if (
            len(candidate) != item["length"] or
            len(candidate) > max_length or
            any(value not in alphabet for value in candidate) or
            any(
                not isinstance(item.get(key), str) or
                len(str(item[key])) != 64 or
                any(character not in "0123456789abcdef"
                    for character in str(item[key]))
                for key in (
                    "primary_trace_sha256",
                    "secondary_trace_sha256",
                )
            )
        ):
            return False
        normalized_examples.append((len(candidate), candidate))
    if (
        normalized_examples != sorted(normalized_examples) or
        len(set(normalized_examples)) != len(normalized_examples) or
        artifact.get("minimal_counterexample") != (
            artifact["counterexamples"][0]
            if artifact["counterexamples"] else None)
    ):
        return False
    return True


def audit(
    *,
    primary_command: str,
    secondary_command: str,
    alphabet_hex: str,
    max_length: int,
    timeout: float = 5.0,
    counterexample_limit: int = 32,
) -> dict[str, Any]:
    primary_arguments = _parse_command(primary_command)
    secondary_arguments = _parse_command(secondary_command)
    if primary_arguments == secondary_arguments:
        raise ValueError("paired parser commands must be independent")
    alphabet = _normalize_alphabet(alphabet_hex)
    max_length = int(max_length)
    counterexample_limit = int(counterexample_limit)
    if not 0 <= max_length <= 64:
        raise ValueError("max length must be between 0 and 64")
    cases = _domain_size(len(alphabet), max_length)
    if cases > MAX_CASES:
        raise ValueError("bounded equivalence domain exceeds 65536 cases")
    if not 0 <= counterexample_limit <= MAX_COUNTEREXAMPLES:
        raise ValueError("counterexample limit must be between 0 and 256")
    timeout = max(0.01, min(float(timeout), 60.0))

    counts = {
        "both_accept": 0,
        "primary_only": 0,
        "secondary_only": 0,
        "both_reject": 0,
    }
    counterexamples: list[dict[str, Any]] = []
    transcript = hashlib.sha256()
    started = time.monotonic_ns()
    with tempfile.TemporaryDirectory(
            prefix="symcc-parser-equivalence-") as tmp:
        manager = VerifiedProposalManager("", os.path.join(tmp, "manager"))
        input_path = os.path.join(tmp, "candidate")
        trace_path = os.path.join(tmp, "trace.json")
        for candidate in _candidates(alphabet, max_length):
            Path(input_path).write_bytes(candidate)
            primary_accepted = compare(
                input_path=input_path,
                trace_path=trace_path,
                primary_command=primary_command,
                secondary_command=secondary_command,
                timeout=timeout,
            )
            raw = json.loads(Path(trace_path).read_text(encoding="utf-8"))
            telemetry = raw.get("cross_parser_telemetry", {})
            primary_returncode = int(
                telemetry.get("primary_returncode", -1))
            normalized = manager._load_parser_trace(
                trace_path,
                candidate_size=len(candidate),
                returncode=primary_returncode,
                candidate=candidate,
            )
            if normalized is None:
                raise ParserFailure(
                    "manager rejected a bounded-audit parser trace")
            trace = normalized[0]
            cross = trace["cross_values"]
            secondary_accepted = bool(
                cross["both_accept"] or cross["secondary_only"])
            if primary_accepted and secondary_accepted:
                outcome = "both_accept"
            elif primary_accepted:
                outcome = "primary_only"
            elif secondary_accepted:
                outcome = "secondary_only"
            else:
                outcome = "both_reject"
            counts[outcome] += 1
            entry = {
                "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
                "input_length": len(candidate),
                "outcome": outcome,
                "primary_trace_sha256": str(
                    telemetry["primary_trace_sha256"]),
                "secondary_trace_sha256": str(
                    telemetry["secondary_trace_sha256"]),
            }
            transcript.update(_canonical_bytes(entry))
            transcript.update(b"\n")
            if (
                outcome in {"primary_only", "secondary_only"} and
                len(counterexamples) < counterexample_limit
            ):
                counterexamples.append({
                    "input_hex": candidate.hex(),
                    "length": len(candidate),
                    "outcome": outcome,
                    "primary_trace_sha256":
                        entry["primary_trace_sha256"],
                    "secondary_trace_sha256":
                        entry["secondary_trace_sha256"],
                })

    mismatches = counts["primary_only"] + counts["secondary_only"]
    core = {
        "schema": AUDIT_SCHEMA,
        "proof": AUDIT_PROOF,
        "complete": True,
        "equivalent": mismatches == 0,
        "alphabet_hex": bytes(alphabet).hex(),
        "max_length": max_length,
        "cases": cases,
        **counts,
        "mismatches": mismatches,
        "counterexample_limit": counterexample_limit,
        "counterexamples_truncated":
            mismatches > counterexample_limit,
        "minimal_counterexample":
            counterexamples[0] if counterexamples else None,
        "counterexamples": counterexamples,
        "primary_command_sha256": _command_digest(primary_arguments),
        "secondary_command_sha256": _command_digest(secondary_arguments),
        "transcript_sha256": transcript.hexdigest(),
        "elapsed_us": (time.monotonic_ns() - started) // 1000,
    }
    artifact = {
        "artifact_sha256": hashlib.sha256(
            _canonical_bytes(core)).hexdigest(),
        **core,
    }
    if not verify_artifact(artifact):
        raise ParserFailure(
            "bounded equivalence artifact failed self-verification")
    return artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-command")
    parser.add_argument("--secondary-command")
    parser.add_argument("--alphabet-hex")
    parser.add_argument("--max-length", type=int)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--counterexample-limit", type=int, default=32)
    parser.add_argument("--output")
    parser.add_argument("--require-equivalent", action="store_true")
    parser.add_argument("--verify")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.verify:
        try:
            artifact = json.loads(
                Path(args.verify).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return 2
        verified = verify_artifact(artifact)
        print(json.dumps(
            {"verified": verified},
            sort_keys=True,
            separators=(",", ":"),
        ))
        return 0 if verified else 2
    if (
        not args.primary_command or
        not args.secondary_command or
        not args.alphabet_hex or
        args.max_length is None or
        not args.output
    ):
        return 2
    try:
        artifact = audit(
            primary_command=args.primary_command,
            secondary_command=args.secondary_command,
            alphabet_hex=args.alphabet_hex,
            max_length=args.max_length,
            timeout=args.timeout,
            counterexample_limit=args.counterexample_limit,
        )
        _atomic_write(args.output, artifact)
        print(json.dumps({
            "artifact_sha256": artifact["artifact_sha256"],
            "cases": artifact["cases"],
            "equivalent": artifact["equivalent"],
            "mismatches": artifact["mismatches"],
        }, sort_keys=True, separators=(",", ":")))
        if args.require_equivalent and not artifact["equivalent"]:
            return 1
        return 0
    except (OSError, ParserFailure, ValueError) as error:
        print(str(error), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
