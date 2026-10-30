#!/usr/bin/env bash

set -eu

# vault.centos.org now returns 403 for repodata *.sqlite.bz2; use the kernel.org vault mirror instead.
sed -i 's|mirror.centos.org/centos|archive.kernel.org/centos-vault/centos|g' /etc/yum.repos.d/*.repo
sed -i 's/^#.*baseurl=http/baseurl=http/g' /etc/yum.repos.d/*.repo
sed -i 's/^mirrorlist=http/#mirrorlist=http/g' /etc/yum.repos.d/*.repo
yum clean all
