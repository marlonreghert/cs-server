"""Shared admin-config type predicates (app/services/config_validation.py).

The bool-vs-int subtlety these encode is the whole reason they are shared:
``bool`` subclasses ``int``, so every validator that hand-rolls the check
risks accepting ``true`` where a number is required.
"""
from app.services.config_validation import is_int, is_number, is_string_list


def test_is_number_accepts_ints_and_floats_but_not_bools():
    assert is_number(4) and is_number(4.5) and is_number(0) and is_number(-1.2)
    assert not is_number(True) and not is_number(False)
    assert not is_number("4") and not is_number(None) and not is_number([4])


def test_is_int_accepts_ints_but_not_bools_or_floats():
    assert is_int(0) and is_int(-3) and is_int(10)
    assert not is_int(True) and not is_int(False)
    assert not is_int(3.0) and not is_int("3") and not is_int(None)


def test_is_string_list():
    assert is_string_list([]) and is_string_list(["a", "b"])
    assert not is_string_list("ab") and not is_string_list(["a", 1])
    assert not is_string_list(None) and not is_string_list({"a": 1})
