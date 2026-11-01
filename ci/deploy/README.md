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

## Images, chart release, deploy, rollback and pin (New Dev)

Every `develop` build runs these stages (no parameter to tick; `NEW_DEV_DELIVERY_ACTION`
only carries the deliberate maintenance runs `fresh-install`, `connector-plan`,
`connector-upgrade`):

1. **Build & Push Images** pushes the images to ECR (`<commit12>` tags) and records each
   ECR digest in `.build/image-digests.tsv`.
2. **Release images to oan-helm** (`ci/deploy/release_images.py`) writes the build's
   `<commit12>@sha256:<digest>` into `charts/openg2p-livestock-registry/values.yaml` of
   oan-helm (the seven registry image tags, and the dashboard-api tag and digest; only those
   lines change) on `bot/livestock-images-<commit12>`, opens a pull request, waits for its
   checks and merges it. oan-helm's release workflow then publishes the next chart version;
   the stage waits until the archive at the head of `main` carries exactly these images and
   writes that release's pin to `.build/oan-chart-candidate.json`. It needs `oan-ci-app`
   installed on oan-helm with Contents and Pull requests write, Checks read, and permission
   to merge there. If anything fails (no credential, a check fails, the merge needs a person,
   no release in 30 minutes) the stage is UNSTABLE and no candidate is written. Without a
   candidate the deploy stage upgrades nothing: images reach New Dev only through a chart
   release, so New Dev stays on the last working release and the build says so (UNSTABLE for a
   failed release stage). One chart release per commit merged to develop: a re-run, Replay or
   rebuild of a commit whose `<commit12>` tag is already in oan-helm `main` does nothing here
   (no pull request, no chart version, no CHANGELOG entry, stage SUCCESS) and nothing is
   deployed. A commit that only changes `ci/deploy/oan-chart.json` is not released again
   either, with the same result. The reason for a deliberate skip is written to
   `.build/release-note.txt` and printed by the deploy stage. If a release failed after its
   oan-helm pull request merged, re-running the same commit does not repeat it; the next new
   commit releases. Replaying an old commit after a newer one was released is not guarded: do
   not Replay old develop builds.
3. **Deploy to Development** (`ci/deploy/deploy-livestock-dev.sh`). With a candidate
   (`OAN_CHART_PIN_FILE`, chart release mode) it deploys that release, verified by SHA-256;
   the saved release values lose their image settings so the chart's tags and digests apply;
   no image `--set` flags are passed; and before anything changes the rendered manifests
   must contain exactly the images of `IMAGE_DIGESTS_FILE`. The script records the last
   `deployed` Helm revision (printed with its chart, e.g. `rollback target: revision 49
   (openg2p-livestock-registry-1.0.0-dev.19, the last working release)`), upgrades, waits for
   the rollouts and runs the guards. If anything fails from the upgrade onwards it saves
   `deploy-failure-<build>.txt` (archived), runs `helm rollback` to that revision
   (`ci/deploy/release-rollback.sh`) and fails the build. A first install has nothing to go
   back to. A rollback restores the chart and values; it does not undo database changes made
   by hooks. No `--atomic`, so the evidence survives; a `pending-*` release is refused.
4. **Propose chart pin update** (`ci/deploy/propose_pin_pr.py`), after a successful deploy.
   With the build's candidate it opens, or updates, one rolling pull request
   `bot/oan-chart-pin` to `develop` that records exactly the release that was deployed in
   `oan-chart.json` (version, SHA-256, oan-helm commit, public values, platform). A person
   merges it; merging it starts a build that releases nothing new (pin-only commit). Without
   a candidate it compares the pin with the head of oan-helm `main` as before. It refuses a
   downgrade and a changed archive for the same version, and does not propose a chart that
   needs another platform version (the Dockerfiles and the pipeline must move with it: run
   `scripts/bump-rp-version.sh`, move `RP_VERSION` and the `openg2p/openg2p-registry-*` tags in
   `ci/pipelines/dev.groovy` and `ci/refactor`, then `python3 ci/deploy/bump_pin.py <commit> --write`).
   The `dashboard` pin is separate and is not touched.

`DRY_RUN=1 python3 ci/deploy/release_images.py` (with `IMAGE_TAG`, `ECR_REGISTRY` and
`.build/image-digests.tsv`) prints the tags it would write and changes nothing.

Checks without a cluster or network:
`python3 -m unittest discover -s test -p test_deploy_ci.py -v`.
