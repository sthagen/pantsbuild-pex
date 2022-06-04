# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
import re
import shutil
import subprocess

import pytest

from pex.cli.testing import run_pex3
from pex.common import touch
from pex.testing import IS_PYPY, PY_VER, run_pex_command
from pex.typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Callable


@pytest.fixture
def ansicolors_1_1_8(clone):
    # type: (Callable[[str, str], str]) -> str
    return clone(
        "https://github.com/jonathaneunice/colors", "c965f5b9103c5bd32a1572adb8024ebe83278fb0"
    )


def test_fingerprint_stability(
    tmpdir,  # type: Any
    ansicolors_1_1_8,  # type: str
):
    # type: (...) -> None

    lock = os.path.join(str(tmpdir), "lock")
    pex_root = os.path.join(str(tmpdir), "pex_root")
    run_pex3(
        "lock", "create", "--pex-root", pex_root, ansicolors_1_1_8, "-o", lock
    ).assert_success()

    print_colors_version_args = [
        "--pex-root",
        pex_root,
        "--lock",
        lock,
        "--",
        "-c",
        "import colors; print(colors.__version__)",
    ]
    result = run_pex_command(args=print_colors_version_args)
    result.assert_success()
    assert "1.1.8" == result.output.strip()

    # Running the test suite generates .pyc files which should not count against the project
    # content hash.
    subprocess.check_call(
        args=[
            "tox",
            "-e",
            "{name}{major}{minor}".format(
                name="pypy" if IS_PYPY else "py", major=PY_VER[0], minor=PY_VER[1]
            ),
        ],
        cwd=ansicolors_1_1_8,
    )
    run_pex_command(args=print_colors_version_args).assert_success()

    # Touching a project file does not change its content and should not affect the project content
    # hash.
    colors_package_file = os.path.join(ansicolors_1_1_8, "colors", "__init__.py")
    touch(colors_package_file)
    run_pex_command(args=print_colors_version_args).assert_success()

    # Although the project content hash is modified, we can still satisfy the local project
    # requirement from the Pex cache.
    with open(colors_package_file, "a") as fp:
        fp.write("# Modified\n")
    run_pex_command(args=print_colors_version_args).assert_success()

    # With the Pex cache cleared, we should find the project content hash is now mismatched to the
    # lock.
    shutil.rmtree(pex_root)
    result = run_pex_command(args=print_colors_version_args)
    result.assert_failure()
    assert re.search(
        r"There was 1 error downloading required artifacts:\n"
        r"1\. ansicolors 1\.1\.8 from file://{project_dir}\n"
        r"    Expected sha256 hash of "
        r"fca533d24ea5fc1b0fc8bc10ee146535bddda1c40c85510544c5766cef4d10b6 when downloading "
        r"ansicolors but hashed to ".format(project_dir=ansicolors_1_1_8),
        result.error,
    ), result.error


def test_path_mappings(
    tmpdir,  # type: Any
    ansicolors_1_1_8,  # type: str
):
    # type: (...) -> None

    lock = os.path.join(str(tmpdir), "lock")
    pex_root = os.path.join(str(tmpdir), "pex_root")
    run_pex3(
        "lock",
        "create",
        "--pex-root",
        pex_root,
        "--path-mapping",
        "ansicolors|{path}|The local clone of the ansicolors project.".format(
            path=ansicolors_1_1_8
        ),
        ansicolors_1_1_8,
        "-o",
        lock,
    ).assert_success()

    new_path = os.path.join(str(tmpdir), "over", "here", "ansicolors")
    shutil.move(ansicolors_1_1_8, new_path)
    shutil.rmtree(pex_root)
    result = run_pex_command(
        args=[
            "--pex-root",
            pex_root,
            "--lock",
            lock,
            "--path-mapping",
            "ansicolors|{path}".format(path=new_path),
            "--",
            "-c",
            "import colors; print(colors.__version__)",
        ]
    )
    result.assert_success()
    assert "1.1.8" == result.output.strip()
