#!/bin/sh
# Refresh the package lists before anything is installed.  The base tarball
# ships /etc/apt/sources.list.d/ubuntu.sources; nothing else is needed.
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
