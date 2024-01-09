# Copyright 2024 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import atexit
import os
import shutil
import tempfile

from setuptools import setup  # type: ignore[import]  # The Python 2.7 type check can't see this.

_BUILD_DIR = None


def ensure_unique_build_dir():
    # type: () -> str
    global _BUILD_DIR
    if _BUILD_DIR is None:
        _build_dir = tempfile.mkdtemp(prefix="pex-dist-build.")
        atexit.register(shutil.rmtree, _build_dir, ignore_errors=True)
        _BUILD_DIR = _build_dir
    return _BUILD_DIR


def unique_build_dir(name):
    # type: (str) -> str
    path = os.path.join(ensure_unique_build_dir(), name)
    os.mkdir(path)
    return path


if __name__ == "__main__":
    setup(
        # The `egg_info --egg-base`, `build --build-base` and `bdist_wheel --bdist-dir` setup.py
        # sub-command options we pass below work around the otherwise default `<CWD>/build/`
        # directory for all three which defeats concurrency in tests.
        options={
            "egg_info": {"egg_base": unique_build_dir("egg_base")},
            "build": {"build_base": unique_build_dir("build_base")},
            "bdist_wheel": {"bdist_dir": unique_build_dir("bdist_dir")},
        },
        # This supports expanding the supported Python range via the _PEX_REQUIRES_PYTHON env var
        # for testing unreleased Pythons.
        python_requires=os.environ.get("_PEX_REQUIRES_PYTHON"),
    )
