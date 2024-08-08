# Copyright 2024 Pex project contributors.
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import

import inspect
import os
import re
import sys
from textwrap import dedent
from zipfile import is_zipfile

from pex.cli_util import prog_path
from pex.common import is_exe, pluralize
from pex.compatibility import commonpath
from pex.dist_metadata import Distribution
from pex.layout import Layout
from pex.pex_info import PexInfo
from pex.repl import custom
from pex.repl.custom import repl_loop
from pex.third_party.colors import color
from pex.typing import TYPE_CHECKING, cast
from pex.variables import ENV, Variables
from pex.version import __version__

if TYPE_CHECKING:
    from typing import Any, Callable, Dict, Optional, Sequence, Union

    import attr  # vendor:skip
else:
    from pex.third_party import attr


_PEX_CLI_NO_ARGS_IN_USE_ENV_VAR_NAME = "_PEX_CLI_NO_ARGS_IN_USE"


def export_pex_cli_no_args_use(env=None):
    # type: (Optional[Dict[str, str]]) -> Dict[str, str]
    """Records the fact that the Pex CLI executable is being run with no arguments."""

    _env = cast("Dict[str, str]", env or os.environ)
    if len(sys.argv) == 1:
        _env[_PEX_CLI_NO_ARGS_IN_USE_ENV_VAR_NAME] = sys.argv[0]
    return _env


def _pex_cli_no_args_in_use():
    # type: () -> Optional[str]
    return os.environ.pop(_PEX_CLI_NO_ARGS_IN_USE_ENV_VAR_NAME, None)


def _pex_cli_no_args_hint():
    # type: () -> Optional[str]

    pex_cli = _pex_cli_no_args_in_use()
    if not pex_cli or len(sys.argv) > 1:
        return None

    if is_exe(pex_cli):
        pex_cli_no_args = prog_path(pex_cli)
    else:
        pex_cli_no_args = "{python} {pex}".format(
            python=prog_path(sys.executable),
            pex=(
                prog_path(pex_cli)
                if is_zipfile(pex_cli)
                else "-m {module}".format(
                    module=re.sub(
                        r"^\.+",
                        "",
                        prog_path(os.path.dirname(pex_cli)).replace(os.path.sep, "."),
                    )
                )
            ),
        )
    return " Run `{pex} -h` for Pex CLI help.".format(pex=pex_cli_no_args)


def _create_pex_repl(
    banner,  # type: str
    pex_info,  # type: Union[str, Dict[str, Any]]
    pex_info_summary,  # type: str
    history=False,  # type: bool
    history_file=None,  # type: Optional[str]
):
    # type: (...) -> Callable[[], Dict[str, Any]]

    import json as stdlib_json

    def pex_info_func(json=False):
        # type: (bool) -> None
        """Print information about this PEX environment.

        :param json: `True` to print this PEX's PEX-INFO.
        """
        if json:
            if isinstance(pex_info, dict):
                pex_info_data = pex_info
            else:
                with open(pex_info) as fp:
                    pex_info_data = stdlib_json.load(fp)
            print(stdlib_json.dumps(pex_info_data, sort_keys=True, indent=2))
        else:
            print(pex_info_summary)

    return repl_loop(
        banner=banner,
        custom_commands={
            "pex_info": (
                pex_info_func,
                "Type pex_info() for information about this PEX, or pex_info(json=True) for even "
                "more details.",
            )
        },
        history=history,
        history_file=history_file,
    )


@attr.s(frozen=True)
class _REPLData(object):
    banner = attr.ib()  # type: str
    pex_info_summary = attr.ib()  # type: str


def _create_repl_data(
    pex_info,  # type: PexInfo
    requirements,  # type: Sequence[str]
    activated_dists,  # type: Sequence[Distribution]
    pex=sys.argv[0],  # type: str
    env=ENV,  # type: Variables
    venv=False,  # type: bool
):
    # type: (...) -> _REPLData

    layout = Layout.identify_original(env.PEX or pex)
    pex_root = os.path.abspath(env.PEX_ROOT)
    venv = venv or pex_info.venv
    venv_pex = venv and pex_root == commonpath((os.path.abspath(pex), pex_root))
    pex_prog_path = prog_path(env.PEX if venv_pex else pex)
    if venv and not venv_pex:
        pex_info_summary = [
            "Running in a PEX venv: {location}".format(location=os.path.dirname(pex_prog_path))
        ]
    else:
        venv_indicator = "--venv " if venv_pex else ""
        if Layout.ZIPAPP is layout:
            pex_type = "{venv}PEX file".format(venv=venv_indicator)
        else:
            pex_prog_path = (
                os.path.dirname(pex_prog_path) if os.path.isfile(pex_prog_path) else pex_prog_path
            )
            pex_type = "{layout} {venv}PEX directory".format(layout=layout, venv=venv_indicator)

        pex_info_summary = [
            "Running from {pex_type}: {location}".format(pex_type=pex_type, location=pex_prog_path)
        ]

    if activated_dists:
        req_count = len(requirements)
        dist_count = len(activated_dists)
        dep_info = "{req_count} {requirements} and {dist_count} activated {dists}.".format(
            req_count=req_count,
            requirements=pluralize(req_count, "requirement"),
            dist_count=dist_count,
            dists=pluralize(dist_count, "distribution"),
        )
        pex_info_summary.append("Requirements:")
        pex_info_summary.extend("  " + req for req in requirements)
        pex_info_summary.append("Activated Distributions:")
        pex_info_summary.extend("  " + os.path.basename(dist.location) for dist in activated_dists)
    else:
        dep_info = "no dependencies."

    if pex_info.includes_tools and (not venv or venv_pex):
        pex_info_summary.append(
            "This PEX includes tools. Exit the repl (type quit()) and run "
            "`PEX_TOOLS=1 {pex} -h` for tools help.".format(
                pex=pex_prog_path
                if Layout.ZIPAPP is layout
                else os.path.join(pex_prog_path, "__main__.py")
            )
        )

    color_style = dict(fg="yellow", style="negative")
    pex_header = color(
        "Pex {pex_version} hermetic environment with {dep_info}{maybe_pex_cli_no_args_hint}".format(
            pex_version=__version__,
            dep_info=dep_info,
            maybe_pex_cli_no_args_hint=_pex_cli_no_args_hint() or "",
        ),
        **color_style
    )
    more_info_footer = (
        'Type "help", "{pex_info}", "copyright", "credits" or "license" for more information.'
    ).format(pex_info=color("pex_info", **color_style))
    banner = (
        dedent(
            """\
            {pex_header}
            Python {python_version} on {platform}
            {more_info_footer}
            """
        )
        .format(
            pex_header=pex_header,
            python_version=sys.version,
            platform=sys.platform,
            more_info_footer=more_info_footer,
        )
        .strip()
    )

    return _REPLData(banner=banner, pex_info_summary=os.linesep.join(pex_info_summary))


def create_pex_repl_exe(
    shebang,  # type: str
    pex_info,  # type: PexInfo
    activated_dists,  # type: Sequence[Distribution]
    pex=sys.argv[0],  # type: str
    env=ENV,  # type: Variables
    venv=False,  # type: bool
):
    # type: (...) -> str

    repl_data = _create_repl_data(
        pex_info=pex_info,
        requirements=tuple(pex_info.requirements),
        activated_dists=activated_dists,
        pex=pex,
        env=env,
        venv=venv,
    )

    return dedent(
        """\
        {shebang}
        {custom_module}


        {create_pex_repl}


        _BANNER = {banner!r}
        _PEX_INFO_SUMMARY = {pex_info_summary!r}


        if __name__ == "__main__":
            import os
            import sys

            _create_pex_repl(
                banner=_BANNER,
                pex_info=os.path.join(os.path.dirname(__file__), "PEX-INFO"),
                pex_info_summary=_PEX_INFO_SUMMARY,
                history=os.environ.get("PEX_INTERPRETER_HISTORY", "0").lower() in ("1", "true"),
                history_file=os.environ.get("PEX_INTERPRETER_HISTORY_FILE")
            )()
        """
    ).format(
        shebang=shebang,
        custom_module=inspect.getsource(custom),
        create_pex_repl=inspect.getsource(_create_pex_repl),
        banner=repl_data.banner,
        pex_info_summary=repl_data.pex_info_summary,
    )


def create_pex_repl(
    pex_info,  # type: PexInfo
    requirements,  # type: Sequence[str]
    activated_dists,  # type: Sequence[Distribution]
    pex=sys.argv[0],  # type: str
    env=ENV,  # type: Variables
):
    # type: (...) -> Callable[[], Dict[str, Any]]

    repl_data = _create_repl_data(
        pex_info=pex_info,
        requirements=requirements,
        activated_dists=activated_dists,
        pex=pex,
        env=env,
    )
    return _create_pex_repl(
        banner=repl_data.banner,
        pex_info=pex_info.as_json_dict(),
        pex_info_summary=repl_data.pex_info_summary,
        history=env.PEX_INTERPRETER_HISTORY,
        history_file=env.PEX_INTERPRETER_HISTORY_FILE,
    )
