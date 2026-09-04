# RUN: env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider %s

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QSYM_TESTS = ROOT / "runtime" / "src" / "backends" / "qsym" / "qsym" / "tests"


def test_default_pytest_discovery_is_project_scoped(pytestconfig):
    assert pytestconfig.rootpath == ROOT
    assert tuple(pytestconfig.getini("testpaths")) == ("test",)

    missing_lit_entry = []
    lit_marker = "# " + "RUN" + ":"
    for path in sorted((ROOT / "test").glob("test_*.py")):
        with path.open(encoding="utf-8") as test_file:
            if not any(line.startswith(lit_marker) for line in test_file):
                missing_lit_entry.append(path.relative_to(ROOT).as_posix())
    assert not missing_lit_entry


def test_qsym_suite_is_not_globally_ignored(pytestconfig):
    assert ROOT in QSYM_TESTS.parents
    assert ROOT / "test" not in QSYM_TESTS.parents

    ignored = tuple(pytestconfig.getini("norecursedirs"))
    assert not any("qsym" in pattern.lower() for pattern in ignored)

    addopts = tuple(pytestconfig.getini("addopts"))
    assert not any(option.startswith("--ignore") for option in addopts)
