"""Guards for the alembic_version widening migration (0011).

The ALTER runs against real Postgres in CI (alembic upgrade head); these offline
checks pin the chain and the widened width.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0011_widen_alembic_version.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0011", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0011_widen_alembic_version"
    assert m.down_revision == "0010_reactivate_eligibility"
    assert len(m.revision) <= 32  # must itself fit the (pre-widen) column


def test_widens_version_num_to_128():
    m = _load()
    assert "alembic_version" in m.WIDEN
    assert "version_num" in m.WIDEN
    assert "varchar(128)" in m.WIDEN
