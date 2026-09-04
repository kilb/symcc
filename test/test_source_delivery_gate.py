# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.source_delivery_gate import (  # noqa: E402
    DeliveryManifestError,
    RECURSIVE_ROOTS,
    ROOT_FILES,
    load_manifest,
    verify,
    write_manifest,
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True,
    )


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repository"
    for name in RECURSIVE_ROOTS:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ROOT_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="ascii")
    (root / "util" / "engine.py").write_text("VALUE = 1\n", encoding="ascii")
    (root / "test" / "test_engine.py").write_text(
        "def test_value():\n    assert True\n", encoding="ascii",
    )
    (root / "benchmark" / "run_case.py").write_text(
        "print('case')\n", encoding="ascii",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "SymCC Tests")
    manifest = root / "source-delivery-manifest.json"
    write_manifest(root, manifest)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "sealed source")
    return root, manifest


def _repository_with_submodule(tmp_path: Path) -> tuple[Path, Path, Path]:
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    _git(dependency, "init", "-q")
    _git(dependency, "config", "user.email", "tests@example.invalid")
    _git(dependency, "config", "user.name", "SymCC Tests")
    (dependency / "runtime.c").write_text("int value = 1;\n", encoding="ascii")
    _git(dependency, "add", ".")
    _git(dependency, "commit", "-qm", "runtime v1")

    root, manifest = _repository(tmp_path)
    _git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(dependency),
        "runtime",
    )
    write_manifest(root, manifest)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal runtime gitlink")
    return root, manifest, dependency


def test_delivery_gate_accepts_exact_tracked_clean_checkout(tmp_path):
    root, manifest = _repository(tmp_path)
    result = verify(root, manifest, require_tracked=True, require_clean=True)
    assert result["matched"] is True
    assert result["file_count"] == 3 + len(ROOT_FILES)
    assert result["missing"] == []
    assert result["unexpected"] == []
    assert result["changed"] == []
    assert result["untracked"] == []
    assert result["dirty"] == []

    (root / "Dockerfile").unlink()
    with pytest.raises(DeliveryManifestError, match="required.*missing"):
        write_manifest(root, manifest)


def test_delivery_gate_rejects_changes_replacements_and_untracked_source(tmp_path):
    root, manifest = _repository(tmp_path)
    (root / "util" / "engine.py").write_text("VALUE = 2\n", encoding="ascii")
    changed = verify(root, manifest, require_tracked=True, require_clean=True)
    assert changed["matched"] is False
    assert changed["changed"] == ["util/engine.py"]
    assert changed["dirty"] == ["util/engine.py"]

    _git(root, "checkout", "--", "util/engine.py")
    (root / "test" / "test_engine.py").unlink()
    (root / "test" / "test_replacement.py").write_text(
        "def test_replacement():\n    assert True\n", encoding="ascii",
    )
    replacement = verify(root, manifest, require_tracked=True, require_clean=True)
    assert replacement["matched"] is False
    assert replacement["missing"] == ["test/test_engine.py"]
    assert replacement["unexpected"] == ["test/test_replacement.py"]

    (root / "util" / "new_backend.rs").write_text(
        "fn main() {}\n", encoding="ascii"
    )
    (root / ".github" / "workflows" / "unsealed.yml").write_text(
        "name: unsealed\n", encoding="ascii"
    )
    expanded = verify(root, manifest, require_tracked=True, require_clean=True)
    assert expanded["unexpected"] == [
        ".github/workflows/unsealed.yml",
        "test/test_replacement.py",
        "util/new_backend.rs",
    ]


def test_delivery_manifest_rejects_duplicate_json_members(tmp_path):
    root, manifest = _repository(tmp_path)
    payload = json.loads(manifest.read_text(encoding="ascii"))
    tampered = manifest.with_name("tampered.json")
    tampered.write_text(
        '{"schema":"%s","schema":"%s"}\n'
        % (payload["schema"], payload["schema"]),
        encoding="ascii",
    )
    with pytest.raises(DeliveryManifestError, match="duplicate JSON member"):
        load_manifest(tampered)


def test_delivery_gate_binds_submodule_commit(tmp_path):
    root, manifest, dependency = _repository_with_submodule(tmp_path)
    accepted = verify(root, manifest, require_tracked=True, require_clean=True)
    assert accepted["matched"] is True
    assert accepted["gitlink_count"] == 1

    (dependency / "runtime.c").write_text("int value = 2;\n", encoding="ascii")
    _git(dependency, "add", ".")
    _git(dependency, "commit", "-qm", "runtime v2")
    _git(root / "runtime", "fetch", "-q")
    _git(root / "runtime", "checkout", "-q", "FETCH_HEAD")
    _git(root, "add", "runtime")
    changed = verify(root, manifest, require_tracked=True, require_clean=True)
    assert changed["matched"] is False
    assert changed["changed_gitlinks"] == ["runtime"]
    assert changed["dirty"] == ["runtime"]
