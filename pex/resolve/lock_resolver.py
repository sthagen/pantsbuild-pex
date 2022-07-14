# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import

import functools
import os.path
import shutil
from collections import OrderedDict
from multiprocessing.pool import ThreadPool

from pex import resolver
from pex.auth import PasswordDatabase, PasswordEntry
from pex.common import FileLockStyle, pluralize
from pex.compatibility import cpu_count
from pex.network_configuration import NetworkConfiguration
from pex.orderedset import OrderedSet
from pex.pep_503 import ProjectName
from pex.pip.local_project import digest_local_project
from pex.pip.tool import PackageIndexConfiguration, get_pip
from pex.pip.vcs import digest_vcs_archive
from pex.resolve.downloads import ArtifactDownloader
from pex.resolve.locked_resolve import (
    DownloadableArtifact,
    FileArtifact,
    LocalProjectArtifact,
    VCSArtifact,
)
from pex.resolve.lockfile.download_manager import DownloadedArtifact, DownloadManager
from pex.resolve.lockfile.model import Lockfile
from pex.resolve.lockfile.subset import subset
from pex.resolve.requirement_configuration import RequirementConfiguration
from pex.resolve.resolver_configuration import ResolverVersion
from pex.resolve.resolvers import Installed, Resolver
from pex.resolver import BuildAndInstallRequest, BuildRequest, InstallRequest
from pex.result import Error, catch, try_
from pex.targets import Targets
from pex.tracer import TRACER
from pex.typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Dict, Iterable, Optional, Sequence, Union

    from pex.hashing import HintedDigest


class FileArtifactDownloadManager(DownloadManager[FileArtifact]):
    def __init__(
        self,
        file_lock_style,  # type: FileLockStyle.Value
        downloader,  # type: ArtifactDownloader
        pex_root=None,  # type: Optional[str]
    ):
        super(FileArtifactDownloadManager, self).__init__(
            pex_root=pex_root, file_lock_style=file_lock_style
        )
        self._downloader = downloader

    def save(
        self,
        artifact,  # type: FileArtifact
        project_name,  # type: ProjectName
        dest_dir,  # type: str
        digest,  # type: HintedDigest
    ):
        # type: (...) -> Union[str, Error]
        return self._downloader.download(artifact=artifact, dest_dir=dest_dir, digest=digest)


class VCSArtifactDownloadManager(DownloadManager[VCSArtifact]):
    def __init__(
        self,
        file_lock_style,  # type: FileLockStyle.Value
        indexes=None,  # type: Optional[Sequence[str]]
        find_links=None,  # type: Optional[Sequence[str]]
        resolver_version=None,  # type: Optional[ResolverVersion.Value]
        network_configuration=None,  # type: Optional[NetworkConfiguration]
        password_entries=(),  # type: Iterable[PasswordEntry]
        cache=None,  # type: Optional[str]
        use_pep517=None,  # type: Optional[bool]
        build_isolation=True,  # type: bool
        pex_root=None,  # type: Optional[str]
    ):
        super(VCSArtifactDownloadManager, self).__init__(
            pex_root=pex_root, file_lock_style=file_lock_style
        )
        self._indexes = indexes
        self._find_links = find_links
        self._resolver_version = resolver_version
        self._network_configuration = network_configuration
        self._password_entries = password_entries
        self._cache = cache
        self._use_pep517 = use_pep517
        self._build_isolation = build_isolation

    def save(
        self,
        artifact,  # type: VCSArtifact
        project_name,  # type: ProjectName
        dest_dir,  # type: str
        digest,  # type: HintedDigest
    ):
        # type: (...) -> Union[str, Error]

        requirement = artifact.as_unparsed_requirement(project_name)
        downloaded_vcs = resolver.download(
            requirements=[requirement],
            transitive=False,
            indexes=self._indexes,
            find_links=self._find_links,
            resolver_version=self._resolver_version,
            network_configuration=self._network_configuration,
            password_entries=self._password_entries,
            use_wheel=False,
            prefer_older_binary=False,
            use_pep517=self._use_pep517,
            build_isolation=self._build_isolation,
            max_parallel_jobs=1,
        )
        if len(downloaded_vcs.local_distributions) != 1:
            return Error(
                "Expected 1 artifact for an intransitive download of {requirement}, found "
                "{count}:\n"
                "{downloads}".format(
                    requirement=requirement,
                    count=len(downloaded_vcs.local_distributions),
                    downloads="\n".join(
                        "{index}. {download}".format(index=index, download=download.path)
                        for index, download in enumerate(
                            downloaded_vcs.local_distributions, start=1
                        )
                    ),
                )
            )

        local_distribution = downloaded_vcs.local_distributions[0]
        filename = os.path.basename(local_distribution.path)
        digest_vcs_archive(
            archive_path=local_distribution.path,
            vcs=artifact.vcs,
            digest=digest,
        )
        shutil.move(local_distribution.path, os.path.join(dest_dir, filename))
        return filename


class LocalProjectDownloadManager(DownloadManager[LocalProjectArtifact]):
    def __init__(
        self,
        file_lock_style,  # type: FileLockStyle.Value
        build_requires_resolver,  # type: Resolver
        pex_root=None,  # type: Optional[str]
    ):
        super(LocalProjectDownloadManager, self).__init__(
            pex_root=pex_root, file_lock_style=file_lock_style
        )
        self._resolver = build_requires_resolver

    def save(
        self,
        artifact,  # type: LocalProjectArtifact
        project_name,  # type: ProjectName
        dest_dir,  # type: str
        digest,  # type: HintedDigest
    ):
        # type: (...) -> Union[str, Error]
        source_dir_or_error = digest_local_project(
            directory=artifact.directory,
            digest=digest,
            resolver=self._resolver,
            dest_dir=dest_dir,
        )
        if isinstance(source_dir_or_error, Error):
            return source_dir_or_error
        return os.path.basename(source_dir_or_error)


def download_artifact(
    downloadable_artifact,  # type: DownloadableArtifact
    file_download_manager,  # type: FileArtifactDownloadManager
    vcs_download_manager,  # type: VCSArtifactDownloadManager
    local_project_download_manager,  # type: LocalProjectDownloadManager
):
    # type: (...) -> Union[DownloadedArtifact, Error]

    if isinstance(downloadable_artifact.artifact, VCSArtifact):
        return catch(
            vcs_download_manager.store,
            downloadable_artifact.artifact,
            downloadable_artifact.pin.project_name,
        )

    if isinstance(downloadable_artifact.artifact, FileArtifact):
        return catch(
            file_download_manager.store,
            downloadable_artifact.artifact,
            downloadable_artifact.pin.project_name,
        )

    return catch(
        local_project_download_manager.store,
        downloadable_artifact.artifact,
        downloadable_artifact.pin.project_name,
    )


# Derived from notes in the bandersnatch PyPI mirroring tool:
# https://github.com/pypa/bandersnatch/blob/1485712d6aa77fba54bbf5a2df0d7314124ad097/src/bandersnatch/default.conf#L30-L35
MAX_PARALLEL_DOWNLOADS = 10


def resolve_from_lock(
    targets,  # type: Targets
    lock,  # type: Lockfile
    resolver,  # type: Resolver
    requirements=None,  # type: Optional[Iterable[str]]
    requirement_files=None,  # type: Optional[Iterable[str]]
    constraint_files=None,  # type: Optional[Iterable[str]]
    indexes=None,  # type: Optional[Sequence[str]]
    find_links=None,  # type: Optional[Sequence[str]]
    resolver_version=None,  # type: Optional[ResolverVersion.Value]
    network_configuration=None,  # type: Optional[NetworkConfiguration]
    password_entries=(),  # type: Iterable[PasswordEntry]
    build=True,  # type: bool
    use_wheel=True,  # type: bool
    prefer_older_binary=False,  # type: bool
    use_pep517=None,  # type: Optional[bool]
    build_isolation=True,  # type: bool
    compile=False,  # type: bool
    transitive=True,  # type: bool
    verify_wheels=True,  # type: bool
    max_parallel_jobs=None,  # type: Optional[int]
):
    # type: (...) -> Union[Installed, Error]

    subset_result = try_(
        subset(
            targets=targets,
            lock=lock,
            requirement_configuration=RequirementConfiguration(
                requirements=requirements,
                requirement_files=requirement_files,
                constraint_files=constraint_files,
            ),
            network_configuration=network_configuration,
            build=build,
            use_wheel=use_wheel,
            prefer_older_binary=prefer_older_binary,
            transitive=transitive,
        )
    )
    downloadable_artifacts = OrderedSet(
        downloadable_artifact
        for resolved_subset in subset_result.subsets
        for downloadable_artifact in resolved_subset.resolved.downloadable_artifacts
    )

    # Since the download managers are stored to via a thread pool, we need to use BSD style locks.
    # These locks are not as portable as POSIX style locks but work with threading unlike POSIX
    # locks which are subject to threading-unaware deadlock detection per the standard. Linux, in
    # fact, implements deadlock detection for POSIX locks; so we can run afoul of false EDEADLCK
    # errors under the right interleaving of processes and threads and download artifact targets.
    file_lock_style = FileLockStyle.BSD

    # We eagerly initialize a Pip tool for reasons alluded to above, creation of the Pip tool venv
    # is not thread-safe and this can lead to errors.
    pip = get_pip()

    file_download_manager = FileArtifactDownloadManager(
        file_lock_style=file_lock_style,
        downloader=ArtifactDownloader(
            resolver=resolver,
            pip=pip,
            package_index_configuration=PackageIndexConfiguration.create(
                resolver_version=resolver_version,
                indexes=indexes,
                find_links=find_links,
                network_configuration=network_configuration,
                password_entries=PasswordDatabase.from_netrc().append(password_entries).entries,
            ),
        ),
    )

    vcs_download_manager = VCSArtifactDownloadManager(
        file_lock_style=file_lock_style,
        indexes=indexes,
        find_links=find_links,
        resolver_version=resolver_version,
        network_configuration=network_configuration,
        password_entries=password_entries,
        use_pep517=use_pep517,
        build_isolation=build_isolation,
    )

    local_project_download_manager = LocalProjectDownloadManager(
        file_lock_style=file_lock_style,
        build_requires_resolver=resolver,
    )

    max_threads = min(
        len(downloadable_artifacts) or 1,
        min(MAX_PARALLEL_DOWNLOADS, 4 * (max_parallel_jobs or cpu_count() or 1)),
    )
    with TRACER.timed(
        "Downloading {url_count} distributions to satisfy {requirement_count} requirements".format(
            url_count=len(downloadable_artifacts),
            requirement_count=len(subset_result.requirements),
        )
    ):
        pool = ThreadPool(processes=max_threads)
        try:
            download_results = tuple(
                zip(
                    downloadable_artifacts,
                    pool.map(
                        functools.partial(
                            download_artifact,
                            file_download_manager=file_download_manager,
                            vcs_download_manager=vcs_download_manager,
                            local_project_download_manager=local_project_download_manager,
                        ),
                        downloadable_artifacts,
                    ),
                )
            )
        finally:
            pool.close()
            pool.join()

    with TRACER.timed("Categorizing {} downloaded artifacts".format(len(download_results))):
        downloaded_artifacts = {}  # type: Dict[DownloadableArtifact, DownloadedArtifact]
        download_errors = OrderedDict()  # type: OrderedDict[DownloadableArtifact, Error]
        for downloadable_artifact, download_result in download_results:
            if isinstance(download_result, DownloadedArtifact):
                downloaded_artifacts[downloadable_artifact] = download_result
            else:
                download_errors[downloadable_artifact] = download_result

        if download_errors:
            error_count = len(download_errors)
            return Error(
                "There {were} {count} {errors} downloading required artifacts:\n"
                "{error_details}".format(
                    were="was" if error_count == 1 else "were",
                    count=error_count,
                    errors=pluralize(download_errors, "error"),
                    error_details="\n".join(
                        "{index}. {pin} from {url}\n    {error}".format(
                            index=index,
                            pin=downloadable_artifact.pin,
                            url=downloadable_artifact.artifact.url,
                            error=error,
                        )
                        for index, (downloadable_artifact, error) in enumerate(
                            download_errors.items(), start=1
                        )
                    ),
                )
            )

        build_requests = []
        install_requests = []
        for resolved_subset in subset_result.subsets:
            for downloadable_artifact in resolved_subset.resolved.downloadable_artifacts:
                downloaded_artifact = downloaded_artifacts[downloadable_artifact]
                if downloaded_artifact.path.endswith(".whl"):
                    install_requests.append(
                        InstallRequest(
                            target=resolved_subset.target,
                            wheel_path=downloaded_artifact.path,
                            fingerprint=downloaded_artifact.fingerprint,
                        )
                    )
                else:
                    build_requests.append(
                        BuildRequest(
                            target=resolved_subset.target,
                            source_path=downloaded_artifact.path,
                            fingerprint=downloaded_artifact.fingerprint,
                        )
                    )

    with TRACER.timed(
        "Building {} artifacts and installing {}".format(
            len(build_requests), len(build_requests) + len(install_requests)
        )
    ):
        build_and_install_request = BuildAndInstallRequest(
            build_requests=build_requests,
            install_requests=install_requests,
            direct_requirements=subset_result.requirements,
            package_index_configuration=PackageIndexConfiguration.create(
                resolver_version=resolver_version,
                indexes=indexes,
                find_links=find_links,
                network_configuration=network_configuration,
                password_entries=PasswordDatabase.from_netrc().append(password_entries).entries,
            ),
            compile=compile,
            prefer_older_binary=prefer_older_binary,
            use_pep517=use_pep517,
            build_isolation=build_isolation,
            verify_wheels=verify_wheels,
        )
        installed_distributions = build_and_install_request.install_distributions(
            # This otherwise checks that resolved distributions all meet internal requirement
            # constraints (This allows pip-legacy-resolver resolves with invalid solutions to be
            # failed post-facto by Pex at PEX build time). We've already done this via
            # `LockedResolve.resolve` above and need not waste time (~O(100ms)) doing this again.
            ignore_errors=True,
            max_parallel_jobs=max_parallel_jobs,
        )
    return Installed(installed_distributions=tuple(installed_distributions))
