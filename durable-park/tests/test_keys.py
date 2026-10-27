import importlib.util
import os
import sys

import pytest

from durable_park import keys
from durable_park.keys import DEFAULT_TOKEN_FILE, INSTANCES_DIR, is_default_profile, token_file_for, valid_profile

HERE = os.path.dirname(os.path.abspath(__file__))
RECEIVER = os.path.join(os.path.dirname(HERE), "receiver")


def test_token_file_for_default():
    assert token_file_for(None) == DEFAULT_TOKEN_FILE
    assert token_file_for("default") == DEFAULT_TOKEN_FILE


def test_token_file_for_empty_is_default():
    assert token_file_for("") == DEFAULT_TOKEN_FILE


def test_token_file_for_main_is_default():
    # Hermes names the default profile ``main`` in a session key; it is never a person's own profile
    assert token_file_for("main") == DEFAULT_TOKEN_FILE


def test_token_file_for_named():
    assert token_file_for("sarah") == "/etc/durable-park/instances/sarah.token"
    assert INSTANCES_DIR == "/etc/durable-park/instances"


def test_token_file_for_uses_the_directory_and_default_it_is_given(tmp_path):
    assert token_file_for("sarah", instances_dir=str(tmp_path)) == f"{tmp_path}/sarah.token"
    assert token_file_for(None, default=str(tmp_path / "d")) == str(tmp_path / "d")


@pytest.mark.parametrize("bad", ["../x", "Sarah", "a" * 64, "x/y", "x y", "x.y", "-x", "_x", "sarah\n", " sarah",
                                 "sarah ", "sárah", "sárah", "x\x00y"])
def test_token_file_for_bad_name_raises(bad):
    with pytest.raises(ValueError):
        token_file_for(bad)


def test_the_error_never_carries_the_name():
    with pytest.raises(ValueError) as caught:
        token_file_for("../../etc/shadow")
    assert "shadow" not in str(caught.value)


@pytest.mark.parametrize("good", ["sarah", "tom", "a", "0", "a-b", "a_b", "a" * 63, "x9"])
def test_valid_profile_accepts(good):
    assert valid_profile(good)


@pytest.mark.parametrize("bad", ["", "default", "main", "Sarah", "a" * 64, "a/b", "a.b", "..", "a\n", None, 5, b"sarah"])
def test_valid_profile_refuses(bad):
    assert not valid_profile(bad)


@pytest.mark.parametrize("profile,expected", [(None, True), ("", True), ("default", True), ("sarah", False),
                                              ("main", True)])
def test_is_default_profile(profile, expected):
    assert is_default_profile(profile) is expected


def test_a_non_string_profile_is_refused_not_defaulted():
    with pytest.raises(ValueError):
        token_file_for(5)  # type: ignore[arg-type]


def test_the_receivers_copy_of_the_name_rule_is_the_same():
    """receiver/wake_post.py cannot import the plugin (it is copied alone), so it repeats the rule: keep them equal."""
    spec = importlib.util.spec_from_file_location("wake_post_for_keys_test", os.path.join(RECEIVER, "wake_post.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.PROFILE_PATTERN == keys.PROFILE_PATTERN
    assert set(module.RESERVED_PROFILES) == set(keys.RESERVED_PROFILES)
