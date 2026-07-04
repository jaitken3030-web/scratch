import json
from pathlib import Path

import pytest

from arbot.ledger import Ledger

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "test.db", tmp_path / "audit.log")
    yield led
    led.close()
