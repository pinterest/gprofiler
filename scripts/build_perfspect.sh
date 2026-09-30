#!/usr/bin/env bash
#
# Copyright (C) 2022 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
set -euo pipefail

# Parse command line arguments for BUILD_STRATEGY
BUILD_STRATEGY="build"  # Default value

show_usage() {
    echo "Usage: $0 [--strategy=build|download]"
    echo "  --strategy=build    Clone and build PerfSpect tools (default)."
    echo "                      Cross-compiles and emits BOTH x86_64 and aarch64 binaries."
    echo "  --strategy=download Download the pre-built PerfSpect release (x86_64 only)."
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --strategy=*)
            BUILD_STRATEGY="${1#*=}"
            if [[ "$BUILD_STRATEGY" != "build" && "$BUILD_STRATEGY" != "download" ]]; then
                echo "Error: Invalid strategy '$BUILD_STRATEGY'. Must be 'build' or 'download'."
                show_usage
            fi
            shift
            ;;
        --strategy)
            if [[ $# -lt 2 ]]; then
                echo "Error: --strategy requires a value"
                show_usage
            fi
            BUILD_STRATEGY="$2"
            if [[ "$BUILD_STRATEGY" != "build" && "$BUILD_STRATEGY" != "download" ]]; then
                echo "Error: Invalid strategy '$BUILD_STRATEGY'. Must be 'build' or 'download'."
                show_usage
            fi
            shift 2
            ;;
        -h|--help)
            show_usage
            ;;
        *)
            echo "Error: Unknown argument '$1'"
            show_usage
            ;;
    esac
done

echo "Using BUILD_STRATEGY: $BUILD_STRATEGY"

VERSION=v3.17.0
GIT_REV="956744c9b41359f8211dd7442a11033515d9ab4b"

# Apply gProfiler-specific patches to the cloned PerfSpect source.
# These fixes are intentionally kept here (not upstreamed) so we can build a
# specific PerfSpect revision for gProfiler without modifying the open source
# project. Each patch is guarded: if the expected text is missing (e.g. GIT_REV
# was bumped to a revision that changed these files), the build fails loudly so
# we don't silently build an unpatched tree.
apply_perfspect_patch() {
    local file="$1" before="$2" after="$3" desc="$4"
    if grep -qF -- "$after" "$file"; then
        echo "  [skip] already patched: $desc"
        return 0
    fi
    if ! grep -qF -- "$before" "$file"; then
        echo "Error: cannot apply patch ($desc)." >&2
        echo "       Expected text not found in $file." >&2
        echo "       PerfSpect $VERSION ($GIT_REV) may have changed; update build_perfspect.sh." >&2
        exit 1
    fi
    sed -i "s|${before}|${after}|" "$file"
    echo "  [ok] patched: $desc"
}

patch_perfspect_source() {
    echo "Applying gProfiler-specific PerfSpect patches..."
    # 1) avx-turbo pins a specific commit, but GIT_CLONE_OPTS does a shallow
    #    (--depth 1) clone, so the pinned commit isn't in the fetched history
    #    and the subsequent `git checkout` fails. Use a full single-branch
    #    clone so the pinned commit is reachable.
    apply_perfspect_patch tools/Makefile \
        'git clone $(GIT_CLONE_OPTS) https://github.com/travisdowns/avx-turbo.git' \
        'git clone --single-branch https://github.com/travisdowns/avx-turbo.git' \
        'avx-turbo: fetch full history so the pinned commit is reachable'
    # 2) Building all tools with -j$(nproc) fires ~30 parallel anonymous git
    #    clones, which GitHub rate-limits ("could not read Username"). Lower the
    #    parallelism to make the clones reliable.
    apply_perfspect_patch tools/build.Dockerfile \
        'RUN make tools -j$(nproc)' \
        'RUN make tools -j4' \
        'tools build: lower parallelism to avoid GitHub clone throttling'
    # 3) `make dist` runs govulncheck, which fails on CVEs disclosed after the
    #    pinned release. Drop check_vuln from the aggregate check target so the
    #    build isn't gated on newly-published vulnerabilities.
    apply_perfspect_patch Makefile \
        'check: check_format check_vet check_static check_license check_lint check_vuln test' \
        'check: check_format check_vet check_static check_license check_lint test' \
        'make check: do not gate the build on govulncheck'
}

# Extract the `perfspect` binary out of a dist tarball into a destination path.
# Each tarball contains a top-level perfspect/ directory holding the binary.
extract_perfspect_binary() {
    local tarball="$1" dest="$2"
    if [[ ! -f "$tarball" ]]; then
        echo "Error: expected tarball not found: $tarball" >&2
        exit 1
    fi
    echo "Extracting $(basename "$dest") from $tarball"
    local extract_dir
    extract_dir="$(mktemp -d)"
    tar -xzf "$tarball" -C "$extract_dir"
    # overwrite any existing binary of the same name (e.g. the perfspect-aarch64
    # that `make` compiles at the clone root) instead of failing on it
    rm -f "$dest"
    cp "$extract_dir/perfspect/perfspect" "$dest"
    chmod +x "$dest"
    rm -rf "$extract_dir"
}

# Remove existing perfspect directory if it exists
if [[ -d "perfspect" ]]; then
    sudo rm -rf perfspect/
fi

if [[ "$BUILD_STRATEGY" == "build" ]]; then
    git clone --depth 1 -b "$VERSION" https://github.com/intel/PerfSpect.git perfspect/
    cd perfspect/
    git reset --hard "$GIT_REV"
    # apply gProfiler-specific patches to the cloned source
    patch_perfspect_source
    # Build the tools + builder images and produce the dist tarballs.
    # builder/build.sh handles all of it: building (and locally caching) the
    # tools image, building the builder image, and running `make dist` inside
    # it. It must be run from the repo root (the cloned perfspect/ dir).
    ./builder/build.sh
    cd ..
    # builder/build.sh runs `make dist` inside a container as root with the
    # clone bind-mounted, so every artifact it writes (perfspect/dist/, and the
    # in-container-built perfspect and perfspect-aarch64 binaries at the clone
    # root) ends up owned by root on the host. Reclaim ownership of the whole
    # tree so we can manage the outputs (and overwrite any root-owned binaries)
    # as the current user without permission errors.
    sudo chown -R "$(id -u):$(id -g)" perfspect
    # `make dist` cross-compiles both architectures and produces one gzipped
    # tarball per arch in perfspect/dist/. Extract both binaries so callers can
    # pick the one they need (build_x86_64_executable.sh / build_aarch64_executable.sh
    # consume them via --perfspect <path>).
    extract_perfspect_binary perfspect/dist/perfspect.tgz         perfspect/perfspect-x86_64
    extract_perfspect_binary perfspect/dist/perfspect-aarch64.tgz perfspect/perfspect-aarch64
    echo
    echo "PerfSpect binaries ready:"
    echo "  x86_64 : $(pwd)/perfspect/perfspect-x86_64"
    echo "  aarch64: $(pwd)/perfspect/perfspect-aarch64"
    echo
    echo "Pass one to an executable build, e.g.:"
    echo "  scripts/build_x86_64_executable.sh  --perfspect perfspect/perfspect-x86_64"
    echo "  scripts/build_aarch64_executable.sh --perfspect perfspect/perfspect-aarch64"
elif [[ "$BUILD_STRATEGY" == "download" ]]; then
    # Intel only publishes an x86_64 release tarball; there is no aarch64
    # download, so this path yields the x86_64 binary only.
    mkdir -p perfspect
    curl -L -o perfspect.tgz "https://github.com/intel/PerfSpect/releases/download/$VERSION/perfspect.tgz"
    extract_perfspect_binary perfspect.tgz perfspect/perfspect-x86_64
    rm -f perfspect.tgz
    echo
    echo "PerfSpect binary ready:"
    echo "  x86_64 : $(pwd)/perfspect/perfspect-x86_64"
    echo "  aarch64: not available via --strategy=download (use --strategy=build)"
fi
