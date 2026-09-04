#!/usr/bin/env python3
"""Bounded remote worker used by the F451 physical transport oracle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from typing import Any, Mapping


REQUEST_SCHEMA = "symcc-f451-remote-worker-request-v1"
RESPONSE_SCHEMA = "symcc-f451-remote-worker-response-v1"
MAX_REQUEST_BYTES = 16 * 1024 * 1024


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("remote worker JSON contains duplicate members")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"remote worker JSON contains {value}")


def _verify_digest(value: Mapping[str, Any], field: str, label: str) -> dict[str, Any]:
    body = dict(value)
    supplied = body.pop(field, None)
    if not isinstance(supplied, str) or supplied != _digest(body):
        raise ValueError(f"{label} identity changed")
    body[field] = supplied
    return body


def _load_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("remote worker request exceeds its byte contract")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("remote worker request is not strict ASCII JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("remote worker request must be an object")
    request = _verify_digest(value, "request_sha256", "remote worker request")
    if set(request) != {
        "schema",
        "lease",
        "result_template",
        "assignment",
        "request_sha256",
    } or request["schema"] != REQUEST_SCHEMA:
        raise ValueError("remote worker request scope changed")
    lease = request["lease"]
    if not isinstance(lease, Mapping):
        raise ValueError("remote worker lease is invalid")
    checked_lease = _verify_digest(lease, "lease_sha256", "remote cube lease")
    if set(checked_lease) != {
        "schema",
        "protocol",
        "cube",
        "ulfm_fence",
        "lease_sha256",
    } or (
        checked_lease.get("schema")
        != "symcc-qfbv-generation-fenced-cube-lease-v1"
        or checked_lease.get("protocol")
        != "symcc-qfbv-generation-fenced-partition-execution-v1"
    ):
        raise ValueError("remote cube lease schema changed")
    cube = checked_lease.get("cube")
    if not isinstance(cube, Mapping):
        raise ValueError("remote cube scope is invalid")
    fence = checked_lease.get("ulfm_fence")
    if not isinstance(fence, Mapping):
        raise ValueError("remote ULFM fence is invalid")
    checked_fence = _verify_digest(fence, "fence_sha256", "remote ULFM fence")
    if set(checked_fence) != {
        "schema",
        "protocol",
        "run_id",
        "generation",
        "generation_token",
        "endpoint_id",
        "shard_id",
        "shard_token",
        "work_id",
        "lease_token",
        "fence_sha256",
    } or (
        checked_fence.get("schema") != "symcc-ulfm-work-fence-v1"
        or checked_fence.get("protocol")
        != "symcc-generation-fenced-ulfm-recovery-v1"
    ):
        raise ValueError("remote ULFM fence scope changed")
    execution = cube.get("execution_sha256")
    ordinal = cube.get("ordinal")
    if (
        not isinstance(execution, str)
        or not isinstance(ordinal, int)
        or checked_fence.get("run_id") != f"qfbv-cubes:{execution}"
        or checked_fence.get("work_id") != f"cube:{execution}:{ordinal}"
    ):
        raise ValueError("remote work is not bound to the cube")
    if not isinstance(request["result_template"], Mapping):
        raise ValueError("remote result template is invalid")
    assignment = request["assignment"]
    if type(assignment) is not int or not 0 <= assignment <= 255:
        raise ValueError("remote assignment is outside one byte")
    return request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("solve", "fail"), required=True)
    args = parser.parse_args()
    request = _load_request()
    if args.mode == "fail":
        os._exit(86)
    result = dict(request["result_template"])
    result.update(
        {
            "status": "sat",
            "assignments": {"0": request["assignment"]},
            "backend_model_verified": True,
            "backend_f451_remote_host": socket.gethostname(),
            "backend_f451_remote_lease_sha256": request["lease"][
                "lease_sha256"
            ],
            "backend_f451_remote_request_sha256": request["request_sha256"],
        }
    )
    response = {
        "schema": RESPONSE_SCHEMA,
        "lease_sha256": request["lease"]["lease_sha256"],
        "request_sha256": request["request_sha256"],
        "host": socket.gethostname(),
        "result": result,
    }
    response["response_sha256"] = _digest(response)
    sys.stdout.buffer.write(_canonical_json(response) + b"\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"f451-remote-worker: {error}", file=sys.stderr)
        raise SystemExit(2) from error
