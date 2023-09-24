# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
import subprocess
from textwrap import dedent

import pytest
from colors import colors

from pex.typing import TYPE_CHECKING
from testing import IntegResults, run_pex_command

if TYPE_CHECKING:
    from typing import Any, List


# N.B.: To test that we are running a REPL we'll show that it computes result even after
# assertion errors and exceptions that would normally halt a script.
# To check that it forwards python options we use -O to deactivate asserts
# See: https://docs.python.org/3/using/cmdline.html#cmdoption-O
@pytest.mark.parametrize(
    "execution_mode_args",
    [
        pytest.param([], id="ZIPAPP"),
        pytest.param(["--venv"], id="VENV"),
    ],
)
def test_repl_python_options(
    execution_mode_args,  # type: List[str]
    tmpdir,  # type: Any
):
    # type: (...) -> None

    pex = os.path.join(str(tmpdir), "pex")
    run_pex_command(args=["ansicolors==1.1.8", "-o", pex] + execution_mode_args).assert_success()

    repl_commands = dedent(
        """
        import colors
        assert False
        raise Exception("customexc")
        result = 20 + 103
        print(colors.green("Worked: {}".format(result)))
        quit()
        """
    )

    def execute_pex(disable_assertions):
        # type: (bool) -> IntegResults
        args = [pex]
        if disable_assertions:
            args.append("-O")
        process = subprocess.Popen(
            args=args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(input=repl_commands.encode("utf-8"))
        return IntegResults(
            output=stdout.decode("utf-8"),
            error=stderr.decode("utf-8"),
            return_code=process.returncode,
        )

    # The assertion will fail and print but since it is a REPL it will keep going
    # and compute the result
    result = execute_pex(disable_assertions=False)
    result.assert_success()
    assert "InteractiveConsole" in result.error
    assert "AssertionError" in result.error
    assert "customexc" in result.error
    assert colors.green("Worked: 123") in result.output

    # The -O will disable the assertion, but the regular exception will still get raised.
    result = execute_pex(disable_assertions=True)
    result.assert_success()
    assert "InteractiveConsole" in result.error
    assert "AssertionError" not in result.error
    assert "customexc" in result.error
    assert colors.green("Worked: 123") in result.output
