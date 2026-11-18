import os

import oracledb
import pytest

import granary

oracledb.defaults.fetch_decimals = True


@pytest.fixture(scope="session")
def cfg():
    return granary.load_config()


@pytest.fixture(scope="session")
def conn(cfg):
    if not os.environ.get(cfg["password_env"]):
        pytest.skip(f"{cfg['password_env']} is not set, database tests need a granary schema")
    with granary.connect(cfg) as connection:
        yield connection
