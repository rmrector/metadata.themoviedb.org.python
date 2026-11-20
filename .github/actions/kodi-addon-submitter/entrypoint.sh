#!/bin/sh -l
# Arguments: 1 kodi-repository, 2 kodi-version, 3 addon-id, 4 kodi-matrix,
#            5 sub-directory, 6 sync-fork
set -eu

REPO="$1"
BRANCH="$2"
ADDON_ID="$3"

# The workspace is mounted into the container with a different owner, which
# makes git refuse to work in it ("detected dubious ownership").
git config --global --add safe.directory "${GITHUB_WORKSPACE:-/github/workspace}"
git config --global --add safe.directory '*'

MATRIX=""
[ "${4:-}" = true ] && MATRIX="-m"
SUBDIRECTORY=""
[ "${5:-}" = true ] && SUBDIRECTORY="-s"
SYNC=""
[ "${6:-}" = true ] && SYNC="--sync-fork"

# Fail early, before doing any work, if the token / fork / branch setup is broken.
submit-addon --check -r "$REPO" -b "$BRANCH" $MATRIX $SYNC

# Build the zip(s)
submit-addon -z "$ADDON_ID" $MATRIX $SUBDIRECTORY

if [ -n "$MATRIX" ]; then
  echo "addon-zip=$(ls *.zip | awk '$0 !~ /\+matrix\./')" >> "$GITHUB_OUTPUT"
  echo "addon-zip-matrix=$(ls *+matrix*.zip)" >> "$GITHUB_OUTPUT"
else
  echo "addon-zip=$(ls *.zip)" >> "$GITHUB_OUTPUT"
  echo "addon-zip-matrix=" >> "$GITHUB_OUTPUT"
fi

# Submit
submit-addon -r "$REPO" -b "$BRANCH" --pull-request "$ADDON_ID" $MATRIX $SUBDIRECTORY $SYNC
