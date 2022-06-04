# coding=utf-8
# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, print_function

import os
import re
import subprocess
import sys
from collections import deque
from textwrap import dedent

from pex import dist_metadata, targets, third_party
from pex.auth import PasswordEntry
from pex.common import atomic_directory, safe_mkdir, safe_mkdtemp
from pex.compatibility import urlparse
from pex.interpreter import PythonInterpreter
from pex.jobs import Job
from pex.network_configuration import NetworkConfiguration
from pex.pep_376 import Record
from pex.pep_425 import CompatibilityTags
from pex.pex import PEX
from pex.pex_bootstrapper import ensure_venv
from pex.pip import foreign_platform
from pex.pip.download_observer import DownloadObserver
from pex.pip.log_analyzer import ErrorAnalyzer, ErrorMessage, LogAnalyzer, LogScrapeJob
from pex.platforms import Platform
from pex.resolve.resolver_configuration import ResolverVersion
from pex.targets import LocalInterpreter, Target
from pex.third_party import isolated
from pex.tracer import TRACER
from pex.typing import TYPE_CHECKING
from pex.util import named_temporary_file
from pex.variables import ENV

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        Dict,
        Iterable,
        Iterator,
        List,
        Mapping,
        Optional,
        Sequence,
        Text,
        Tuple,
    )

    import attr  # vendor:skip
else:
    from pex.third_party import attr


class PackageIndexConfiguration(object):
    @staticmethod
    def _calculate_args(
        indexes=None,  # type: Optional[Sequence[str]]
        find_links=None,  # type: Optional[Iterable[str]]
        network_configuration=None,  # type: Optional[NetworkConfiguration]
    ):
        # type: (...) -> Iterator[str]

        # N.B.: `--cert` and `--client-cert` are passed via env var to work around:
        #   https://github.com/pypa/pip/issues/5502
        # See `_calculate_env`.

        trusted_hosts = []

        def maybe_trust_insecure_host(url):
            url_info = urlparse.urlparse(url)
            if "http" == url_info.scheme:
                # Implicitly trust explicitly asked for http indexes and find_links repos instead of
                # requiring separate trust configuration.
                trusted_hosts.append(url_info.netloc)
            return url

        # N.B.: We interpret None to mean accept pip index defaults, [] to mean turn off all index
        # use.
        if indexes is not None:
            if len(indexes) == 0:
                yield "--no-index"
            else:
                all_indexes = deque(indexes)
                yield "--index-url"
                yield maybe_trust_insecure_host(all_indexes.popleft())
                if all_indexes:
                    for extra_index in all_indexes:
                        yield "--extra-index-url"
                        yield maybe_trust_insecure_host(extra_index)

        if find_links:
            for find_link_url in find_links:
                yield "--find-links"
                yield maybe_trust_insecure_host(find_link_url)

        for trusted_host in trusted_hosts:
            yield "--trusted-host"
            yield trusted_host

        network_configuration = network_configuration or NetworkConfiguration()

        yield "--retries"
        yield str(network_configuration.retries)

        yield "--timeout"
        yield str(network_configuration.timeout)

    @staticmethod
    def _calculate_env(
        network_configuration,  # type: NetworkConfiguration
        isolated,  # type: bool
    ):
        # type: (...) -> Iterator[Tuple[str, str]]
        if network_configuration.proxy:
            # We use the backdoor of the universality of http(s)_proxy env var support to continue
            # to allow Pip to operate in `--isolated` mode.
            yield "http_proxy", network_configuration.proxy
            yield "https_proxy", network_configuration.proxy

        if network_configuration.cert:
            # We use the backdoor of requests (which is vendored by Pip to handle all network
            # operations) support for REQUESTS_CA_BUNDLE when possible to continue to allow Pip to
            # operate in `--isolated` mode.
            yield (
                ("REQUESTS_CA_BUNDLE" if isolated else "PIP_CERT"),
                os.path.abspath(network_configuration.cert),
            )

        if network_configuration.client_cert:
            assert not isolated
            yield "PIP_CLIENT_CERT", os.path.abspath(network_configuration.client_cert)

    @classmethod
    def create(
        cls,
        resolver_version=None,  # type: Optional[ResolverVersion.Value]
        indexes=None,  # type: Optional[Sequence[str]]
        find_links=None,  # type: Optional[Iterable[str]]
        network_configuration=None,  # type: Optional[NetworkConfiguration]
        password_entries=(),  # type: Iterable[PasswordEntry]
    ):
        # type: (...) -> PackageIndexConfiguration
        resolver_version = resolver_version or ResolverVersion.PIP_LEGACY
        network_configuration = network_configuration or NetworkConfiguration()

        # We must pass `--client-cert` via PIP_CLIENT_CERT to work around
        # https://github.com/pypa/pip/issues/5502. We can only do this by breaking Pip `--isolated`
        # mode.
        isolated = not network_configuration.client_cert

        return cls(
            resolver_version=resolver_version,
            network_configuration=network_configuration,
            args=cls._calculate_args(
                indexes=indexes, find_links=find_links, network_configuration=network_configuration
            ),
            env=cls._calculate_env(network_configuration=network_configuration, isolated=isolated),
            isolated=isolated,
            password_entries=password_entries,
        )

    def __init__(
        self,
        resolver_version,  # type: ResolverVersion.Value
        network_configuration,  # type: NetworkConfiguration
        args,  # type: Iterable[str]
        env,  # type: Iterable[Tuple[str, str]]
        isolated,  # type: bool
        password_entries=(),  # type: Iterable[PasswordEntry]
    ):
        # type: (...) -> None
        self.resolver_version = resolver_version  # type: ResolverVersion.Value
        self.network_configuration = network_configuration  # type: NetworkConfiguration
        self.args = tuple(args)  # type: Iterable[str]
        self.env = dict(env)  # type: Mapping[str, str]
        self.isolated = isolated  # type: bool
        self.password_entries = password_entries  # type: Iterable[PasswordEntry]


if TYPE_CHECKING:
    from pex.pip.log_analyzer import ErrorAnalysis


@attr.s
class _Issue9420Analyzer(ErrorAnalyzer):
    # Works around: https://github.com/pypa/pip/issues/9420

    _strip = attr.ib(default=None)  # type: Optional[int]

    def analyze(self, line):
        # type: (str) -> ErrorAnalysis
        # N.B.: Pip --log output looks like:
        # 2021-01-04T16:12:01,119 ERROR: Cannot install pantsbuild-pants==1.24.0.dev2 and wheel==0.33.6 because these package versions have conflicting dependencies.
        # 2021-01-04T16:12:01,119
        # 2021-01-04T16:12:01,119 The conflict is caused by:
        # 2021-01-04T16:12:01,119     The user requested wheel==0.33.6
        # 2021-01-04T16:12:01,119     pantsbuild-pants 1.24.0.dev2 depends on wheel==0.31.1
        # 2021-01-04T16:12:01,119
        # 2021-01-04T16:12:01,119 To fix this you could try to:
        # 2021-01-04T16:12:01,119 1. loosen the range of package versions you've specified
        # 2021-01-04T16:12:01,119 2. remove package versions to allow pip attempt to solve the dependency conflict
        # 2021-01-04T16:12:01,119 ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/user_guide/#fixing-conflicting-dependencies
        if not self._strip:
            match = re.match(r"^(?P<timestamp>[^ ]+) ERROR: Cannot install ", line)
            if match:
                self._strip = len(match.group("timestamp"))
        else:
            match = re.match(r"^[^ ]+ ERROR: ResolutionImpossible: ", line)
            if match:
                return self.Complete()
            else:
                return self.Continue(ErrorMessage(line[self._strip :]))
        return self.Continue()


@attr.s(frozen=True)
class Pip(object):
    _PATCHES_MODULE_ENV_VAR_NAME = "_PEX_PIP_RUNTIME_PATCHES"

    @classmethod
    def _patch_code(cls, code):
        # type: (Text) -> Mapping[str, str]
        patches_dir = safe_mkdtemp()
        patches_module = "_pex_pip_patches"
        python_file = "{patches_module}.py".format(patches_module=patches_module)
        with open(os.path.join(patches_dir, python_file), "wb") as code_fp:
            code_fp.write(code.encode("utf-8"))
        return {"PEX_EXTRA_SYS_PATH": patches_dir, cls._PATCHES_MODULE_ENV_VAR_NAME: patches_module}

    @classmethod
    def create(
        cls,
        path,  # type: str
        interpreter=None,  # type: Optional[PythonInterpreter]
    ):
        # type: (...) -> Pip
        """Creates a pip tool with PEX isolation at path.

        :param path: The path to assemble the pip tool at.
        :param interpreter: The interpreter to run Pip with. The current interpreter by default.
        :return: The path of a PEX that can be used to execute Pip in isolation.
        """
        pip_interpreter = interpreter or PythonInterpreter.get()
        pip_pex_path = os.path.join(path, isolated().pex_hash)
        with atomic_directory(pip_pex_path, exclusive=True) as chroot:
            if not chroot.is_finalized():
                from pex.pex_builder import PEXBuilder

                isolated_pip_builder = PEXBuilder(path=chroot.work_dir)
                isolated_pip_builder.info.venv = True
                for dist_location in third_party.expose(["pip", "setuptools", "wheel"]):
                    isolated_pip_builder.add_dist_location(dist=dist_location)
                with named_temporary_file(prefix="", suffix=".py", mode="w") as fp:
                    fp.write(
                        dedent(
                            """\
                            import os
                            import runpy
                            
                            patches_module = os.environ.pop({patches_module_env_var_name!r}, None)
                            if patches_module:
                                # Apply runtime patches to Pip to work around issues or else bend
                                # Pip to Pex's needs.
                                __import__(patches_module)
                            
                            runpy.run_module(mod_name="pip", run_name="__main__", alter_sys=True)
                            """
                        ).format(patches_module_env_var_name=cls._PATCHES_MODULE_ENV_VAR_NAME)
                    )
                    fp.close()
                    isolated_pip_builder.set_executable(fp.name, "__pex_patched_pip__.py")
                isolated_pip_builder.freeze()
        return cls(ensure_venv(PEX(pip_pex_path, interpreter=pip_interpreter)))

    _pip_pex_path = attr.ib()  # type: str

    @staticmethod
    def _calculate_resolver_version(package_index_configuration=None):
        # type: (Optional[PackageIndexConfiguration]) -> ResolverVersion.Value
        return (
            package_index_configuration.resolver_version
            if package_index_configuration
            else ResolverVersion.PIP_LEGACY
        )

    @classmethod
    def _calculate_resolver_version_args(
        cls,
        interpreter,  # type: PythonInterpreter
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
    ):
        # type: (...) -> Iterator[str]
        resolver_version = cls._calculate_resolver_version(
            package_index_configuration=package_index_configuration
        )
        # N.B.: The pip default resolver depends on the python it is invoked with. For Python 2.7
        # Pip defaults to the legacy resolver and for Python 3 Pip defaults to the 2020 resolver.
        # Further, Pip warns when you do not use the default resolver version for the interpreter
        # in play. To both avoid warnings and set the correct resolver version, we need
        # to only set the resolver version when it's not the default for the interpreter in play:
        if resolver_version == ResolverVersion.PIP_2020 and interpreter.version[0] == 2:
            yield "--use-feature"
            yield "2020-resolver"
        elif resolver_version == ResolverVersion.PIP_LEGACY and interpreter.version[0] == 3:
            yield "--use-deprecated"
            yield "legacy-resolver"

    def _spawn_pip_isolated(
        self,
        args,  # type: Iterable[str]
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
        interpreter=None,  # type: Optional[PythonInterpreter]
        pip_verbosity=0,  # type: int
        extra_env=None,  # type: Optional[Dict[str, str]]
        **popen_kwargs  # type: Any
    ):
        # type: (...) -> Tuple[List[str], subprocess.Popen]
        pip_args = [
            # We vendor the version of pip we want so pip should never check for updates.
            "--disable-pip-version-check",
            # If we want to warn about a version of python we support, we should do it, not pip.
            "--no-python-version-warning",
            # If pip encounters a duplicate file path during its operations we don't want it to
            # prompt and we'd also like to know about this since it should never occur. We leverage
            # the pip global option:
            # --exists-action <action>
            #   Default action when a path already exists: (s)witch, (i)gnore, (w)ipe, (b)ackup,
            #   (a)bort.
            "--exists-action",
            "a",
        ]
        python_interpreter = interpreter or PythonInterpreter.get()
        pip_args.extend(
            self._calculate_resolver_version_args(
                python_interpreter, package_index_configuration=package_index_configuration
            )
        )
        if not package_index_configuration or package_index_configuration.isolated:
            # Don't read PIP_ environment variables or pip configuration files like
            # `~/.config/pip/pip.conf`.
            pip_args.append("--isolated")

        # The max pip verbosity is -vvv and for pex it's -vvvvvvvvv; so we scale down by a factor
        # of 3.
        pip_verbosity = pip_verbosity or (ENV.PEX_VERBOSE // 3)
        if pip_verbosity > 0:
            pip_args.append("-{}".format("v" * pip_verbosity))
        else:
            pip_args.append("-q")

        pip_cache = os.path.join(ENV.PEX_ROOT, "pip_cache")
        pip_args.extend(["--cache-dir", pip_cache])

        command = pip_args + list(args)

        # N.B.: Package index options in Pep always have the same option names, but they are
        # registered as subcommand-specific, so we must append them here _after_ the pip subcommand
        # specified in `args`.
        if package_index_configuration:
            command.extend(package_index_configuration.args)

        extra_env = extra_env or {}
        if package_index_configuration:
            extra_env.update(package_index_configuration.env)

        # Ensure the pip cache (`http/` and `wheels/` dirs) is housed in the same partition as the
        # temporary directories it creates. This is needed to ensure atomic filesystem operations
        # since Pip relies upon `shutil.move` which is only atomic when `os.rename` can be used.
        # See https://github.com/pantsbuild/pex/issues/1776 for an example of the issues non-atomic
        # moves lead to in the `pip wheel` case.
        pip_tmpdir = os.path.join(pip_cache, ".tmp")
        safe_mkdir(pip_tmpdir)
        extra_env.update(TMPDIR=pip_tmpdir)

        with ENV.strip().patch(
            PEX_ROOT=ENV.PEX_ROOT,
            PEX_VERBOSE=str(ENV.PEX_VERBOSE),
            __PEX_UNVENDORED__="1",
            **extra_env
        ) as env:
            # Guard against API calls from environment with ambient PYTHONPATH preventing pip PEX
            # bootstrapping. See: https://github.com/pantsbuild/pex/issues/892
            pythonpath = env.pop("PYTHONPATH", None)
            if pythonpath:
                TRACER.log(
                    "Scrubbed PYTHONPATH={} from the pip PEX environment.".format(pythonpath), V=3
                )

            # Pip has no discernible stdout / stderr discipline with its logging. Pex guarantees
            # stdout will only contain usable (parseable) data and all logging will go to stderr.
            # To uphold the Pex standard, force Pip to comply by re-directing stdout to stderr.
            #
            # See:
            # + https://github.com/pantsbuild/pex/issues/1267
            # + https://github.com/pypa/pip/issues/9420
            if "stdout" not in popen_kwargs:
                popen_kwargs["stdout"] = sys.stderr.fileno()

            args = [self._pip_pex_path] + command
            return args, subprocess.Popen(args=args, env=env, **popen_kwargs)

    def _spawn_pip_isolated_job(
        self,
        args,  # type: Iterable[str]
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
        interpreter=None,  # type: Optional[PythonInterpreter]
        pip_verbosity=0,  # type: int
        finalizer=None,  # type: Optional[Callable[[int], None]]
        extra_env=None,  # type: Optional[Dict[str, str]]
        **popen_kwargs  # type: Any
    ):
        # type: (...) -> Job
        command, process = self._spawn_pip_isolated(
            args,
            package_index_configuration=package_index_configuration,
            interpreter=interpreter,
            pip_verbosity=pip_verbosity,
            extra_env=extra_env,
            **popen_kwargs
        )
        return Job(command=command, process=process, finalizer=finalizer)

    def spawn_download_distributions(
        self,
        download_dir,  # type: str
        requirements=None,  # type: Optional[Iterable[str]]
        requirement_files=None,  # type: Optional[Iterable[str]]
        constraint_files=None,  # type: Optional[Iterable[str]]
        allow_prereleases=False,  # type: bool
        transitive=True,  # type: bool
        target=None,  # type: Optional[Target]
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
        build=True,  # type: bool
        use_wheel=True,  # type: bool
        prefer_older_binary=False,  # type: bool
        use_pep517=None,  # type: Optional[bool]
        build_isolation=True,  # type: bool
        observer=None,  # type: Optional[DownloadObserver]
    ):
        # type: (...) -> Job
        target = target or targets.current()

        if not use_wheel:
            if not build:
                raise ValueError(
                    "Cannot both ignore wheels (use_wheel=False) and refrain from building "
                    "distributions (build=False)."
                )
            elif not isinstance(target, LocalInterpreter):
                raise ValueError(
                    "Cannot ignore wheels (use_wheel=False) when resolving for a platform: "
                    "{}".format(target.platform)
                )

        download_cmd = ["download", "--dest", download_dir]
        extra_env = {}  # type: Dict[str, str]

        if not isinstance(target, LocalInterpreter) or not build:
            # If we're not targeting a local interpreter, we can't build wheels from sdists.
            download_cmd.extend(["--only-binary", ":all:"])

        if not use_wheel:
            download_cmd.extend(["--no-binary", ":all:"])

        if prefer_older_binary:
            download_cmd.append("--prefer-binary")

        if use_pep517 is not None:
            download_cmd.append("--use-pep517" if use_pep517 else "--no-use-pep517")

        if not build_isolation:
            download_cmd.append("--no-build-isolation")
            extra_env.update(PEP517_BACKEND_PATH=os.pathsep.join(sys.path))

        if allow_prereleases:
            download_cmd.append("--pre")

        if not transitive:
            download_cmd.append("--no-deps")

        if requirement_files:
            for requirement_file in requirement_files:
                download_cmd.extend(["--requirement", requirement_file])

        if constraint_files:
            for constraint_file in constraint_files:
                download_cmd.extend(["--constraint", constraint_file])

        if requirements:
            download_cmd.extend(requirements)

        foreign_platform_observer = foreign_platform.patch(target)
        if (
            foreign_platform_observer
            and foreign_platform_observer.patch.code
            and observer
            and observer.patch.code
        ):
            raise ValueError(
                "Can only have one patch for Pip code, but, in addition to patching for a foreign "
                "platform, asked to patch code for {observer}.".format(observer=observer)
            )

        log_analyzers = []  # type: List[LogAnalyzer]
        code = None  # type: Optional[Text]
        for obs in (foreign_platform_observer, observer):
            if obs:
                log_analyzers.append(obs.analyzer)
                download_cmd.extend(obs.patch.args)
                extra_env.update(obs.patch.env)
                code = code or obs.patch.code

        if code:
            extra_env.update(self._patch_code(code))

        # The Pip 2020 resolver hides useful dependency conflict information in stdout interspersed
        # with other information we want to suppress. We jump though some hoops here to get at that
        # information and surface it on stderr. See: https://github.com/pypa/pip/issues/9420.
        if (
            self._calculate_resolver_version(
                package_index_configuration=package_index_configuration
            )
            == ResolverVersion.PIP_2020
        ):
            log_analyzers.append(_Issue9420Analyzer())

        log = None
        popen_kwargs = {}
        if log_analyzers:
            log = os.path.join(safe_mkdtemp(prefix="pex-pip-log"), "pip.log")
            download_cmd = ["--log", log] + download_cmd
            # N.B.: The `pip -q download ...` command is quiet but
            # `pip -q --log log.txt download ...` leaks download progress bars to stdout. We work
            # around this by sending stdout to the bit bucket.
            popen_kwargs["stdout"] = open(os.devnull, "wb")

        command, process = self._spawn_pip_isolated(
            download_cmd,
            package_index_configuration=package_index_configuration,
            interpreter=target.get_interpreter(),
            pip_verbosity=0,
            extra_env=extra_env,
            **popen_kwargs
        )
        if log:
            return LogScrapeJob(command, process, log, log_analyzers)
        else:
            return Job(command, process)

    def spawn_build_wheels(
        self,
        distributions,  # type: Iterable[str]
        wheel_dir,  # type: str
        interpreter=None,  # type: Optional[PythonInterpreter]
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
        prefer_older_binary=False,  # type: bool
        use_pep517=None,  # type: Optional[bool]
        build_isolation=True,  # type: bool
        verify=True,  # type: bool
    ):
        # type: (...) -> Job
        wheel_cmd = ["wheel", "--no-deps", "--wheel-dir", wheel_dir]
        extra_env = {}  # type: Dict[str, str]

        # It's not clear if Pip's implementation of PEP-517 builds respects this option for
        # resolving build dependencies, but in case it is we pass it.
        if use_pep517 is not False and prefer_older_binary:
            wheel_cmd.append("--prefer-binary")

        if use_pep517 is not None:
            wheel_cmd.append("--use-pep517" if use_pep517 else "--no-use-pep517")

        if not build_isolation:
            wheel_cmd.append("--no-build-isolation")
            interpreter = interpreter or PythonInterpreter.get()
            extra_env.update(PEP517_BACKEND_PATH=os.pathsep.join(interpreter.sys_path))

        if not verify:
            wheel_cmd.append("--no-verify")

        wheel_cmd.extend(distributions)

        return self._spawn_pip_isolated_job(
            wheel_cmd,
            # If the build leverages PEP-518 it will need to resolve build requirements.
            package_index_configuration=package_index_configuration,
            interpreter=interpreter,
            extra_env=extra_env,
        )

    def spawn_install_wheel(
        self,
        wheel,  # type: str
        install_dir,  # type: str
        compile=False,  # type: bool
        target=None,  # type: Optional[Target]
    ):
        # type: (...) -> Job

        project_name_and_version = dist_metadata.project_name_and_version(wheel)
        assert project_name_and_version is not None, (
            "Should never fail to parse a wheel path into a project name and version, but "
            "failed to parse these from: {wheel}".format(wheel=wheel)
        )

        target = target or targets.current()
        interpreter = target.get_interpreter()
        if target.is_foreign:
            if compile:
                raise ValueError(
                    "Cannot compile bytecode for {} using {} because the wheel has a foreign "
                    "platform.".format(wheel, interpreter)
                )

        install_cmd = [
            "install",
            "--no-deps",
            "--no-index",
            "--only-binary",
            ":all:",
            # In `--prefix` scheme, Pip warns about installed scripts not being on $PATH. We fix
            # this when a PEX is turned into a venv.
            "--no-warn-script-location",
            # In `--prefix` scheme, Pip normally refuses to install a dependency already in the
            # `sys.path` of Pip itself since the requirement is already satisfied. Since `pip`,
            # `setuptools` and `wheel` are always in that `sys.path` (Our `pip.pex` venv PEX), we
            # force installation so that PEXes with dependencies on those projects get them properly
            # installed instead of skipped.
            "--force-reinstall",
            "--ignore-installed",
            # We're potentially installing a wheel for a foreign platform. This is just an
            # unpacking operation though; so we don't actually need to perform it with a target
            # platform compatible interpreter (except for scripts - which we deal with in fixup
            # install below).
            "--ignore-requires-python",
            "--prefix",
            install_dir,
        ]

        # The `--prefix` scheme causes Pip to refuse to install foreign wheels. It assumes those
        # wheels must be compatible with the current venv. Since we just install wheels in
        # individual chroots for later re-assembly on the `sys.path` at runtime or at venv install
        # time, we override this concern by forcing the wheel's tags to be considered compatible
        # with the current Pip install interpreter being used.
        compatible_tags = CompatibilityTags.from_wheel(wheel).extend(
            interpreter.identity.supported_tags
        )
        patch = foreign_platform.patch_tags(compatible_tags)
        extra_env = dict(patch.env)
        if patch.code:
            extra_env.update(self._patch_code(patch.code))
        install_cmd.extend(patch.args)
        install_cmd.append("--compile" if compile else "--no-compile")
        install_cmd.append(wheel)

        def fixup_install(returncode):
            if returncode != 0:
                return
            record = Record.from_prefix_install(
                prefix_dir=install_dir,
                project_name=project_name_and_version.project_name,
                version=project_name_and_version.version,
            )
            record.fixup_install(interpreter=interpreter)

        return self._spawn_pip_isolated_job(
            args=install_cmd,
            interpreter=interpreter,
            finalizer=fixup_install,
            extra_env=extra_env,
        )

    def spawn_debug(
        self,
        platform,  # type: Platform
        manylinux=None,  # type: Optional[str]
    ):
        # type: (...) -> Job

        # N.B.: Pip gives fair warning:
        #   WARNING: This command is only meant for debugging. Do not use this with automation for
        #   parsing and getting these details, since the output and options of this command may
        #   change without notice.
        #
        # We suppress the warning by capturing stderr below. The information there will be dumped
        # only if the Pip command fails, which is what we want.

        debug_command = ["debug"]
        debug_command.extend(foreign_platform.iter_platform_args(platform, manylinux=manylinux))
        return self._spawn_pip_isolated_job(
            debug_command, pip_verbosity=1, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )


_PIP = {}  # type: Dict[Optional[PythonInterpreter], Pip]


def get_pip(interpreter=None):
    # type: (Optional[PythonInterpreter]) -> Pip
    """Returns a lazily instantiated global Pip object that is safe for un-coordinated use."""
    pip = _PIP.get(interpreter)
    if pip is None:
        pip = Pip.create(path=os.path.join(ENV.PEX_ROOT, "pip.pex"), interpreter=interpreter)
        _PIP[interpreter] = pip
    return pip
