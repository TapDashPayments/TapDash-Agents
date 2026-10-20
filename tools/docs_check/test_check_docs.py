"""Tests for the docs-update check. Run: python3 -m pytest -q tools/docs_check"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import check_docs  # noqa: E402
from check_docs import DocsMap, dangling_docs, glob_to_regex, missing_docs, overrides, uncovered  # noqa: E402

MAP = DocsMap.load(json.dumps({
    "version": 1,
    "exempt": ["**/*.md", "**/tests/**"],
    "rules": [
        {"name": "Bridge", "code": ["app/bridge/**"], "docs": ["app/docs/architecture.md", "app/docs/gateway.md"]},
        {"name": "Ledger", "code": ["app/plugin/ledger.py"], "docs": ["app/docs/architecture.md"]},
    ],
}))


@pytest.mark.parametrize("pattern, path, ok", [
    ("app/bridge/**", "app/bridge/app.py", True),
    ("app/bridge/**", "app/bridge/sub/x.py", True),
    ("app/bridge/**", "app/bridgework/x.py", False),
    ("**/*.md", "README.md", True),
    ("**/*.md", "a/b/c.md", True),
    ("**/tests/**", "app/tests/test_x.py", True),
    ("app/deploy/*.sh", "app/deploy/x.sh", True),
    ("app/deploy/*.sh", "app/deploy/tunnel/x.sh", False),
    ("a.py", "a_py", False),
])
def test_glob(pattern, path, ok):
    assert bool(glob_to_regex(pattern).match(path)) is ok


def test_code_without_docs_is_reported():
    ((rule, code),) = missing_docs(MAP, ["app/bridge/app.py"])
    assert rule.name == "Bridge" and code == ["app/bridge/app.py"]


def test_any_one_listed_doc_is_enough():
    assert missing_docs(MAP, ["app/bridge/app.py", "app/docs/gateway.md"]) == []


def test_one_doc_can_satisfy_two_rules():
    assert missing_docs(MAP, ["app/bridge/app.py", "app/plugin/ledger.py", "app/docs/architecture.md"]) == []


def test_tests_and_docs_alone_need_nothing():
    assert missing_docs(MAP, ["app/tests/test_bridge.py", "README.md"]) == []


def test_uncovered_files_are_found():
    assert uncovered(MAP, ["app/bridge/app.py", "app/new_thing.py", "app/tests/t.py", "x.md"]) == ["app/new_thing.py"]


def test_dangling_docs_are_found():
    assert dangling_docs(MAP, ["app/docs/architecture.md"]) == ["app/docs/gateway.md"]


def test_override_needs_a_reason():
    assert overrides(["fix: x\n\nDocs-Not-Needed: rename only\n"]) == ["rename only"]
    assert overrides(["fix: x\n\nDocs-Not-Needed:\n"]) == []
    assert overrides(["mentions Docs-Not-Needed: inline but not as a trailer line"]) == []


@pytest.mark.parametrize("bad", [
    {"version": 2, "rules": []},
    {"version": 1, "rules": [{"name": "x", "code": ["a"]}]},
])
def test_bad_map_is_refused(bad):
    with pytest.raises(ValueError):
        DocsMap.load(json.dumps(bad))


# ---- end to end against a throwaway git repo ------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    def run(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/docs-map.json").write_text(json.dumps({
        "version": 1, "exempt": ["**/*.md", ".github/**"],
        "rules": [{"name": "Bridge", "code": ["bridge/**"], "docs": ["docs/arch.md"]}]}))
    (tmp_path / "bridge").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "bridge/app.py").write_text("v1\n")
    (tmp_path / "docs/arch.md").write_text("v1\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    monkeypatch.setattr(check_docs, "REPO", tmp_path)

    def commit(path, text, message):
        (tmp_path / path).write_text(text)
        run("add", "-A")
        run("commit", "-q", "-m", message)
        return run("rev-parse", "HEAD").strip()

    commit.base = run("rev-parse", "HEAD").strip()
    return commit


def test_range_fails_on_code_only_change(repo, capsys):
    head = repo("bridge/app.py", "v2\n", "change bridge")
    assert check_docs.cmd_range(repo.base, head) == 1
    out = capsys.readouterr().out
    assert "bridge/app.py" in out and "docs/arch.md" in out


def test_range_passes_when_docs_change_in_a_later_commit(repo):
    repo("bridge/app.py", "v2\n", "change bridge")
    head = repo("docs/arch.md", "v2\n", "docs")
    assert check_docs.cmd_range(repo.base, head) == 0


def test_range_override_trailer(repo, capsys):
    head = repo("bridge/app.py", "v2\n", "rename a variable\n\nDocs-Not-Needed: no behaviour change")
    assert check_docs.cmd_range(repo.base, head) == 0
    assert "no behaviour change" in capsys.readouterr().out


def test_missing_base_falls_back_to_parent(repo):
    head = repo("bridge/app.py", "v2\n", "change bridge")
    assert check_docs.cmd_range("0" * 40, head) == 1


def test_verify_map_on_repo(repo):
    assert check_docs.cmd_verify_map() == 0
    repo("loose.py", "x\n", "add an unmapped file\n\nDocs-Not-Needed: test")
    assert check_docs.cmd_verify_map() == 1
