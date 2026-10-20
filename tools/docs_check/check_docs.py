#!/usr/bin/env python3
"""Make sure code changes come with the doc changes they need.

The rules live in ``.github/docs-map.json``. A change to a file matching a rule's ``code`` patterns must
also change at least one of that rule's ``docs`` in the same range of commits (a push or a pull request).
To skip the check once, a commit in the range carries the trailer ``Docs-Not-Needed: <reason>``.

    python3 tools/docs_check/check_docs.py --base <rev> --head <rev>   # check a range of commits
    python3 tools/docs_check/check_docs.py --verify-map                # every tracked file is covered
    python3 tools/docs_check/check_docs.py --explain                   # print the rules

Standard library only (it runs in CI, in the git pre-push hook and on the VM's system Python).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAP_PATH = ".github/docs-map.json"
OVERRIDE = re.compile(r"^Docs-Not-Needed:[ \t]*(\S.*)$", re.MULTILINE)
ZERO_SHA = re.compile(r"^0+$")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``**`` matches across directories, ``*`` and ``?`` stay inside one path segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


@dataclass
class Rule:
    name: str
    code: list[str]
    docs: list[str]


@dataclass
class DocsMap:
    exempt: list[str]
    rules: list[Rule]

    @classmethod
    def load(cls, text: str) -> "DocsMap":
        data = json.loads(text)
        if data.get("version") != 1:
            raise ValueError("docs map: unsupported version")
        rules = []
        for raw in data.get("rules", []):
            if not raw.get("name") or not raw.get("code") or not raw.get("docs"):
                raise ValueError(f"docs map: rule needs name, code and docs: {raw!r}")
            rules.append(Rule(str(raw["name"]), list(raw["code"]), list(raw["docs"])))
        return cls(list(data.get("exempt", [])), rules)

    def rules_for(self, path: str) -> list[Rule]:
        return [rule for rule in self.rules if matches(path, rule.code)]


def missing_docs(dmap: DocsMap, changed: list[str]) -> list[tuple[Rule, list[str]]]:
    """Rules whose code changed while none of their docs did, with the code files that triggered them."""
    changed_set = set(changed)
    problems = []
    for rule in dmap.rules:
        code = sorted(p for p in changed if matches(p, rule.code))
        if code and not changed_set.intersection(rule.docs):
            problems.append((rule, code))
    return problems


def uncovered(dmap: DocsMap, tracked: list[str]) -> list[str]:
    """Tracked files that no rule covers and that are not exempt (new files must be mapped)."""
    return sorted(p for p in tracked if not matches(p, dmap.exempt) and not dmap.rules_for(p))


def dangling_docs(dmap: DocsMap, tracked: list[str]) -> list[str]:
    """Docs named in the map that do not exist."""
    known = set(tracked)
    return sorted({d for rule in dmap.rules for d in rule.docs if d not in known})


def overrides(messages: list[str]) -> list[str]:
    return [m.group(1).strip() for text in messages for m in OVERRIDE.finditer(text)]


# ---- git ------------------------------------------------------------------------------------------

def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout


def rev_exists(rev: str) -> bool:
    if not rev or ZERO_SHA.match(rev):
        return False
    return subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"], cwd=REPO,
                          capture_output=True).returncode == 0


def range_files(base: str, head: str) -> list[str]:
    out = git("diff", "--name-only", "--no-renames", f"{base}...{head}")
    return [line for line in out.splitlines() if line]


def range_messages(base: str, head: str) -> list[str]:
    out = git("log", "--format=%B%x00", f"{base}..{head}")
    return [m for m in out.split("\0") if m.strip()]


def tracked_files(rev: str = "HEAD") -> list[str]:
    return [line for line in git("ls-tree", "-r", "--name-only", rev).splitlines() if line]


def map_at(rev: str) -> DocsMap:
    return DocsMap.load(git("show", f"{rev}:{MAP_PATH}"))


# ---- commands -------------------------------------------------------------------------------------

def cmd_range(base: str, head: str) -> int:
    if not rev_exists(head):
        print(f"docs check: unknown head {head!r}", file=sys.stderr)
        return 2
    if not rev_exists(base):
        parent = f"{head}~1"
        if not rev_exists(parent):
            print("docs check: no earlier commit to compare with; skipped")
            return 0
        base = parent
    dmap = map_at(head)
    problems = missing_docs(dmap, range_files(base, head))
    if not problems:
        print("docs check: ok")
        return 0
    reasons = overrides(range_messages(base, head))
    if reasons:
        print(f"docs check: skipped by Docs-Not-Needed ({'; '.join(reasons)})")
        return 0
    print("docs check: these code changes need their docs updated in the same change:\n")
    for rule, code in problems:
        print(f"  {rule.name}")
        for path in code:
            print(f"    changed: {path}")
        print(f"    update at least one of: {', '.join(rule.docs)}\n")
    print("Update the docs (diagrams included), or, if the change really needs none, add this line to a\n"
          "commit message:  Docs-Not-Needed: <why>")
    return 1


def cmd_verify_map(rev: str = "HEAD") -> int:
    dmap = map_at(rev)
    tracked = tracked_files(rev)
    loose, dangling = uncovered(dmap, tracked), dangling_docs(dmap, tracked)
    for path in loose:
        print(f"docs map: not covered by any rule (add it to {MAP_PATH}): {path}")
    for path in dangling:
        print(f"docs map: names a doc that does not exist: {path}")
    if loose or dangling:
        return 1
    print(f"docs map: ok ({len(dmap.rules)} rules, {len(tracked)} files)")
    return 0


def cmd_explain(rev: str = "HEAD") -> int:
    dmap = map_at(rev)
    for rule in dmap.rules:
        print(f"{rule.name}\n  code: {', '.join(rule.code)}\n  docs: {', '.join(rule.docs)}")
    print(f"exempt: {', '.join(dmap.exempt)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="commit the range starts after (exclusive)")
    parser.add_argument("--head", default="HEAD", help="last commit of the range (default HEAD)")
    parser.add_argument("--verify-map", action="store_true", help="check the map covers every tracked file")
    parser.add_argument("--explain", action="store_true", help="print the rules")
    args = parser.parse_args(argv)
    if args.explain:
        return cmd_explain(args.head)
    if args.verify_map:
        return cmd_verify_map(args.head)
    if args.base is None:
        parser.error("give --base (and optionally --head), --verify-map or --explain")
    return cmd_range(args.base, args.head)


if __name__ == "__main__":
    sys.exit(main())
