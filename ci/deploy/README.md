# Immutable central chart consumer

Install the deploy-agent dependency with `python3 -m pip install -r ci/deploy/requirements.txt`.
The registry-owned Jenkins job and agent/credential IDs are unchanged.

`oan-chart.json` records the COSS oan-helm commit, chart version, SHA-256, native
platform version, public-values checksum, release and environment. CI downloads
and validates the archive before Helm uses it. There is no local Helm source,
dependency update, moving branch URL or chart-version stamping.

Read-only verification: `python3 ci/deploy/oan_chart.py fetch registry <directory>`
and `python3 ci/deploy/oan_chart.py values registry <public-values.yaml>`.
Unit checks: `python3 -m unittest discover -s test -p test_oan_chart.py` and
`python3 -m unittest discover -s test -p test_deployment_guard.py`.

All deployment entry points call require-deployable first: a profile deploys only
when `promotionReady` is true for its environment (the dev profile is). Accept
domain/source/Kafka/AWE/MDM and preservation behavior before marking a new
immutable release/profile promotion-ready.
Staging requires its own environment profile and restored-data rehearsal; it
must not use the dev public-values overlay as a fallback.

Live metadata is read only by ci/refactor/new_dev_metadata.py with an explicit
authorized --credentials path. It never captures registry records or submits
approval actions. Credentials/cookies remain in memory, and metadata snapshots
remove credential values. Rehearse/compare scripts run serially using disposable
test databases; they do not bypass the real seed promotion preflight.

Crop's build agent runs `prefetch registry` and `prefetch dashboard` before
stashing `.central-charts/**`. Archives and public values are cached by SHA-256;
the VPN deploy node rechecks the exact package and values without GitHub access.
A corrupt cached archive fails closed and does not trigger a network fallback.
Manual runs can prefetch on a connected machine and carry that directory with
the same immutable profile. Cached archives are not editable Helm sources.

## Build, deploy, rollback, oan-helm pull request and pin updates (New Dev)

One pipeline (`ci/pipelines/dev.groovy`). Every `develop` build runs these stages (no parameter to tick; the
`NEW_DEV_DELIVERY_ACTION` choice only carries the deliberate maintenance runs `fresh-install`, `connector-plan` and
`connector-upgrade`):

1. **Build & Push Images** pushes the images to ECR (`eoan/livestock-registry/<image>-<RP version>:<version>-<branch>.<build>`, named by `ci/image_names.py`; the dashboard-api and dashboard-ui images too) and records each ECR digest in
   `.build/image-digests.tsv`. If this fails nothing after it runs and the log shows why.
2. **Package chart** (`ci/deploy/package-chart.sh`) packages `helm/openg2p-livestock-registry` with this
   build's images, using `scripts/ci/package_chart.py` of oan-helm. The version comes from git:
   `0.0.0-develop.<commit count>`. If this fails nothing is deployed.
3. **Deploy to Development** (`ci/deploy/deploy-livestock-dev.sh`) records the last `deployed` Helm revision (printed
   with its chart, for example `rollback target: revision 49 (openg2p-livestock-registry-1.0.0-dev.19, the last working
   release)`), upgrades the release with the packaged chart and public values and this build's image tags, waits for the
   rollouts and runs the guards. If anything fails from the upgrade onwards it saves `deploy-failure-<build>.txt` (pods,
   events, hook and application logs; archived by the build), runs `helm rollback` to that revision
   (`ci/deploy/release-rollback.sh`) and fails the build, so the next stage does not run. A first install has nothing
   to go back to. A rollback restores the chart and values; it does not undo database changes made by hooks. There is
   no `--atomic`, so the evidence survives. A release left in a `pending-*` Helm state is refused before any change.
4. **Publish chart** (`ci/deploy/publish-chart.sh`), only after a successful deploy, adds that same archive to
   `repository/develop/` of oan-helm, whose workflow updates the index, changelog and rollback history. If
   publishing fails the stage and the build are UNSTABLE; New Dev already runs the build. No pull request is
   involved. It uses the Jenkins credential `oan-helm-fork-token`, a maintainer's token for now.

`oan-chart.json` stays for a deploy by hand without `BUILT_CHART_DIR`; `ci/deploy/bump_pin.py` moves it.

Checks without a cluster or network:
`python3 -m unittest discover -s test -p test_deploy_ci.py -v`.
