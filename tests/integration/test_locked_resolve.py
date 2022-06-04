# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import hashlib
import os

import pytest

from pex import dist_metadata, resolver, targets
from pex.fetcher import URLFetcher
from pex.pip.tool import PackageIndexConfiguration
from pex.resolve.configured_resolver import ConfiguredResolver
from pex.resolve.locked_resolve import LockConfiguration, LockedResolve, LockStyle
from pex.resolve.lockfile.operations import LockObserver
from pex.resolve.resolved_requirement import Pin
from pex.resolve.resolver_configuration import PipConfiguration
from pex.resolve.testing import normalize_locked_resolve
from pex.resolver import Downloaded, LocalDistribution, WheelBuilder
from pex.typing import TYPE_CHECKING
from pex.util import CacheHelper

if TYPE_CHECKING:
    from typing import Any, Dict, Iterable, Tuple


def normalize(
    locked_resolves,  # type: Tuple[LockedResolve, ...]
    skip_additional_artifacts=False,  # type: bool
    skip_urls=False,  # type: bool
    skip_verified=False,  # type: bool
):
    # type: (...) -> Tuple[LockedResolve, ...]
    return tuple(
        normalize_locked_resolve(
            lock,
            skip_additional_artifacts=skip_additional_artifacts,
            skip_urls=skip_urls,
            skip_verified=skip_verified,
        )
        for lock in locked_resolves
    )


def create_lock_observer(lock_configuration):
    # type: (LockConfiguration) -> LockObserver
    pip_configuration = PipConfiguration()
    return LockObserver(
        lock_configuration=lock_configuration,
        resolver=ConfiguredResolver(pip_configuration=pip_configuration),
        wheel_builder=WheelBuilder(
            package_index_configuration=PackageIndexConfiguration.create(
                resolver_version=pip_configuration.resolver_version,
                indexes=pip_configuration.repos_configuration.indexes,
                find_links=pip_configuration.repos_configuration.find_links,
                network_configuration=pip_configuration.network_configuration,
            ),
            prefer_older_binary=pip_configuration.prefer_older_binary,
            use_pep517=pip_configuration.use_pep517,
            build_isolation=pip_configuration.build_isolation,
        ),
        url_fetcher=URLFetcher(network_configuration=pip_configuration.network_configuration),
    )


def create_lock(
    lock_configuration,  # type: LockConfiguration
    **kwargs  # type: Any
):
    # type: (...) -> Tuple[Downloaded, Tuple[LockedResolve, ...]]
    lock_observer = create_lock_observer(lock_configuration)
    downloaded = resolver.download(observer=lock_observer, **kwargs)
    return downloaded, lock_observer.lock(downloaded)


@pytest.mark.parametrize(
    "requirements",
    (
        pytest.param(["ansicolors==1.1.8"], id="pinned-no-transitive-deps"),
        pytest.param(["isort==4.3.21"], id="pinned-transitive-deps"),
        pytest.param(["ansicolors"], id="float-no-transitive-deps"),
        pytest.param(["isort"], id="float-transitive-deps"),
    ),
)
@pytest.mark.parametrize(
    "lock_configuration",
    (
        pytest.param(LockConfiguration(style=LockStyle.STRICT), id="strict"),
        pytest.param(LockConfiguration(style=LockStyle.SOURCES), id="sources"),
    ),
)
def test_lock_single_target(
    tmpdir,  # type: Any
    requirements,  # type: Iterable[str]
    lock_configuration,  # type: LockConfiguration
):
    # type: (...) -> None

    downloaded, locked_resolves = create_lock(lock_configuration, requirements=requirements)
    assert 1 == len(locked_resolves)
    lock = locked_resolves[0]

    assert targets.current().platform.tag == lock.platform_tag

    def pin(local_distribution):
        # type: (LocalDistribution) -> Pin
        project_name_and_version = dist_metadata.project_name_and_version(local_distribution.path)
        assert project_name_and_version is not None
        return Pin.canonicalize(project_name_and_version)

    local_distributions_by_pin = {
        pin(local_dist): local_dist for local_dist in downloaded.local_distributions
    }  # type: Dict[Pin, LocalDistribution]

    assert sorted(local_distributions_by_pin) == sorted(
        locked_req.pin for locked_req in lock.locked_requirements
    ), (
        "Expected the actual set of downloaded distributions to match the set of pinned "
        "requirements in the lock."
    )

    for locked_req in lock.locked_requirements:
        fingerprint = locked_req.artifact.fingerprint
        assert fingerprint.hash == CacheHelper.hash(
            path=local_distributions_by_pin[locked_req.pin].path,
            hasher=lambda: hashlib.new(fingerprint.algorithm),
        ), (
            "Expected the fingerprint of the downloaded distribution to match the fingerprint "
            "recorded in the lock."
        )

    find_links_repo = os.path.join(str(tmpdir), "find-links")
    os.mkdir(find_links_repo)
    for local_dist in downloaded.local_distributions:
        os.symlink(
            local_dist.path, os.path.join(find_links_repo, os.path.basename(local_dist.path))
        )
    _, find_links_locked_resolves = create_lock(
        lock_configuration,
        requirements=requirements,
        indexes=[],
        find_links=[find_links_repo],
    )
    assert normalize(
        locked_resolves, skip_additional_artifacts=True, skip_urls=True, skip_verified=True
    ) == normalize(
        find_links_locked_resolves,
        skip_additional_artifacts=True,
        skip_urls=True,
        skip_verified=True,
    ), (
        "Expected a find-links lock to match an equivalent PyPI lock except for the primary "
        "artifact urls and their verification status and lack of additional artifacts (since these "
        "are never downloaded; but instead, just recorded)."
    )

    lock_file = os.path.join(str(tmpdir), "requirements.txt")
    with open(lock_file, "w") as fp:
        lock.emit_requirements(fp)
    _, export_locked_resolves = create_lock(lock_configuration, requirement_files=[lock_file])
    assert normalize(locked_resolves) == normalize(export_locked_resolves), (
        "Expected the download used to create a lock to be reproduced by a download using the "
        "requirements generated from the lock."
    )
