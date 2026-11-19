#!/usr/bin/env bash
# deploy-ui.sh <registry> <namespace> <chart dir>
# Install the dashboard chart that 'Package chart' packaged with this build's UI image (<chart dir>/*.tgz, with the
# central dev values beside it as values.yaml); 'Publish chart' publishes the same archive to oan-helm. Offline: the
# deploy node needs no GitHub access.
set -euo pipefail
if [ "$(python3 ci/deploy/oan_chart.py is-deployable dashboard dev)" != "true" ]; then
  echo "SKIP: dashboard UI held back on dev (ci/deploy/oan-chart.json dashboard.promotionReady=false); registry deploy unaffected"
  exit 0
fi
python3 ci/deploy/oan_chart.py require-deployable dashboard dev
registry=$1
namespace=$2
built=$3
bash ci/dashboard/validate-deploy-files.sh "$registry"
release="${registry}-registry-dashboard"
chart=$(ls "$built"/"$release"-*.tgz)
values="$built/values.yaml"
test -s "$values"
# The image of the packaged chart, set again on the command line so values saved in the release cannot override it.
read -r image tag < <(helm show values "$chart" | python3 -c '
import sys
sys.path.insert(0, "ci/deploy")
from oan_chart import yaml_document
image = yaml_document(sys.stdin.read())["image"]
print(image["repository"], image["tag"])')
case "$tag" in *@sha256:*) ;; *) echo "The packaged dashboard chart has no image digest: $tag" >&2; exit 1 ;; esac
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# Preserve installed settings and Secrets; a new release uses the central public overlay.
saved=()
if helm status "$release" -n "$namespace" >/dev/null 2>&1; then
  (umask 077; helm get values "$release" -n "$namespace" --all -o yaml > "$work/current-values.yaml")
  test -s "$work/current-values.yaml"
  saved=(-f "$work/current-values.yaml")
fi
helm lint "$chart" "${saved[@]}" -f "$values" --set-string "image.repository=$image" --set-string "image.tag=$tag"
helm template "$release" "$chart" -n "$namespace" "${saved[@]}" -f "$values" \
  --set-string "image.repository=$image" --set-string "image.tag=$tag" > /dev/null
helm upgrade --install "$release" "$chart" -n "$namespace" "${saved[@]}" -f "$values" \
  --set-string "image.repository=$image" --set-string "image.tag=$tag" \
  --wait --wait-for-jobs --timeout 15m
