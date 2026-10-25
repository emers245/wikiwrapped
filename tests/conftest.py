"""Test-wide safety net: no test can read or write real user data.

The visit log in the user-data dir is irreplaceable (D9), and most modules
default to config's paths whenever a test forgets to pass one. So before
any test module imports config, the dev override points the whole user-data
dir at a throwaway directory.

This has to happen at import time of this file, not in a fixture: config
resolves its paths once, when it is first imported, and pytest imports
conftest.py before it collects (and so imports) any test module.
pytest_sessionstart then checks that it worked, so a future change in
import order fails loudly instead of quietly touching real data.
"""

import os
import shutil
import tempfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="wikiwrapped-tests-")).resolve()
os.environ["WIKIPEDIA_WRAPPED_DATA_DIR"] = str(_SCRATCH)
# A developer's own contact address must not leak into User-Agent tests.
os.environ.pop("WIKIPEDIA_WRAPPED_EMAIL", None)


def pytest_sessionstart(session):
    import config

    if Path(config.USER_DATA_DIR).resolve() != _SCRATCH:
        raise RuntimeError(
            f"config.USER_DATA_DIR is {config.USER_DATA_DIR}, not the test "
            f"scratch dir {_SCRATCH}: config was imported before conftest.py "
            "set the override. Refusing to run against real user data."
        )


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SCRATCH, ignore_errors=True)
