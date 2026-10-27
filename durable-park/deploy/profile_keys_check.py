#!/usr/bin/env python3
"""Which Hermes profiles exist, and does every profile that has tools have its own withHuman key?

Two commands, both for the gateway start (deploy/tool_policy_check.sh, deploy/plugin_load_check.sh):

    profile_keys_check.py list  --hermes-home H [--serving]
        The installed profile names, one per line. Stdlib only. ``--serving`` leaves out the ones an admin parked.
    profile_keys_check.py check --hermes-home H [--status-root DIR] [--instances-dir DIR] [--default-key FILE]
                                [--dry-run]
        Prints one problem per line, never key text; exits 0 (none) or 1 (problems). With problems it also rewrites
        the tool policy report of the default profile and of every installed profile as failing, so every copy of
        durable-park refuses every tool call until an admin fixes it and restarts the gateway. Exit 3: the check
        did not finish (the caller then fails the reports itself). Needs PyYAML or ruamel.yaml (Hermes's own
        python has ruamel.yaml).

What counts as a problem (docs/specs/2026-10-26-per-person-roles-design.md, sections 6.3, 6.4 and 7): a profile with
tools but no key file, a secrets command that does not name that profile, two profiles that share a key. The
``not-set-up`` profile needs no key; it must have no tools at all.

A profile is a directory under ``<home>/profiles`` that Hermes itself would list: a name Hermes accepts, not
``default``, one of Hermes's identity files inside, and no tombstone in ``profiles/.deleted``
(hermes_constants.py ``named_profile_has_identity``, hermes_cli/profiles.py ``_iter_named_profile_dirs``).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _load_keys():
    """plugin/durable_park/keys.py, loaded by path (this script runs from the checkout, outside the plugin package)."""
    path = Path(__file__).resolve().parent.parent / "plugin" / "durable_park" / "keys.py"
    spec = importlib.util.spec_from_file_location("durable_park_keys", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_keys = _load_keys()

DEFAULT = "default"
NOT_SET_UP = "not-set-up"
HELPER_PROGRAM = "hermes-secrets"
STATUS_FILE = "tool_policy_status.json"
PROFILES_DIR = "profiles"
DEFAULT_STATUS_SUBDIR = os.path.join("plugin-data", "durable-park")
# What Hermes itself accepts as a profile name (hermes_constants.PROFILE_ID_RE; one character longer than our own rule).
HERMES_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# A directory is a profile only when one of these is in it (hermes_constants._PROFILE_IDENTITY_MARKERS).
IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
TRUE_WORDS = frozenset({"1", "true", "yes", "on", "y", "t"})  # a ``gateway.standalone`` written as text


@dataclass(frozen=True)
class ProfileFacts:
    """What the start check knows about one profile. ``key_sha256`` is a hash of the key's first line (for the
    shared-key check only; never printed). ``key_unreadable``: the key file exists but is empty or cannot be read."""
    has_tools: bool
    key_sha256: str | None
    secrets_command: str | None
    secrets_enabled: bool = True
    secrets_override: bool = True
    key_unreadable: bool = False


@dataclass(frozen=True)
class InstalledProfile:
    name: str
    home: Path
    parked: bool  # an admin ran ``hermes -p NAME gateway stop``: the gateway does not serve it


# ---- the rules ---------------------------------------------------------------------------------------------

def _command_words(command: str | None) -> list[str] | None:
    if command is None:
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _secrets_problems(name: str, facts: ProfileFacts) -> list[str]:
    problems = []
    if not facts.secrets_enabled:
        problems.append(f"profile {name}: secrets command is off")
    words = _command_words(facts.secrets_command)
    if name == DEFAULT:
        if words is None or not words or os.path.basename(words[0]) != HELPER_PROGRAM:
            problems.append(f"profile {name}: secrets command is not the durable-park helper")
        elif len(words) > 1:
            problems.append(f"profile {name}: secrets command must not name a profile")
        return problems
    if words is None or len(words) < 2 or words[1:] != [name]:
        if words and os.path.basename(words[0]) != HELPER_PROGRAM:
            problems.append(f"profile {name}: secrets command is not the durable-park helper")
        else:
            problems.append(f"profile {name}: secrets command does not name {name}")
    elif os.path.basename(words[0]) != HELPER_PROGRAM:
        problems.append(f"profile {name}: secrets command is not the durable-park helper")
    if not facts.secrets_override:
        problems.append(f"profile {name}: secrets command does not override")
    return problems


def key_problems(profiles: dict[str, ProfileFacts]) -> list[str]:
    """What is wrong with the keys of *profiles* (empty = fine). Profile order; the shared-key lines come last."""
    problems: list[str] = []
    keyed: dict[str, list[str]] = {}
    for name in sorted(profiles):
        facts = profiles[name]
        if name == NOT_SET_UP:
            if facts.has_tools:
                problems.append(f"profile {name}: has tools")
            continue
        if not facts.has_tools:
            continue
        if facts.key_unreadable:
            problems.append(f"profile {name}: key file is empty or unreadable")
        elif facts.key_sha256 is None:
            problems.append(f"profile {name}: no key file")
        else:
            keyed.setdefault(facts.key_sha256, []).append(name)
        problems += _secrets_problems(name, facts)
    for names in sorted(keyed.values()):
        if len(names) > 1:
            problems.append(f"profiles {', '.join(sorted(names))} share a key")
    return problems


# ---- which profiles exist ----------------------------------------------------------------------------------

def _has_identity(folder: Path) -> bool:
    return any((folder / marker).is_file() or (folder / marker).is_symlink() for marker in IDENTITY_MARKERS)


def installed_profiles(hermes_home: str | os.PathLike, *, serving_only: bool = False) -> list[InstalledProfile]:
    """The profile folders Hermes would list, sorted by name. Raises ``OSError`` when the folder cannot be read;
    a missing ``profiles`` folder is simply no profiles."""
    root = Path(hermes_home) / PROFILES_DIR
    if not root.is_dir():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if (not entry.is_dir() or name == DEFAULT or not HERMES_PROFILE_ID.fullmatch(name)
                or not _has_identity(entry) or (root / ".deleted" / name).exists()):
            continue
        parked = (entry / "gateway.parked").exists()
        if serving_only and parked:
            continue
        found.append(InstalledProfile(name, entry, parked))
    return found


# ---- facts from the files ----------------------------------------------------------------------------------

def read_yaml(path: Path) -> dict:
    """A Hermes ``config.yaml`` as a dict. Uses PyYAML (the system python has it) and falls back to ruamel.yaml,
    which is what Hermes's own environment has (no PyYAML there; Hermes reads its config as YAML 1.1,
    hermes_yaml.py). ``ImportError`` when neither is installed, ``ValueError`` for a non-mapping."""
    try:
        import yaml
    except ImportError:
        yaml = None
    with open(path, encoding="utf-8") as handle:
        if yaml is not None:
            data = yaml.safe_load(handle)
        else:
            from ruamel.yaml import YAML

            # The pure parser, as Hermes does when libyaml rejects a document (hermes_yaml.safe_load): "the pure
            # parser defines what parses". The live config.yaml has a line the C parser refuses.
            loader = YAML(typ="safe", pure=True)
            loader.version = (1, 1)
            data = loader.load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml is not a mapping")
    return data


def _key_state(path: str | os.PathLike) -> tuple[str | None, bool]:
    """``(hash of the first line, False)``; ``(None, False)`` for no file; ``(None, True)`` for an empty or unreadable one."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError):
        return None, True
    first = text.splitlines()[0].strip() if text.strip() else ""
    if not first:
        return None, True
    return hashlib.sha256(first.encode("utf-8")).hexdigest(), False


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in TRUE_WORDS)


def _read_config(path: Path, read_config: Callable[[Path], dict]) -> dict | None:
    """The config as a dict; ``{}`` when there is no file; ``None`` when it cannot be read."""
    if not path.exists():
        return {}
    try:
        config = read_config(path)
    except Exception:  # noqa: BLE001 - any failure to read is "cannot read": the check must fail closed, not crash
        return None
    return config if isinstance(config, dict) else None


def _secrets_of(config: dict) -> tuple[str | None, bool, bool]:
    section = config.get("secrets")
    section = section.get("command") if isinstance(section, dict) else None
    if not isinstance(section, dict):
        return None, False, False
    command = section.get("command")
    return (command if isinstance(command, str) else None, section.get("enabled") is True,
            section.get("override_existing") is True)


def _not_set_up_has_tools(status_root: Path) -> bool:
    """False only when ``not-set-up``'s own report says ``has_tools`` is exactly false (the no-tools check wrote it)."""
    try:
        report = json.loads((status_root / PROFILES_DIR / NOT_SET_UP / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (isinstance(report, dict) and report.get("has_tools") is False)


def gather(hermes_home: str | os.PathLike, *, instances_dir: str = _keys.INSTANCES_DIR,
           default_key: str = _keys.DEFAULT_TOKEN_FILE, status_root: str | os.PathLike,
           read_config: Callable[[Path], dict] | None = None) -> tuple[dict[str, ProfileFacts], list[str]]:
    """``(facts per profile, extra problems)`` for the default profile and every installed profile. The extra
    problems are the ones that are not about keys: a config that cannot be read, ``gateway.standalone``, a name the
    key rule does not allow."""
    read_config = read_config or read_yaml
    home = Path(hermes_home)
    status_root = Path(status_root)
    facts: dict[str, ProfileFacts] = {}
    extra: list[str] = []
    jobs = [(DEFAULT, home, default_key)]
    for profile in installed_profiles(home):
        if not _keys.valid_profile(profile.name):
            extra.append(f"profile {profile.name}: name is not allowed")
            continue
        jobs.append((profile.name, profile.home, _keys.token_file_for(profile.name, instances_dir=instances_dir)))
    for name, folder, key_path in jobs:
        config = _read_config(folder / "config.yaml", read_config)
        digest, unreadable = _key_state(key_path)
        if config is None:
            extra.append(f"profile {name}: config cannot be read")
            facts[name] = ProfileFacts(True, digest, None, False, False, unreadable)
            continue
        command, enabled, override = _secrets_of(config)
        gateway = config.get("gateway")
        if name != DEFAULT and isinstance(gateway, dict) and _truthy(gateway.get("standalone")):
            extra.append(f"profile {name}: gateway.standalone is on; this setup does not support it")
        has_tools = _not_set_up_has_tools(status_root) if name == NOT_SET_UP else True
        facts[name] = ProfileFacts(has_tools, digest, command, enabled, override, unreadable)
    return facts, extra


# ---- failing the reports -----------------------------------------------------------------------------------

def mark_failing(path: str | os.PathLike, problems: list[str]) -> None:
    """Rewrite the tool policy report at *path* as failing, keeping what it already said (atomic: temp file in the
    same directory, then rename). A report that is missing or is not JSON is replaced by a fresh failing one."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    if not isinstance(report, dict):
        report = {}
    old = report.get("problems")
    merged = [p for p in old if isinstance(p, str)] if isinstance(old, list) else []
    merged += [p for p in problems if p not in merged]
    report.update({"ok": False, "problems": merged,
                   "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    report.setdefault("allowed", [])
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _report_paths(status_root: Path, names: list[str]) -> list[Path]:
    return [status_root / STATUS_FILE, *(status_root / PROFILES_DIR / name / STATUS_FILE for name in names)]


# ---- command line ------------------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="profile_keys_check.py")
    sub = parser.add_subparsers(dest="command", required=True)
    lister = sub.add_parser("list")
    lister.add_argument("--hermes-home", required=True)
    lister.add_argument("--serving", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("--hermes-home", required=True)
    check.add_argument("--status-root", default=None)
    check.add_argument("--instances-dir", default=_keys.INSTANCES_DIR)
    check.add_argument("--default-key", default=_keys.DEFAULT_TOKEN_FILE)
    check.add_argument("--dry-run", action="store_true")
    return parser


def _run_list(args) -> int:
    try:
        names = [p.name for p in installed_profiles(args.hermes_home, serving_only=args.serving)]
    except OSError as exc:
        print(f"profile_keys_check: cannot list the profiles ({type(exc).__name__})", file=sys.stderr)
        return 3
    for name in names:
        print(name)
    return 0


def _run_check(args) -> int:
    home = Path(args.hermes_home)
    status_root = Path(args.status_root) if args.status_root else home / DEFAULT_STATUS_SUBDIR
    try:
        installed = [p.name for p in installed_profiles(home)]
        facts, extra = gather(home, instances_dir=args.instances_dir, default_key=args.default_key,
                              status_root=status_root, read_config=read_yaml)
    except OSError as exc:
        print(f"profile_keys_check: cannot read the profiles ({type(exc).__name__})", file=sys.stderr)
        return 3
    problems = [*extra, *key_problems(facts)]
    for problem in problems:
        print(problem)
    if not problems or args.dry_run:
        return 1 if problems else 0
    try:
        for path in _report_paths(status_root, installed):
            mark_failing(path, problems)
    except OSError as exc:
        print(f"profile_keys_check: could not write a report ({type(exc).__name__})", file=sys.stderr)
        return 3
    return 1


def main(argv: list[str]) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:  # argparse: usage error -> 2 (--help -> 0)
        return int(exc.code or 0)
    try:
        return _run_list(args) if args.command == "list" else _run_check(args)
    except Exception as exc:  # noqa: BLE001 - an unexpected crash must never look like "problems found" (exit 1)
        print(f"profile_keys_check: crashed ({type(exc).__name__})", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
