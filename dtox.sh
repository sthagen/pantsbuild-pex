#!/usr/bin/env bash

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"

BASE_MODE="${BASE_MODE:-build}"
CACHE_MODE="${CACHE_MODE:-}"
CACHE_TAG="${CACHE_TAG:-latest}"

BASE_INPUT=(
  "${ROOT}/docker/base/Dockerfile"
  "${ROOT}/docker/base/install_pythons.sh"
)
base_hash=$(cat "${BASE_INPUT[@]}" | git hash-object -t blob --stdin)

function base_image_id() {
  docker image ls -q "ghcr.io/pantsbuild/pex/base:${base_hash}"
}

if [[ "${BASE_MODE}" == "build" && -z "$(base_image_id)" ]]; then
  docker build \
    --tag ghcr.io/pantsbuild/pex/base:latest \
    --tag "ghcr.io/pantsbuild/pex/base:${base_hash}" \
    "${ROOT}/docker/base"
elif [[ "${BASE_MODE}" == "pull" ]]; then
  docker pull "ghcr.io/pantsbuild/pex/base:${base_hash}"
fi

USER_INPUT=(
  "${BASE_INPUT[@]}"
  "${ROOT}/docker/user/Dockerfile"
  "${ROOT}/docker/user/create_docker_image_user.sh"
)
user_hash=$(cat "${USER_INPUT[@]}" | git hash-object -t blob --stdin)

function user_image_id() {
  docker image ls -q "pantsbuild/pex/user:${user_hash}"
}

if [[ -z "$(user_image_id)" ]]; then
  docker build \
    --build-arg BASE_IMAGE_TAG="${base_hash}" \
    --build-arg USER="$(id -un)" \
    --build-arg UID="$(id -u)" \
    --build-arg GROUP="$(id -gn)" \
    --build-arg GID="$(id -g)" \
    --tag pantsbuild/pex/user:latest \
    --tag "pantsbuild/pex/user:${user_hash}" \
    "${ROOT}/docker/user"
fi

if [[ "${CACHE_MODE}" == "pull" ]]; then
  # N.B.: This is a fairly particular dance / trick that serves to populate a local named volume
  # with the contents of a data-only image. In particular, starting with an empty named volume is
  # required to get the subsequent no-op `docker run --volume pex-caches:...` to populate that
  # volume. This population only happens under that condition.
  docker volume rm --force pex-caches
  docker volume create pex-caches
  docker run \
    --rm \
    --volume pex-caches:/development/pex_dev \
    "ghcr.io/pantsbuild/pex/cache:${CACHE_TAG}" || true
  docker run \
    --rm \
    --volume pex-caches:/development/pex_dev \
    --entrypoint bash \
    --user root \
    "pantsbuild/pex/user:${user_hash}" \
    -c "chown -R $(id -u):$(id -g) /development/pex_dev"
fi

DOCKER_ARGS=()
if [[ "${1:-}" == "inspect" ]]; then
  shift
  DOCKER_ARGS+=(
    --entrypoint bash
  )
fi
if [[ -t 1 ]]; then
  DOCKER_ARGS+=(
    --interactive
    --tty
  )
fi

if [[ -n "${SSH_AUTH_SOCK:-}" ]]; then
  # Some integration tests need an SSH agent. Propagate it when available.
  DOCKER_ARGS+=(
    --volume "${SSH_AUTH_SOCK}:${SSH_AUTH_SOCK}"
    --env SSH_AUTH_SOCK="${SSH_AUTH_SOCK}"
  )
fi

# This ensures the current user owns the host .tox/ dir before launching the container, which
# otherwise sets the ownership as root for undetermined reasons
mkdir -p "${ROOT}/.tox"

CONTAINER_HOME="/home/$(id -un)"
exec docker run \
  --rm \
  --volume pex-tmp:/tmp \
  --volume "${HOME}/.netrc:${CONTAINER_HOME}/.netrc" \
  --volume "${HOME}/.ssh:${CONTAINER_HOME}/.ssh" \
  --volume "pex-root:${CONTAINER_HOME}/.pex" \
  --volume pex-caches:/development/pex_dev \
  --volume "${ROOT}:/development/pex" \
  --volume pex-tox:/development/pex/.tox \
  "${DOCKER_ARGS[@]}" \
  "pantsbuild/pex/user:${user_hash}" \
  "$@"

