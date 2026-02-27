"""pytest configuration for the trading-engine test suite.

Problem solved here
-------------------
test_st_pullback_cci.py calls ``logging.disable(logging.CRITICAL)`` at
*module level* to suppress log noise during its own tests.  When pytest
runs multiple test files in the same process, that module-level call
disables logging globally – causing the ``assertLogs()`` checks inside
test_exit_logic.py to fail with
  "no logs of level INFO or higher triggered on root"

Fix
---
The autouse fixture below resets the logging disable level to NOTSET
(i.e., "nothing disabled") before every test, regardless of collection
order.  Tests that genuinely want to suppress output can still call
``logging.disable(logging.CRITICAL)`` inside their own body; the fixture
will undo it again before the next test.
"""

import logging

import pytest


@pytest.fixture(autouse=True)
def reset_logging_disable():
    """Restore logging to fully-enabled state before every test."""
    logging.disable(logging.NOTSET)
    yield
