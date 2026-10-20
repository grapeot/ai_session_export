from __future__ import annotations

import os
import time


def pytest_configure(config) -> None:
    """Pin the suite to one timezone so date assertions are host-independent."""
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
