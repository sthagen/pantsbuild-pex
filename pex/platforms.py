# Copyright 2017 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import

import json
import os
import re
from textwrap import dedent

from pex import compatibility
from pex.common import atomic_directory, safe_open, safe_rmtree
from pex.pep_425 import CompatibilityTags
from pex.third_party.packaging import tags
from pex.tracer import TRACER
from pex.typing import TYPE_CHECKING, cast
from pex.variables import ENV

if TYPE_CHECKING:
    from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

    import attr  # vendor:skip
else:
    from pex.third_party import attr


def _normalize_platform(platform):
    # type: (str) -> str
    return platform.replace("-", "_").replace(".", "_")


@attr.s(frozen=True)
class Platform(object):
    """Represents a target platform and it's extended interpreter compatibility tags (e.g.
    implementation, version and ABI)."""

    class InvalidPlatformError(Exception):
        """Indicates an invalid platform string."""

    SEP = "-"

    @classmethod
    def create(cls, platform):
        # type: (Union[str, Platform]) -> Platform
        if isinstance(platform, Platform):
            return platform

        platform = platform.lower()
        try:
            platform, impl, version, abi = platform.rsplit(cls.SEP, 3)
            return cls(platform, impl, version, abi)
        except ValueError:
            raise cls.InvalidPlatformError(
                dedent(
                    """\
                    Not a valid platform specifier: {}
                    
                    Platform strings must be in one of two forms:
                    1. Canonical: <platform>-<python impl abbr>-<python version>-<abi>
                    2. Abbreviated: <platform>-<python impl abbr>-<python version>-<abbr abi>
                    
                    Given a canonical platform string for CPython 3.7.5 running on 64 bit linux of:
                      linux-x86_64-cp-37-cp37m
                    
                    Where the fields above are:
                    + <platform>: linux-x86_64 
                    + <python impl abbr>: cp
                    + <python version>: 37
                    + <abi>: cp37m
                    
                    The abbreviated platform string is:
                      linux-x86_64-cp-37-m
                    
                    Some other canonical platform string examples:
                    + OSX CPython: macosx-10.13-x86_64-cp-36-cp36m
                    + Linux PyPy: linux-x86_64-pp-273-pypy_73.
                    
                    These fields stem from wheel name conventions as outlined in
                    https://www.python.org/dev/peps/pep-0427#file-name-convention and influenced by
                    https://www.python.org/dev/peps/pep-0425.
                    """.format(
                        platform
                    )
                )
            )

    @classmethod
    def from_tag(cls, tag):
        # type: (tags.Tag) -> Platform
        """Creates a platform corresponding to wheel compatibility tags.

        See: https://www.python.org/dev/peps/pep-0425/#details
        """
        impl, version = tag.interpreter[:2], tag.interpreter[2:]
        return cls(platform=tag.platform, impl=impl, version=version, abi=tag.abi)

    platform = attr.ib(converter=_normalize_platform)  # type: str
    impl = attr.ib()  # type: str
    version = attr.ib()  # type: str
    abi = attr.ib()  # type: str

    @platform.validator
    @impl.validator
    @version.validator
    @abi.validator
    def _non_blank(self, attribute, value):
        if not value:
            raise self.InvalidPlatformError(
                "Platform specifiers cannot have blank fields. Given {field}={value!r}".format(
                    field=attribute.name, value=value
                )
            )

    def __attrs_post_init__(self):
        # type: () -> None
        if self.impl == "cp" and not self.abi.startswith(self.interpreter):
            # N.B. This permits CPython users to pass in simpler extended platform
            # strings like `linux-x86_64-cp-27-mu` vs e.g. `linux-x86_64-cp-27-cp27mu`.
            object.__setattr__(self, "abi", self.interpreter + self.abi)

    @property
    def interpreter(self):
        # type: () -> str
        return self.impl + self.version

    @property
    def tag(self):
        # type: () -> tags.Tag
        return tags.Tag(interpreter=self.interpreter, abi=self.abi, platform=self.platform)

    def _calculate_tags(
        self,
        manylinux=None,  # type: Optional[str]
    ):
        # type: (...) -> Iterator[tags.Tag]
        from pex.jobs import SpawnedJob
        from pex.pip import get_pip

        def parse_tags(output):
            # type: (bytes) -> Iterator[tags.Tag]
            count = None  # type: Optional[int]
            try:
                for line in output.decode("utf-8").splitlines():
                    if count is None:
                        match = re.match(r"^Compatible tags: (?P<count>\d+)\s+", line)
                        if match:
                            count = int(match.group("count"))
                        continue
                    count -= 1
                    if count < 0:
                        raise AssertionError("Expected {} tags but got more.".format(count))
                    for tag in tags.parse_tag(line.strip()):
                        yield tag
            finally:
                if count != 0:
                    raise AssertionError("Finished with count {}.".format(count))

        job = SpawnedJob.stdout(
            job=get_pip().spawn_debug(
                platform=self.platform,
                impl=self.impl,
                version=self.version,
                abi=self.abi,
                manylinux=manylinux,
            ),
            result_func=parse_tags,
        )
        return job.await_result()

    PLAT_INFO_FILE = "PLAT-INFO"

    _SUPPORTED_TAGS_BY_PLATFORM = (
        {}
    )  # type: Dict[Tuple[Platform, Optional[str]], CompatibilityTags]

    def supported_tags(self, manylinux=None):
        # type: (Optional[str]) -> CompatibilityTags

        # We use a 2 level cache, probing memory first and then a json file on disk in order to
        # avoid calculating tags when possible since it's an O(500ms) operation that involves
        # spawning Pip.

        # Read level 1.
        memory_cache_key = (self, manylinux)
        supported_tags = self._SUPPORTED_TAGS_BY_PLATFORM.get(memory_cache_key)
        if supported_tags is not None:
            return supported_tags

        # Read level 2.
        components = list(attr.astuple(self))
        if manylinux:
            components.append(manylinux)
        disk_cache_key = os.path.join(ENV.PEX_ROOT, "platforms", self.SEP.join(components))
        with atomic_directory(target_dir=disk_cache_key, exclusive=False) as cache_dir:
            if not cache_dir.is_finalized():
                # Missed both caches - spawn calculation.
                plat_info = attr.asdict(self)
                plat_info.update(
                    supported_tags=[
                        (tag.interpreter, tag.abi, tag.platform)
                        for tag in self._calculate_tags(manylinux=manylinux)
                    ],
                )
                # Write level 2.
                with safe_open(os.path.join(cache_dir.work_dir, self.PLAT_INFO_FILE), "w") as fp:
                    json.dump(plat_info, fp)

        with open(os.path.join(disk_cache_key, self.PLAT_INFO_FILE)) as fp:
            try:
                data = json.load(fp)
            except ValueError as e:
                TRACER.log(
                    "Regenerating the platform info file at {} since it did not contain parsable "
                    "JSON data: {}".format(fp.name, e)
                )
                safe_rmtree(disk_cache_key)
                return self.supported_tags(manylinux=manylinux)

        if not isinstance(data, dict):
            TRACER.log(
                "Regenerating the platform info file at {} since it did not contain a "
                "configuration object. Found: {!r}".format(fp.name, data)
            )
            safe_rmtree(disk_cache_key)
            return self.supported_tags(manylinux=manylinux)

        sup_tags = data.get("supported_tags")
        if not isinstance(sup_tags, list):
            TRACER.log(
                "Regenerating the platform info file at {} since it was missing a valid "
                "`supported_tags` list. Found: {!r}".format(fp.name, sup_tags)
            )
            safe_rmtree(disk_cache_key)
            return self.supported_tags(manylinux=manylinux)

        count = len(sup_tags)

        def parse_tag(
            index,  # type: int
            tag,  # type: List[Any]
        ):
            # type: (...) -> tags.Tag
            if len(tag) != 3 or not all(
                isinstance(component, compatibility.string) for component in tag
            ):
                raise ValueError(
                    "Serialized platform tags should be lists of three strings. Tag {index} of "
                    "{count} was: {tag!r}.".format(index=index, count=count, tag=tag)
                )
            interpreter, abi, platform = tag
            return tags.Tag(interpreter=interpreter, abi=abi, platform=platform)

        try:
            supported_tags = CompatibilityTags(
                tags=[parse_tag(index, tag) for index, tag in enumerate(sup_tags)]
            )
            # Write level 1.
            self._SUPPORTED_TAGS_BY_PLATFORM[memory_cache_key] = supported_tags
            return supported_tags
        except ValueError as e:
            TRACER.log(
                "Regenerating the platform info file at {} since it did not contain parsable "
                "tag data: {}".format(fp.name, e)
            )
            safe_rmtree(disk_cache_key)
            return self.supported_tags(manylinux=manylinux)

    def __str__(self):
        # type: () -> str
        return cast(str, self.SEP.join(attr.astuple(self)))
