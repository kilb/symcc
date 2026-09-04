#!/usr/bin/env python3
"""Generate one inspectable F336 renameat2 syscall trace."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "util"))

from distributed_state import durable_rename_noreplace  # noqa: E402


with tempfile.TemporaryDirectory(prefix="symcc-f336-trace-") as tmp:
    root = Path(tmp)
    source = root / "active"
    collision_source = root / "active-collision"
    destination = root / "retired"
    source.mkdir()
    collision_source.mkdir()
    (source / "state").write_bytes(b"committed")
    (collision_source / "state").write_bytes(b"must-remain")
    durable_rename_noreplace(str(source), str(destination))
    if (destination / "state").read_bytes() != b"committed":
        raise SystemExit("retired bytes changed")
    try:
        durable_rename_noreplace(str(collision_source), str(destination))
    except FileExistsError:
        pass
    else:
        raise SystemExit("existing retired path was clobbered")
    if (collision_source / "state").read_bytes() != b"must-remain":
        raise SystemExit("collision source bytes changed")
    print("noreplace retirement and collision rejection complete")
