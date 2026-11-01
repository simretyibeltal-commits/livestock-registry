#!/usr/bin/env bash
# Helm release rollback helpers, sourced by ci/deploy/deploy-livestock-dev.sh.
#
#   last_deployed_revision <release> <namespace>   the last revision Helm recorded as `deployed`, or nothing
#   revision_chart         <release> <namespace> <revision>   the chart of that revision (name-version), or nothing
#   latest_status          <release> <namespace>   the status of the newest revision, or nothing
#   rollback_release <release> <namespace> <revision> <why> <report-file>
#                                                  save diagnostics, then `helm rollback` to <revision>
#
# No --atomic anywhere: a failed upgrade keeps its pods, events and hook logs long enough to be saved
# first, then the release is put back. A rollback restores the chart and values; it does NOT undo what
# hooks (migrations, seeds) wrote to a database.
# Python is only used to read Helm's JSON, so any python3 on the deploy node will do.

last_deployed_revision() {
  helm history "$1" -n "$2" -o json 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    rows = []
done = [r["revision"] for r in rows if r.get("status") == "deployed"]
print(done[-1] if done else "")
' || true
}

revision_chart() {
  helm history "$1" -n "$2" -o json 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    rows = []
want = int(sys.argv[1])
print(next((r.get("chart", "") for r in rows if r.get("revision") == want), ""))
' "$3" || true
}

latest_status() {
  helm history "$1" -n "$2" -o json 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    rows = []
print(rows[-1].get("status", "") if rows else "")
' || true
}

rollback_release() {
  local release="$1" ns="$2" prev="$3" why="$4" report="$5"
  local timeout="${HELM_ROLLBACK_TIMEOUT:-10m}" d
  set +e
  echo "FAILED: ${why}" >&2
  {
    echo "== failure: ${why}"
    echo "== helm history"; helm history "$release" -n "$ns" --max 6
    echo "== pods"; kubectl get pods -n "$ns" -l "app.kubernetes.io/instance=${release}" -o wide
    echo "== pods not Running/Succeeded"; kubectl get pods -n "$ns" --field-selector=status.phase!=Running,status.phase!=Succeeded
    echo "== warning events"; kubectl get events -n "$ns" --field-selector type=Warning --sort-by=.lastTimestamp | tail -40
    for d in staff-portal-api staff-portal-ui partner-api dashboard-api; do
      echo "== logs ${release}-${d}"
      kubectl logs "deploy/${release}-${d}" -n "$ns" --all-containers --prefix --tail=60 2>&1
    done
    echo "== jobs"; kubectl get jobs -n "$ns"
  } > "$report" 2>&1
  echo "diagnostics saved to ${report}" >&2
  if [ -n "$prev" ]; then
    echo "rolling back ${release} to revision ${prev}" >&2
    if helm rollback "$release" "$prev" -n "$ns" --wait --timeout "$timeout"; then
      echo "rolled back to revision ${prev}; the previous release is running again" >&2
    else
      echo "ROLLBACK FAILED: the release needs attention: helm -n ${ns} history ${release}" >&2
    fi
  else
    echo "no earlier deployed revision to roll back to (a first install cannot be rolled back)" >&2
  fi
  return 0
}
