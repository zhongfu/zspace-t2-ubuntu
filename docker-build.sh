#!/usr/bin/env bash
#
# Run the build in the container described by ./Dockerfile.
#
#   ./docker-build.sh                # the whole build -> build/out/installer.img
#   ./docker-build.sh bash           # a shell in the same environment
#   ./docker-build.sh kernel/build.sh # one step, same environment
#
# The repository is bind-mounted at /work and the container runs with your uid
# and gid, so everything it writes into build/ stays yours.  The image is built
# once and cached; rebuild it with `docker build --no-cache` when the baked apt
# lists go stale (rootfs/t2-distro.py resolves qemu-user-static's version from
# them).
#
# Usage: docker-build.sh [command [args...]]
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
image=${IMAGE:-zspace-t2-build:latest}

docker build -q -t "$image" "$here" >/dev/null

# seccomp=unconfined: rootfs/t2-distro.py's default chroot backend is proot,
# which needs ptrace, and Docker's default seccomp profile denies it.  No
# --privileged, no loop devices, no host root - the build behaves as it does on
# a host.
exec docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    --security-opt seccomp=unconfined \
    -v "$here:/work" \
    -w /work \
    "$image" "${@:-./build-all.sh}"
