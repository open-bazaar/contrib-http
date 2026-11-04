"""Run package tests with blocking-call detection enabled."""

from __future__ import annotations

import sys
lazy import os
lazy from collections.abc import Iterator
lazy from tempfile import TemporaryDirectory

import pytest
lazy from blockbuster import BlockBuster, blockbuster_ctx


@pytest.fixture(autouse=True)
def blocking_calls() -> Iterator[BlockBuster]:
    with blockbuster_ctx() as detector:
        yield detector


if __name__ == "__main__":
    with TemporaryDirectory(
        prefix="bcn-", dir="/tmp" if os.name == "posix" else None
    ) as directory:
        os.environ["BCN_HOME"] = directory
        raise SystemExit(
            pytest.main(
                ["--basetemp", directory, *sys.argv[1:]],
                plugins=[sys.modules[__name__]],
            )
        )
