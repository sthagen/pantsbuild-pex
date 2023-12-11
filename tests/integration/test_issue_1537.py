# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os.path
import shutil
import subprocess

from pex.typing import TYPE_CHECKING
from testing import run_pex_command

if TYPE_CHECKING:
    from typing import Any, Callable, ContextManager, Tuple


def test_rel_cert_path(
    run_proxy,  # type: Callable[[], ContextManager[Tuple[int, str]]]
    tmpdir,  # type: Any
):
    # type: (...) -> None
    pex_file = os.path.join(str(tmpdir), "pex")
    with run_proxy() as (port, ca_cert):
        shutil.copy(ca_cert, "cert")
        run_pex_command(
            args=[
                "--proxy",
                "http://localhost:{port}".format(port=port),
                "--cert",
                "cert",
                # N.B.: The original issue (https://github.com/pantsbuild/pex/issues/1537) involved
                # avro-python3 1.10.0, but that distribution utilizes setup_requires which leads to
                # issues in CI for Mac. We use the Python 2/3 version of the same distribution
                # instead, which had setup_requires removed in
                # https://github.com/apache/avro/pull/818.
                "avro==1.10.0",
                "-o",
                pex_file,
            ]
        ).assert_success()
        subprocess.check_call(args=[pex_file, "-c", "import avro"])
