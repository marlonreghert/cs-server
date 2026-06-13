"""Offline guards for the 0010 reactivation migration.

The SQL runs only against real Postgres (validated post-provisioning), so these
tests pin the things a typo would silently break: the alembic chain, the scope
(eligibility_filter only — never Google permanently-closed), the cleared columns,
and the irreversible downgrade.
"""
import importlib.util
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0010_reactivate_eligibility_deprecated.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0010", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0010_reactivate_eligibility_deprecated"
    assert m.down_revision == "0009_eligibility_serving_view"


def test_reactivate_scope_is_eligibility_filter_only():
    sql = " ".join(_load().REACTIVATE.split())
    # Only eligibility_filter rows; Google permanently-closed must be untouched.
    assert "deprecated_source = 'eligibility_filter'" in sql
    assert "lifecycle_status = 'deprecated'" in sql  # WHERE guard
    assert "google_places" not in sql
    assert "google_business_status" not in sql       # closure flag left untouched


def test_clears_deprecation_columns_and_activates():
    sql = " ".join(_load().REACTIVATE.split())
    assert "lifecycle_status = 'active'" in sql
    for col in ("deprecated_reason = NULL", "deprecated_source = NULL", "deprecated_at = NULL"):
        assert col in sql, col


def test_downgrade_is_irreversible():
    with pytest.raises(RuntimeError):
        _load().downgrade()
