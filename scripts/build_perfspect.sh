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
ARCH="x86_64"  # Default value

show_usage() {
    echo "Usage: $0 [--strategy=build|download] [--arch=x86_64|aarch64]"
    echo "  --strategy=build    Clone and build PerfSpect tools (default)"
    echo "  --strategy=download Download pre-built PerfSpect tools"
    echo "  --arch=x86_64       Target architecture x86_64 (default)"
    echo "  --arch=aarch64      Target architecture aarch64"
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
        --arch=*)
            ARCH="${1#*=}"
            if [[ "$ARCH" != "x86_64" && "$ARCH" != "aarch64" ]]; then
                echo "Error: Invalid architecture '$ARCH'. Must be 'x86_64' or 'aarch64'."
                show_usage
            fi
            shift
            ;;
        --arch)
            if [[ $# -lt 2 ]]; then
                echo "Error: --arch requires a value"
                show_usage
            fi
            ARCH="$2"
            if [[ "$ARCH" != "x86_64" && "$ARCH" != "aarch64" ]]; then
                echo "Error: Invalid architecture '$ARCH'. Must be 'x86_64' or 'aarch64'."
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
echo "Using ARCH: $ARCH"

VERSION=v3.11.0
GIT_REV="c0650037f2ac55cbd6bb7032bc0596d0c7393a0c"


# Remove existing perfspect directory if it exists
if [[ -d "perfspect" ]]; then
    sudo rm -rf perfspect/
fi

if [[ "$BUILD_STRATEGY" == "build" ]]; then
    git clone --depth 1 -b "$VERSION" https://github.com/intel/PerfSpect.git perfspect/
    cd perfspect/
    git reset --hard "$GIT_REV"
    # build tools image
    docker buildx build -f tools/build.Dockerfile --tag perfspect-tools:local tools/
    # modify the builder Dockerfile to use the fixed golang version
    sed -i 's|FROM golang:1\.25\.1@sha256:a5e935dbd8bc3a5ea24388e376388c9a69b40628b6788a81658a801abbec8f2e|FROM golang@sha256:516827db2015144cf91e042d1b6a3aca574d013a4705a6fdc4330444d47169d5|' builder/build.Dockerfile
    # build the perfspect builder image
    docker buildx build -f builder/build.Dockerfile --build-arg TAG=local --tag perfspect-builder:local .
    # build perfspect using the builder image
    docker container run                                  \
        --volume "$(pwd)":/localrepo                      \
        -w /localrepo                                     \
        --rm                                              \
        perfspect-builder:local                           \
        make dist
    cd ..
elif [[ "$BUILD_STRATEGY" == "download" ]]; then
    if [[ "$ARCH" != "x86_64" ]]; then
        echo "Download strategy is not supported for architecture '$ARCH'. Only x86_64 is supported for downloads."
        echo "Removing perfspect binary from gprofiler resources to avoid accidental usage."
        sed -i '/COPY perfspect\/perfspect gprofiler\/resources\/perfspect\/perfspect/d' executable.Dockerfile
        exit 0
    fi

    curl -L -o perfspect.tgz "https://github.com/intel/PerfSpect/releases/download/$VERSION/perfspect.tgz"
    tar -xzf perfspect.tgz
    rm perfspect.tgz
fi

# Move architecture-specific binary if building for aarch64
if [[ "$ARCH" == "aarch64" ]]; then
    mv perfspect/perfspect perfspect/perfspect-x86_64
    mv perfspect/perfspect-aarch64 perfspect/perfspect
fi
