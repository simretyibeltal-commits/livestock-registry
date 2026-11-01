#!/usr/bin/env bash
# Deploy a Livestock Registry build from ECR into New Dev (release
# livestock-registry, namespace livestock): the body of the Jenkins "Deploy to
# Development" stage, so the same deploy can be run by hand with the same
# credentials.
#
#   1. Verify the exact central chart archive and its public values
#      (ci/deploy/oan-chart.json; the pin must be promotion-ready).
#   2. Read the live release's values (hostnames, Keycloak/IAM wiring set on the
#      release) when a release exists; a first install goes ahead without them.
#   3. Render with this build's images, refuse placeholder domains, and run the
#      route and Farmer-reference guards on the rendered workloads.
#   4. helm upgrade --install, wait for the rollouts, deploy the dashboard UI and
#      run the Farmer-reference guard against the live Deployments.
#   5. If anything fails from the upgrade onwards, save the diagnostics
#      (deploy-failure-<build>.txt) and `helm rollback` to the last revision that was
#      `deployed` before this run (ci/deploy/release-rollback.sh), then fail. A first
#      install has nothing to go back to. Hooks' database changes are not undone.
#
# Chart release mode (OAN_CHART_PIN_FILE set): the develop build has put its own images into the oan-helm chart and
# waited for the release (ci/deploy/release_images.py). The pin of that release replaces the committed pin for this
# run, the saved release values lose their image settings so the chart's tags and digests apply, no image --set flags
# are passed, and the rendered manifests must name exactly the images in IMAGE_DIGESTS_FILE before anything changes.
# Without it (the release step was skipped or failed) the committed pin is deployed with this build's image tags.
#
# Env (Jenkins supplies them):
#   KUBECONFIG        livestock-dev-kubeconfig
#   DEV_NAMESPACE     default livestock
#   HELM_RELEASE      default livestock-registry
#   ECR_REGISTRY      <account>.dkr.ecr.<region>.amazonaws.com
#   ECR_PATH          default openg2p/livestock-registry
#   IMAGE_TAG         this build's image tag
#   DASHBOARD_UI_ECR, DASHBOARD_UI_SHA  dashboard UI image (ci/dashboard/deploy-ui.sh)
#   BUILD_NUMBER      names the archived rendered manifests
#   OAN_CHART_PIN_FILE  optional: pin file of the chart release made for this build
#   IMAGE_DIGESTS_FILE  with it: component<TAB>sha256:digest of this build's images
set -euo pipefail
set +x

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
: "${KUBECONFIG:?Jenkins must bind livestock-dev-kubeconfig}"
: "${ECR_REGISTRY:?set ECR_REGISTRY}"
: "${IMAGE_TAG:?set IMAGE_TAG}"
DEV_NAMESPACE="${DEV_NAMESPACE:-livestock}"
HELM_RELEASE="${HELM_RELEASE:-livestock-registry}"
ECR_PATH="${ECR_PATH:-openg2p/livestock-registry}"
BUILD_NUMBER="${BUILD_NUMBER:-local}"
# shellcheck source=release-rollback.sh
. "$REPO_ROOT/ci/deploy/release-rollback.sh"

# Runs on every exit. Once the upgrade has started, a non-zero exit (the upgrade, a rollout, the
# dashboard UI or the final guard) puts the release back before the build fails.
UPGRADE_STARTED=0
PREV=""
WATCHER_PID=""
on_exit() {
  local rc=$?
  trap - EXIT
  if [ -n "$WATCHER_PID" ]; then kill "$WATCHER_PID" 2>/dev/null || true; fi
  if [ "$rc" -ne 0 ] && [ "$UPGRADE_STARTED" = 1 ]; then
    rollback_release "$HELM_RELEASE" "$DEV_NAMESPACE" "$PREV" "deploy failed (exit ${rc})" "deploy-failure-${BUILD_NUMBER}.txt" || true
  fi
  rm -rf "${WORK:-}"
  exit "$rc"
}

CHART_RELEASE=0
if [ -n "${OAN_CHART_PIN_FILE:-}" ]; then
  CHART_RELEASE=1
  : "${IMAGE_DIGESTS_FILE:?chart release mode needs IMAGE_DIGESTS_FILE}"
  export OAN_CHART_PIN_FILE
  echo "chart release mode: deploying $(python3 ci/deploy/oan_chart.py version registry) from ${OAN_CHART_PIN_FILE}"
fi
python3 ci/deploy/oan_chart.py require-deployable registry dev
WORK=$(mktemp -d)
trap on_exit EXIT
CHART=$(python3 ci/deploy/oan_chart.py fetch registry "$WORK/chart")
CENTRAL_VALUES=$(python3 ci/deploy/oan_chart.py values registry "$WORK/public-values.yaml")
umask 077
# The live release's values. Only a missing release (a first install) may go
# ahead without them.
if ! helm get values "$HELM_RELEASE" -n "$DEV_NAMESPACE" --all -o yaml > "$WORK/current-values.yaml" 2> "$WORK/current.err"; then
  grep -q "release: not found" "$WORK/current.err" || { cat "$WORK/current.err" >&2; exit 1; }
  echo "No release $HELM_RELEASE in $DEV_NAMESPACE yet: installing on the chart's public values."
  : > "$WORK/current-values.yaml"
fi
if [ "$CHART_RELEASE" = 1 ]; then
  python3 ci/deploy/oan_chart.py strip-images registry "$WORK/current-values.yaml"
fi
# A release left half-way through an upgrade or rollback cannot be upgraded or rolled back: stop before changing anything.
case "$(latest_status "$HELM_RELEASE" "$DEV_NAMESPACE")" in
  pending-*) echo "FATAL: $HELM_RELEASE in $DEV_NAMESPACE is in a pending Helm operation; resolve it (helm history) before deploying." >&2; exit 1 ;;
esac
# The revision to go back to if this run fails: the last one Helm recorded as `deployed`.
PREV="$(last_deployed_revision "$HELM_RELEASE" "$DEV_NAMESPACE")"
if [ -n "$PREV" ]; then
  echo "rollback target: revision ${PREV} ($(revision_chart "$HELM_RELEASE" "$DEV_NAMESPACE" "$PREV"), the last working release)"
else
  echo "rollback target: none (no deployed revision found)"
fi

if [ "$CHART_RELEASE" = 1 ]; then
  # The chart carries the images (tag@digest); only the switches are set here.
  IMAGE_SET_FLAGS=(
    --set dashboardApi.enabled=true
    --set reporting.views.enabled=true
  )
else
  IMAGE_SET_FLAGS=(
    --set "registry.staffApi.image.repository=${ECR_REGISTRY}/${ECR_PATH}/staff-api"
    --set-string "registry.staffApi.image.tag=${IMAGE_TAG}"
    --set "registry.partnerApi.image.repository=${ECR_REGISTRY}/${ECR_PATH}/partner-api"
    --set-string "registry.partnerApi.image.tag=${IMAGE_TAG}"
    --set "registry.celeryWorker.image.repository=${ECR_REGISTRY}/${ECR_PATH}/celery"
    --set-string "registry.celeryWorker.image.tag=${IMAGE_TAG}"
    --set "registry.celeryBeat.image.repository=${ECR_REGISTRY}/${ECR_PATH}/celery"
    --set-string "registry.celeryBeat.image.tag=${IMAGE_TAG}"
    --set "registry.dbSeed.image.repository=${ECR_REGISTRY}/${ECR_PATH}/db-seed"
    --set-string "registry.dbSeed.image.tag=${IMAGE_TAG}"
    --set "registry.staffUi.image.repository=${ECR_REGISTRY}/${ECR_PATH}/staff-ui"
    --set-string "registry.staffUi.image.tag=${IMAGE_TAG}"
    --set "registry.sanity.image.repository=${ECR_REGISTRY}/${ECR_PATH}/sanity-tests"
    --set-string "registry.sanity.image.tag=${IMAGE_TAG}"
    --set dashboardApi.enabled=true
    --set "dashboardApi.image.repository=${ECR_REGISTRY}/${ECR_PATH}/dashboard-api"
    --set-string "dashboardApi.image.tag=${IMAGE_TAG}"
    --set reporting.views.enabled=true
  )
fi

helm template "$HELM_RELEASE" "$CHART" -n "$DEV_NAMESPACE" \
  -f "$WORK/current-values.yaml" -f "$CENTRAL_VALUES" "${IMAGE_SET_FLAGS[@]}" \
  > "newdev-rendered-${BUILD_NUMBER}.yaml"
if grep -q 'openg2p\.org' "newdev-rendered-${BUILD_NUMBER}.yaml"; then
  echo "FATAL: rendered manifests still reference the placeholder domain openg2p.org:" >&2
  grep -n 'openg2p\.org' "newdev-rendered-${BUILD_NUMBER}.yaml" | head -20 >&2
  exit 1
fi
if [ "$CHART_RELEASE" = 1 ]; then
  # Fail before the release changes if the chart does not run exactly this build's images.
  python3 - "$IMAGE_DIGESTS_FILE" "newdev-rendered-${BUILD_NUMBER}.yaml" "${ECR_REGISTRY}/${ECR_PATH}" "$IMAGE_TAG" <<'PY'
import sys
digests = dict(line.split("\t") for line in open(sys.argv[1]).read().split("\n") if line.strip())
rendered, base, tag = open(sys.argv[2]).read(), sys.argv[3], sys.argv[4]
wanted = ["%s/%s:%s@%s" % (base, c, tag, digests[c].strip()) for c in ("staff-api", "partner-api", "celery", "db-seed", "staff-ui", "sanity-tests")]
wanted.append("%s/dashboard-api@%s" % (base, digests["dashboard-api"].strip()))
missing = [w for w in wanted if w not in rendered]
if missing:
    print("FATAL: the chart release does not run this build's images; not in the rendered manifests:")
    print("\n".join("  " + m for m in missing))
    sys.exit(1)
print("chart release carries exactly this build's %d images" % len(wanted))
PY
fi
# Convert once and check each guard's exit status before Helm changes the
# release. Neither guard prints rendered Secrets or private key material.
kubectl create --dry-run=client --validate=false -n "$DEV_NAMESPACE" \
  -f "newdev-rendered-${BUILD_NUMBER}.yaml" -o json > "newdev-rendered-${BUILD_NUMBER}.json"
python3 ci/check-new-dev-routes.py livestock --release "$HELM_RELEASE" --namespace "$DEV_NAMESPACE" \
  < "newdev-rendered-${BUILD_NUMBER}.json"
python3 ci/refactor/deployment_guard.py --release "$HELM_RELEASE" --namespace "$DEV_NAMESPACE" --check-live-secret \
  < "newdev-rendered-${BUILD_NUMBER}.json"

# Hook pods are deleted as soon as their Job fails, so helm's own error
# ("post-upgrade hooks failed: ... BackoffLimitExceeded") says nothing about why.
# While helm waits, save the logs of this release's Job pods and list its Jobs
# every minute; print the saved logs if the upgrade fails.
mkdir -p "$WORK/logs"
save_hook_logs() {
  local p n
  for p in $(kubectl get pods -n "$DEV_NAMESPACE" -l job-name -o name 2>/dev/null | grep -E "^pod/${HELM_RELEASE}-" || true); do
    n="${p#pod/}"
    kubectl logs "$n" -n "$DEV_NAMESPACE" --all-containers --prefix --tail=200 > "$WORK/logs/$n.tmp" 2>/dev/null \
      && mv "$WORK/logs/$n.tmp" "$WORK/logs/$n.log" || rm -f "$WORK/logs/$n.tmp"
    kubectl logs "$n" -n "$DEV_NAMESPACE" --all-containers --prefix --tail=200 --previous > "$WORK/logs/$n.prev" 2>/dev/null \
      && mv "$WORK/logs/$n.prev" "$WORK/logs/$n.previous.log" || rm -f "$WORK/logs/$n.prev"
  done
}
(
  set +e
  tick=0
  while :; do
    save_hook_logs
    sleep 10
    tick=$((tick + 10))
    if [ "$tick" -ge 60 ]; then
      tick=0
      echo "--- $(date -u +%H:%M:%S) UTC: $HELM_RELEASE jobs ---"
      kubectl get jobs -n "$DEV_NAMESPACE" --no-headers 2>/dev/null | grep -E "^$HELM_RELEASE-" || true
    fi
  done
) &
WATCHER_PID=$!

UPGRADE_STARTED=1
if ! helm upgrade --install "$HELM_RELEASE" "$CHART" -n "$DEV_NAMESPACE" \
    -f "$WORK/current-values.yaml" -f "$CENTRAL_VALUES" "${IMAGE_SET_FLAGS[@]}" \
    --cleanup-on-fail --timeout 15m; then
  kill "$WATCHER_PID" 2>/dev/null || true
  save_hook_logs
  for f in "$WORK"/logs/*.log; do
    [ -f "$f" ] || continue
    echo "=== hook pod log: $(basename "$f" .log) ===" >&2
    cat "$f" >&2
  done
  echo "=== $HELM_RELEASE-staff-portal-api pod log ===" >&2
  kubectl logs "deploy/$HELM_RELEASE-staff-portal-api" -n "$DEV_NAMESPACE" --all-containers --prefix --tail=60 >&2 || true
  echo "=== release history ===" >&2
  helm history -n "$DEV_NAMESPACE" "$HELM_RELEASE" --max 5 >&2 || true
  echo "=== recent warning events ===" >&2
  kubectl get events -n "$DEV_NAMESPACE" --field-selector type=Warning --sort-by=.lastTimestamp 2>/dev/null | tail -20 >&2 || true
  exit 1
fi
kill "$WATCHER_PID" 2>/dev/null || true
# The sanity Job's output is useful even when it passes.
save_hook_logs
for f in "$WORK/logs/$HELM_RELEASE-sanity"*.log; do
  [ -f "$f" ] || continue
  echo "=== hook pod log: $(basename "$f" .log) ===" 
  cat "$f"
done

kubectl rollout status "deployment/$HELM_RELEASE-dashboard-api" -n "$DEV_NAMESPACE" --timeout=420s
if [ -n "${DASHBOARD_UI_SHA:-}" ]; then
  bash ci/dashboard/deploy-ui.sh livestock "$DEV_NAMESPACE" "${ECR_REGISTRY}/${DASHBOARD_UI_ECR:?set DASHBOARD_UI_ECR}" "$DASHBOARD_UI_SHA"
fi
for component in staff-portal-api staff-portal-ui partner-api celery-worker celery-beat-producer; do
  kubectl rollout status "deployment/$HELM_RELEASE-$component" -n "$DEV_NAMESPACE" --timeout=180s
done
kubectl get deployment "$HELM_RELEASE-staff-portal-api" "$HELM_RELEASE-celery-worker" "$HELM_RELEASE-celery-beat-producer" \
  -n "$DEV_NAMESPACE" -o json > "newdev-live-staff-api-${BUILD_NUMBER}.json"
python3 ci/refactor/deployment_guard.py --release "$HELM_RELEASE" --namespace "$DEV_NAMESPACE" --check-live-secret \
  < "newdev-live-staff-api-${BUILD_NUMBER}.json"
echo "Deployed $HELM_RELEASE ($IMAGE_TAG) to $DEV_NAMESPACE."
