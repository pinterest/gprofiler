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

VERSION=v3.11.0
GIT_REV="c0650037f2ac55cbd6bb7032bc0596d0c7393a0c"

git clone --depth 1 -b "$VERSION" https://github.com/intel/PerfSpect.git perfspect/
git config --global --add safe.directory "$(pwd)/perfspect"
cd perfspect/
git reset --hard "$GIT_REV"
# # Build resources first (required for go:embed to work)
# make resources
# if [ "$(uname -m)" = "aarch64" ]; then
#     make perfspect-aarch64
#     mv perfspect-aarch64 perfspect
# else
#     make perfspect
# fi
make dist