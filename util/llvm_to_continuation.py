#!/usr/bin/env python3
"""Drive the LLVM pass that exports executable SymCC continuation IR."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from typing import Any, Iterable


LOWERING_SCHEMA = "symcc-llvm-continuation-lowering-v1"
PROGRAM_SCHEMA = "symcc-live-program-v1"


def _plugin_path(configured: str = "") -> str:
    candidates: list[Path] = []
    raw = configured or os.environ.get("SYMCC_PASS_DIR", "")
    if raw:
        path = Path(raw).expanduser()
        candidates.append(path / "libsymcc.so" if path.is_dir() else path)
    root = Path(__file__).resolve().parents[1]
    candidates.extend([
        root / "build" / "libsymcc.so",
        root / "libsymcc.so",
    ])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise FileNotFoundError(
        "libsymcc.so was not found; set --plugin or SYMCC_PASS_DIR"
    )


def _tool(configured: str, default: str) -> str:
    candidate = configured or shutil.which(default)
    if not candidate:
        raise FileNotFoundError(f"{default} was not found")
    return candidate


def lower_llvm_to_program(
    source: str,
    output: str,
    *,
    entry: str = "main",
    plugin: str = "",
    clang: str = "",
    opt: str = "",
    compiler_args: Iterable[str] = (),
) -> dict[str, Any]:
    """Lower one LLVM/C source module and atomically publish its artifact."""
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"LLVM lowering input does not exist: {source}")
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plugin_path = _plugin_path(plugin)
    source_suffix = source_path.suffix
    suffix = source_suffix.lower()
    artifact_fd, artifact_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".json",
        dir=output_path.parent,
    )
    os.close(artifact_fd)
    object_name = ""
    compile_command: list[str] = []
    try:
        if suffix in {".ll", ".bc"}:
            command = [
                _tool(opt, "opt"),
                f"-load-pass-plugin={plugin_path}",
                "-passes=live-continuation-export",
                "-disable-output",
                str(source_path),
            ]
        else:
            object_fd, object_name = tempfile.mkstemp(
                prefix=".symcc-live-lowering-",
                suffix=".bc",
                dir=output_path.parent,
            )
            os.close(object_fd)
            default_compiler = (
                "clang++"
                if source_suffix == ".C"
                or suffix in {".cc", ".cpp", ".cxx", ".c++", ".ii"}
                else "clang"
            )
            compile_command = [
                _tool(clang, default_compiler),
                *list(compiler_args),
                "-O0",
                "-Xclang",
                "-disable-O0-optnone",
                "-emit-llvm",
                "-c",
                str(source_path),
                "-o",
                object_name,
            ]
            compiled = subprocess.run(
                compile_command,
                check=False,
                capture_output=True,
                text=True,
            )
            if compiled.returncode != 0:
                raise RuntimeError(
                    "LLVM continuation source compilation failed: "
                    + compiled.stderr[-2048:]
                )
            command = [
                _tool(opt, "opt"),
                f"-load-pass-plugin={plugin_path}",
                "-passes=function(sroa,mem2reg,instcombine),"
                "live-continuation-export",
                "-disable-output",
                object_name,
            ]
        environment = os.environ.copy()
        environment.update({
            "SYMCC_LIVE_PROGRAM_OUT": artifact_name,
            "SYMCC_LIVE_ENTRY": entry,
            "SYMCC_LIVE_STRICT": "0",
        })
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "LLVM continuation lowering command failed: "
                + completed.stderr[-2048:]
            )
        try:
            with open(artifact_name, encoding="utf-8") as stream:
                artifact = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "LLVM pass did not produce a valid lowering artifact"
            ) from exc
        schema = str(artifact.get("schema", ""))
        if schema not in {PROGRAM_SCHEMA, LOWERING_SCHEMA}:
            raise RuntimeError(f"unexpected lowering schema {schema!r}")
        os.replace(artifact_name, output_path)
        status = (
            "lowered" if schema == PROGRAM_SCHEMA
            else str(artifact.get("status", "rejected"))
        )
        result = {
            "schema": LOWERING_SCHEMA,
            "status": status,
            "source": str(source_path),
            "output": str(output_path),
            "entry": entry,
            "command": command,
            "diagnostics": artifact.get("diagnostics", []),
        }
        if compile_command:
            result["compile_command"] = compile_command
        return result
    finally:
        Path(artifact_name).unlink(missing_ok=True)
        if object_name:
            Path(object_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--output", required=True)
    parser.add_argument("--entry", default="main")
    parser.add_argument("--plugin", default="")
    parser.add_argument("--clang", default="")
    parser.add_argument("--opt", default="")
    parser.add_argument(
        "--compiler-args",
        default="",
        help="Additional compiler flags parsed with shell quoting",
    )
    args = parser.parse_args()
    result = lower_llvm_to_program(
        args.source,
        args.output,
        entry=args.entry,
        plugin=args.plugin,
        clang=args.clang,
        opt=args.opt,
        compiler_args=shlex.split(args.compiler_args),
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "lowered" else 2


if __name__ == "__main__":
    raise SystemExit(main())
