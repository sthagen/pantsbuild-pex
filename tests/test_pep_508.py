# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import pytest

from pex.pep_508 import MarkerEnvironment
from pex.platforms import Platform
from pex.third_party.packaging import markers
from pex.typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from typing import Dict


def test_platform_marker_environment():
    # type: () -> None
    platform = Platform.create("linux-x86_64-cp-37-cp37m")
    marker_environment = MarkerEnvironment.from_platform(platform)
    env_defaulted = marker_environment.as_dict(default_unknown=True)
    env_sparse = marker_environment.as_dict(default_unknown=False)

    assert set(env_sparse.items()).issubset(set(env_defaulted.items()))

    def evaluate_marker(
        expression,  # type: str
        environment,  # type: Dict[str, str]
    ):
        # type: (...) -> bool
        markers.default_environment = environment.copy
        return cast(bool, markers.Marker(expression).evaluate())

    def assert_known_marker(expression):
        # type: (str) -> None
        assert evaluate_marker(expression, env_defaulted)
        assert evaluate_marker(expression, env_sparse)

    assert_known_marker("python_version == '3.7'")
    assert_known_marker("implementation_name == 'cpython'")
    assert_known_marker("platform_system == 'Linux'")
    assert_known_marker("platform_machine == 'x86_64'")

    def assert_unknown_marker(expression):
        # type: (str) -> None
        assert not evaluate_marker(expression, env_defaulted)
        with pytest.raises(markers.UndefinedEnvironmentName):
            evaluate_marker(expression, env_sparse)

    assert_unknown_marker("python_full_version == '3.7.10'")
    assert_unknown_marker("platform_release == '5.12.12-arch1-1'")
    assert_unknown_marker("platform_version == '#1 SMP PREEMPT Fri, 18 Jun 2021 21:59:22 +0000'")


def test_platform_marker_environment_issue_1488():
    # type: () -> None

    def assert_platform_machine(
        expected,  # type: str
        platform,  # type: str
    ):
        marker_environment = MarkerEnvironment.from_platform(Platform.create(platform))
        assert expected == marker_environment.platform_machine

    assert_platform_machine("x86_64", "linux-x86_64-cp-37-cp37m")
    assert_platform_machine("x86_64", "manylinux1-x86_64-cp-37-cp37m")
    assert_platform_machine("x86_64", "manylinux2010-x86_64-cp-37-cp37m")
    assert_platform_machine("x86_64", "manylinux2014-x86_64-cp-37-cp37m")
    assert_platform_machine("x86_64", "manylinux_2_5-x86_64-cp-37-cp37m")
    assert_platform_machine("aarch64", "manylinux_2_77-aarch64-cp-37-cp37m")

    assert_platform_machine("x86_64", "macosx-10.15-x86_64-cp-38-m")
    assert_platform_machine("arm64", "macosx-11.0-arm64-cp-39-cp39")
