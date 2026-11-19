#!/usr/bin/env bash
# Package helm/openg2p-livestock-registry with this build's images into built-chart/, with
# scripts/ci/package_chart.py of oan-helm. The deploy installs that archive and publish-chart.sh
# publishes the same file. .build/image-digests.tsv (component, ECR repository, digest) comes from the stage
# Build & Push Images.
#
# When this build built the dashboard UI and the dashboard may be deployed on dev (ci/deploy/oan-chart.json
# dashboard.promotionReady), the pinned dashboard chart (verified by its checksum) is packaged the same way into
# built-chart/dashboard/, with its dev values beside it: the deploy installs that archive (ci/dashboard/deploy-ui.sh)
# and publish-chart.sh publishes it after. A dashboard that is held back is not packaged, so nothing that New Dev does
# not run is published.
set -euo pipefail
: "${BRANCH_NAME:?Jenkins sets the branch}"
: "${ECR_REGISTRY:?Missing ECR registry}"
: "${IMAGE_TAG:?Missing image tag}"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

: > "$WORK/dashboard-images.tsv"
while IFS=$'\t' read -r component repository digest; do
  case "$component" in
    staff-api) paths=registry.staffApi.image ;;
    partner-api) paths=registry.partnerApi.image ;;
    celery) paths='registry.celeryWorker.image registry.celeryBeat.image' ;;
    db-seed) paths=registry.dbSeed.image ;;
    sanity-tests) paths=registry.sanity.image ;;
    staff-ui) paths=registry.staffUi.image ;;
    dashboard-api) paths=dashboardApi.image ;;
    dashboard-ui)
      printf 'image\t%s\t%s@%s\n' "$ECR_REGISTRY/$repository" "$IMAGE_TAG" "$digest" >> "$WORK/dashboard-images.tsv"
      continue ;;
    *) echo "No chart image for $component" >&2; exit 1 ;;
  esac
  for path in $paths; do
    printf '%s\t%s\t%s@%s\n' "$path" "$ECR_REGISTRY/$repository" "$IMAGE_TAG" "$digest"
  done
done < .build/image-digests.tsv > "$WORK/images.tsv"

git clone --quiet --depth 1 --filter=blob:none --sparse \
  https://github.com/Centre-for-Open-Societal-Systems/oan-helm.git "$WORK/oan-helm"
git -C "$WORK/oan-helm" sparse-checkout set scripts/ci
python3 "$WORK/oan-helm/scripts/ci/package_chart.py" \
  --chart helm/openg2p-livestock-registry --images "$WORK/images.tsv" --output built-chart \
  --source-repository Centre-for-Open-Societal-Systems/livestock-registry \
  --source-commit "$(git rev-parse HEAD)" --subject "$(git log -1 --pretty=%s)" \
  --release livestock-registry --namespace livestock \
  --overlay environments/dev/values/livestock.yaml --branch "$BRANCH_NAME"

if [ -s "$WORK/dashboard-images.tsv" ] && [ "$(python3 ci/deploy/oan_chart.py is-deployable dashboard dev)" = "true" ]; then
  archive=$(python3 ci/deploy/oan_chart.py fetch dashboard "$WORK/central")
  name=$(python3 -c 'import json; print(json.load(open("ci/deploy/oan-chart.json"))["dashboard"]["name"])')
  overlay=$(python3 -c 'import json; print(json.load(open("ci/deploy/oan-chart.json"))["dashboard"]["values"]["path"])')
  mkdir -p "$WORK/source"
  tar -xzf "$archive" -C "$WORK/source"
  python3 "$WORK/oan-helm/scripts/ci/package_chart.py" \
    --chart "$WORK/source/$name" --images "$WORK/dashboard-images.tsv" --output built-chart/dashboard \
    --source-repository Centre-for-Open-Societal-Systems/livestock-registry \
    --source-commit "$(git rev-parse HEAD)" --subject "$(git log -1 --pretty=%s)" \
    --release "$name" --namespace livestock --overlay "$overlay" --branch "$BRANCH_NAME"
  python3 ci/deploy/oan_chart.py values dashboard built-chart/dashboard/values.yaml > /dev/null
fi
