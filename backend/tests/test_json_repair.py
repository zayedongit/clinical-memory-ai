"""JSON repair for models that promise strict JSON and occasionally do not.

The rule this file exists to enforce: repairing *structure* is allowed,
inventing *content* is not. A truncated field is dropped; it is never guessed.
"""
from __future__ import annotations

import json

import pytest

from app.ai import json_repair


def test_clean_json_needs_no_repair():
    r = json_repair.parse('{"a": 1, "b": "two"}')
    assert r.data == {"a": 1, "b": "two"}
    assert r.repaired is False


@pytest.mark.parametrize("wrapped", [
    '```json\n{"a": 1}\n```',
    '```\n{"a": 1}\n```',
    '```JSON\n{"a": 1}\n```',
])
def test_markdown_fences_are_stripped(wrapped):
    assert json_repair.parse(wrapped).data == {"a": 1}


def test_leading_prose_before_the_object_is_discarded():
    r = json_repair.parse('Here is the note you asked for:\n{"assessment": "URTI"}')
    assert r.data == {"assessment": "URTI"}
    assert r.repaired is True


# --------------------------------------------------------------------- #
# Truncation — the failure that happens when the model hits its token cap
# --------------------------------------------------------------------- #
def test_truncated_nested_object_is_closed():
    r = json_repair.parse('{"soap": {"subjective": "cough", "objective": "chest clear"')
    assert r.repaired is True
    assert r.data["soap"]["subjective"] == "cough"


def test_truncated_mid_string_keeps_what_was_received():
    r = json_repair.parse('{"hpi": "patient reports cough since')
    assert r.data["hpi"] == "patient reports cough since"


def test_truncated_array_is_closed():
    r = json_repair.parse('{"symptoms": ["cough", "fever"')
    assert r.data["symptoms"] == ["cough", "fever"]


def test_dangling_key_is_dropped_not_guessed():
    """A key with no value must disappear, not acquire an invented one."""
    r = json_repair.parse('{"hpi": "cough", "allergies":')
    assert r.data == {"hpi": "cough"}
    assert "allergies" not in r.data


def test_trailing_comma_is_removed():
    r = json_repair.parse('{"a": 1, "b": 2,')
    assert r.data == {"a": 1, "b": 2}


def test_braces_inside_clinical_free_text_do_not_confuse_the_brace_counter():
    """Naive brace counting breaks on any brace inside a string value.

    Clinical free text contains them often enough (dose notations, quoted
    speech) that a counting bug would corrupt real notes.
    """
    payload = '{"plan": "Tab Paracetamol {500mg} BD", "note": "said \\"it hurts\\""'
    r = json_repair.parse(payload)
    assert r.data["plan"] == "Tab Paracetamol {500mg} BD"
    assert r.data["note"] == 'said "it hurts"'


def test_escaped_quote_at_the_truncation_point_is_handled():
    r = json_repair.parse(r'{"note": "the patient said \"')
    assert r.data is not None


# --------------------------------------------------------------------- #
# Genuinely unrecoverable input
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "",
    "   ",
    "I'm sorry, I cannot help with that request.",
    "null",
    "[1, 2, 3]",
])
def test_non_objects_and_prose_return_nothing(text):
    r = json_repair.parse(text)
    assert r.data is None
    assert not r


def test_result_is_falsy_when_parsing_failed():
    assert not json_repair.parse("nonsense")
    assert json_repair.parse('{"a":1}')


# --------------------------------------------------------------------- #
# Property: repair never invents a key
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("cut", range(10, 120, 7))
def test_repair_never_adds_a_key_that_was_not_present(cut):
    """Truncate a real note at every offset and check the invariant.

    Whatever comes back must be a subset of the original object's keys with
    unchanged values — repair may lose the tail, never fabricate it.
    """
    original = {
        "hpi": "cough and fever for three days",
        "allergies": "no known drug allergies",
        "vitals": {"bp": "138/86", "hr": "92"},
        "symptoms": ["cough", "fever", "sore throat"],
    }
    text = json.dumps(original)
    r = json_repair.parse(text[:cut])
    if r.data is None:
        return
    for key, value in r.data.items():
        assert key in original, f"repair invented key {key!r}"
        if isinstance(value, str) and isinstance(original[key], str):
            assert original[key].startswith(value), "repair altered a value"
        elif not isinstance(value, (dict, list)):
            assert value == original[key]
