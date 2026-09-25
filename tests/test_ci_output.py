"""Large test inputs must never become unbounded CI log lines."""
from types import SimpleNamespace

import pytest

from tests.conftest import MAX_TEST_ID_LENGTH, pytest_collection_modifyitems


def item(nodeid):
    return SimpleNamespace(nodeid=nodeid)


def test_collection_accepts_short_and_boundary_ids():
    pytest_collection_modifyitems([
        item("tests/test_list_refresh.py::test_bad_remote_hostlist_preserves_working_file[over-5MiB]"),
        item("x" * MAX_TEST_ID_LENGTH),
    ])


def test_collection_rejects_oversized_ids_with_bounded_diagnostic():
    huge = "tests/test_example.py::test_payload[" + "x" * (5 * 1024 * 1024) + "]"
    with pytest.raises(pytest.UsageError) as error:
        pytest_collection_modifyitems([item(huge)] * 10)
    message = str(error.value)
    assert "10 test IDs exceed" in message
    assert "pytest.param" in message
    assert str(len(huge)) in message
    assert len(message) < 1024
    assert "x" * 200 not in message


def test_all_collected_ids_are_bounded(request):
    assert all(len(test.nodeid) <= MAX_TEST_ID_LENGTH for test in request.session.items)
