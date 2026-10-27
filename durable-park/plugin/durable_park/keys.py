"""Which withHuman key file belongs to which Hermes profile.

Standard library only (the sweep may load this file by path). The default profile keeps the one key file it has
always had; every other profile has its own file, named after the profile, so one person's key is never read for
another person's calls.
"""
from __future__ import annotations

import re

DEFAULT_TOKEN_FILE = "/etc/durable-park/instance_token"
INSTANCES_DIR = "/etc/durable-park/instances"
# Letters, digits, ``-`` and ``_``; at most 63 characters; starts with a letter or digit. No dots and no slashes,
# so a name can never walk out of the instances directory.
PROFILE_PATTERN = r"[a-z0-9][a-z0-9_-]{0,62}"
_PROFILE = re.compile(PROFILE_PATTERN)
# Names that mean "the default profile" and so can never be a person's own profile. ``main`` is the name Hermes puts
# in the session key of the default profile (``agent:main:...``).
RESERVED_PROFILES = frozenset({"default", "main"})
DEFAULT_PROFILE_NAMES = frozenset({"", "default", "main"})


def valid_profile(name: object) -> bool:
    """True for a name a person's profile may have (``fullmatch``: a trailing newline does not pass)."""
    return isinstance(name, str) and name not in RESERVED_PROFILES and _PROFILE.fullmatch(name) is not None


def is_default_profile(profile: str | None) -> bool:
    """None, ``""``, ``"default"`` and ``"main"`` are the default profile."""
    return profile is None or profile in DEFAULT_PROFILE_NAMES


def token_file_for(profile: str | None, *, default: str = DEFAULT_TOKEN_FILE,
                   instances_dir: str = INSTANCES_DIR) -> str:
    """The key file of *profile*: ``default`` for the default profile (see ``is_default_profile``), else ``<instances_dir>/<profile>.token``.

    Raises ``ValueError`` for a name that is not a valid profile name (never builds a path from it).
    """
    if is_default_profile(profile):
        return default
    if not valid_profile(profile):
        raise ValueError("not a valid profile name")
    return f"{instances_dir.rstrip('/')}/{profile}.token"
