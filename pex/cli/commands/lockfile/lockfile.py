# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, print_function

import os

from pex.compatibility import urlparse
from pex.distribution_target import DistributionTarget
from pex.resolve.locked_resolve import LockedResolve, LockStyle
from pex.resolve.resolver_configuration import ResolverVersion
from pex.sorted_tuple import SortedTuple
from pex.third_party.packaging import tags
from pex.third_party.pkg_resources import Requirement, RequirementParseError
from pex.tracer import TRACER
from pex.typing import TYPE_CHECKING

if TYPE_CHECKING:
    import attr  # vendor:skip
    from typing import (
        Iterable,
        Iterator,
        List,
        Mapping,
        Optional,
        Tuple,
    )
else:
    from pex.third_party import attr


@attr.s(frozen=True)
class _RankedLock(object):
    @classmethod
    def rank(
        cls,
        locked_resolve,  # type: LockedResolve
        supported_tags,  # type: Mapping[tags.Tag, int]
    ):
        # type: (...) -> Optional[_RankedLock]
        """Rank the given resolve for the supported tags of a distribution target.

        Pex allows choosing an array of distribution targets as part of building multiplatform PEX
        files. Whether via interpreter constraint ranges, multiple `--python` or `--platform`
        specifications or some combination of these, parallel resolves will be executed for each
        distinct distribution target selected. When generating a lock, Pex similarly will create a
        locked resolve per selected distribution target in parallel. Later, at lock consumption
        time, there will again be one or more distribution targets that may need to resolve from the
        lock. For each of these distribution targets, either one or more of the generated locks will
        be applicable or none will. If the distribution target matches one of those used to generate
        the lock file, the corresponding locked resolve will clearly work. The distribution target
        need not match though for the locked resolve to be usable. All that's needed is for at least
        one artifact for each locked requirement in the resolve to be usable by the distribution
        target. The classic example is a locked resolve that is populated with only universal
        wheels. Even if such a locked resolve was generated by a PyPy 2 interpreter, it should be
        usable by a CPython 3.10 interpreter or any other Python 2 or Python 3 interpreter.

        To help select which locked resolve to use, ranking gives a score to each locked resolve
        that is the average of the score of each locked requirement in the resolve. Each locked
        requirement is, in turn, scored by its best matching artifact score. Artifacts are scored as
        follows:

        + If the artifact is a wheel, score it based on its best matching tag.
        + If the artifact is an sdist, score it as usable, but a worse match than any wheel.
        + Otherwise treat the artifact as unusable.

        If a locked requirement has no matching artifact, the scoring is aborted since the locked
        resolve has an unsatisfied requirement and `None` is returned.

        :param locked_resolve: The resolve to rank.
        :param supported_tags: The supported tags of the distribution target looking to pick a
                               resolve to use.
        :return: A ranked lock if the resolve is applicable to the distribution target else `None`.
        """
        resolve_rank = None  # type: Optional[int]
        for req in locked_resolve.locked_requirements:
            requirement_rank = None  # type: Optional[int]
            for artifact in req.iter_artifacts():
                url_info = urlparse.urlparse(artifact.url)
                artifact_file = os.path.basename(url_info.path)
                if artifact_file.endswith(
                    (".sdist", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".zip")
                ):
                    # N.B.: This is greater (worse) than any wheel rank can be by 1, ensuring sdists
                    # are picked last amongst a set of artifacts. We do this, since a wheel is known
                    # to work with a target by the platform tags on the tin, whereas an sdist may
                    # not successfully build for a given target at all. This is an affordance for
                    # LockStyle.SOURCES and LockStyle.CROSS_PLATFORM lock styles.
                    sdist_rank = len(supported_tags)
                    requirement_rank = (
                        sdist_rank
                        if requirement_rank is None
                        else min(sdist_rank, requirement_rank)
                    )
                elif artifact_file.endswith(".whl"):
                    artifact_stem, _ = os.path.splitext(artifact_file)
                    for tag in tags.parse_tag(artifact_stem.split("-", 2)[-1]):
                        wheel_rank = supported_tags.get(tag)
                        if wheel_rank is not None:
                            requirement_rank = (
                                wheel_rank
                                if requirement_rank is None
                                else min(wheel_rank, requirement_rank)
                            )

            if requirement_rank is None:
                return None

            resolve_rank = (
                requirement_rank if resolve_rank is None else resolve_rank + requirement_rank
            )

        if resolve_rank is None:
            return None

        average_requirement_rank = float(resolve_rank) / len(locked_resolve.locked_requirements)
        return cls(average_requirement_rank=average_requirement_rank, locked_resolve=locked_resolve)

    average_requirement_rank = attr.ib()  # type: float
    locked_resolve = attr.ib()  # type: LockedResolve


@attr.s(frozen=True)
class Lockfile(object):
    @classmethod
    def create(
        cls,
        pex_version,  # type: str
        style,  # type: LockStyle.Value
        resolver_version,  # type: ResolverVersion.Value
        requirements,  # type: Iterable[Requirement]
        constraints,  # type: Iterable[Requirement]
        allow_prereleases,  # type: bool
        allow_wheels,  # type: bool
        allow_builds,  # type: bool
        prefer_older_binary,  # type: bool
        use_pep517,  # type: Optional[bool]
        build_isolation,  # type: bool
        transitive,  # type: bool
        locked_resolves,  # type: Iterable[LockedResolve]
        source=None,  # type: Optional[str]
    ):
        # type: (...) -> Lockfile
        return cls(
            pex_version=pex_version,
            style=style,
            resolver_version=resolver_version,
            requirements=SortedTuple(requirements, key=str),
            constraints=SortedTuple(constraints, key=str),
            allow_prereleases=allow_prereleases,
            allow_wheels=allow_wheels,
            allow_builds=allow_builds,
            prefer_older_binary=prefer_older_binary,
            use_pep517=use_pep517,
            build_isolation=build_isolation,
            transitive=transitive,
            locked_resolves=SortedTuple(locked_resolves),
            source=source,
        )

    pex_version = attr.ib()  # type: str
    style = attr.ib()  # type: LockStyle.Value
    resolver_version = attr.ib()  # type: ResolverVersion.Value
    requirements = attr.ib()  # type: SortedTuple[Requirement]
    constraints = attr.ib()  # type: SortedTuple[Requirement]
    allow_prereleases = attr.ib()  # type: bool
    allow_wheels = attr.ib()  # type: bool
    allow_builds = attr.ib()  # type: bool
    prefer_older_binary = attr.ib()  # type: bool
    use_pep517 = attr.ib()  # type: Optional[bool]
    build_isolation = attr.ib()  # type: bool
    transitive = attr.ib()  # type: bool
    locked_resolves = attr.ib()  # type: SortedTuple[LockedResolve]
    source = attr.ib(default=None, eq=False)  # type: Optional[str]

    def select(self, targets):
        # type: (Iterable[DistributionTarget]) -> Iterator[Tuple[DistributionTarget, LockedResolve]]
        """Finds the most appropriate lock, if any, for each of the given targets.

        :param targets: The targets to select locked resolves for.
        :return: The selected locks.
        """
        for target in targets:
            lock = self._select(target)
            if lock:
                yield target, lock

    def _select(self, target):
        # type: (DistributionTarget) -> Optional[LockedResolve]
        ranked_locks = []  # type: List[_RankedLock]

        supported_tags = {tag: index for index, tag in enumerate(target.get_supported_tags())}
        for locked_resolve in self.locked_resolves:
            ranked_lock = _RankedLock.rank(locked_resolve, supported_tags)
            if ranked_lock is not None:
                ranked_locks.append(ranked_lock)

        if not ranked_locks:
            return None

        ranked_lock = sorted(ranked_locks)[0]
        count = len(supported_tags)
        TRACER.log(
            "Selected lock generated by {platform} with an average requirement rank of "
            "{average_requirement_rank:.2f} (out of {count}, so ~{percent:.1%} platform specific) "
            "from locks generated by {platforms}".format(
                platform=ranked_lock.locked_resolve.platform_tag,
                average_requirement_rank=ranked_lock.average_requirement_rank,
                count=count,
                percent=(count - ranked_lock.average_requirement_rank) / count,
                platforms=", ".join(
                    sorted(str(lock.platform_tag) for lock in self.locked_resolves)
                ),
            )
        )
        return ranked_lock.locked_resolve
