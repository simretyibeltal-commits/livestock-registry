#!/usr/bin/env bash
# Add the archives in built-chart/ (and built-chart/dashboard/) to repository/<channel>/ of oan-helm, whose workflow updates the
# index, changelog and rollback history. Needs GH_TOKEN with push access to oan-helm.
set -euo pipefail
: "${GH_TOKEN:?Bind the oan-helm credential}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git clone --quiet --depth 1 --filter=blob:none --sparse \
  https://github.com/Centre-for-Open-Societal-Systems/oan-helm.git "$WORK/oan-helm"
git -C "$WORK/oan-helm" sparse-checkout set scripts/ci
# The registry chart, and the dashboard chart when this build packaged one.
for archive in built-chart/*.tgz built-chart/dashboard/*.tgz; do
  [ -e "$archive" ] || continue
  status=0
  python3 "$WORK/oan-helm/scripts/ci/publish_chart.py" "$archive" || status=$?
  if [ "$status" -eq 4 ]; then
    echo "$(basename "$archive") was published by an earlier run of this commit."
  elif [ "$status" -ne 0 ]; then
    exit "$status"
  fi
done
