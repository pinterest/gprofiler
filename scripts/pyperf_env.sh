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

if [ "$#" -gt 1 ]; then
    echo "Too many arguments"
    exit 1
elif [ "$#" -eq 0 ]; then
    with_staticx=""
elif [ "$1" == "--with-staticx" ]; then
    with_staticx="$1"
else
    echo "Unexpected argument: $1"
    exit 1
fi

if [ "$(uname -m)" = "aarch64" ]; then
    ./bcc_helpers_build.sh  # it needs to create dummy files
    exit 0;
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    iperf llvm-12-dev \
    clang-12 libclang-12-dev \
    cmake \
    flex \
    libfl-dev \
    bison \
    libelf-dev \
    libz-dev \
    liblzma-dev \
    ca-certificates \
    git \
    patchelf \
    make \
    libssl-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    wget \
    llvm \
    libncurses5-dev \
    libncursesw5-dev \
    xz-utils \
    tk-dev \
    libffi-dev \
    liblzma-dev \
    python-openssl

# Install specific python version.
curl -fsSL https://pyenv.run | bash
export PYENV_ROOT="$HOME/.pyenv"
[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init - bash)"
eval "$(pyenv virtualenv-init -)"

pyenv install 3.10
pyenv global 3.10

if [ -n "$with_staticx" ]; then
    if [ "$(uname -m)" = "aarch64" ]; then
        exit 0;
    fi
    python3 -m pip install --upgrade pip
    python3 -m pip install --upgrade setuptools
    git clone https://github.com/Granulate/staticx.git

    # We're using staticx to build a distribution-independent binary of PyPerf because PyPerf
    # can only build with latest llvm (>10), which cannot be obtained on CentOS.
    cd staticx
    git checkout 33eefdadc72832d5aa67c0792768c9e76afb746d # After fixing build deps
    # - apply patch to ensure staticx bootloader propagates dump signal to actual PyPerf binary
    # to avoid crashing the staticx bootloader on ubuntu:22.04+ and centos:8+
    git apply ../staticx_for_pyperf_patch.diff
    python3 -m pip install --no-cache-dir .
    cd ..
    rm -rf staticx
fi

./bcc_helpers_build.sh

apt-get clean
rm -rf /var/lib/apt/lists/*

