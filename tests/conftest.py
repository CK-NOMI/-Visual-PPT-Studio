"""Fast deterministic unit tests: real Office rendering is separately exercised
by samples/seed_transformer.py and the end-to-end smoke script.
"""
import pytest

@pytest.fixture(autouse=True)
def avoid_slow_office_during_unit_tests(monkeypatch):
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
