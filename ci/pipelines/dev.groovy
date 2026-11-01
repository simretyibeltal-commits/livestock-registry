def call() {

pipeline {
    parameters {
        choice(name: 'NEW_DEV_DELIVERY_ACTION', choices: ['deploy', 'fresh-install', 'connector-plan', 'connector-upgrade'],
            description: 'develop only. There is nothing to choose for a normal build: every develop build (push, merge or poll) builds and pushes the images, upgrades the existing New Dev release with them and, if the upgrade or a rollout fails, rolls the release back to the last deployed revision (deploy, the default). The other actions are deliberate maintenance runs. fresh-install backs up the New Dev livestock databases to the New Dev box, uninstalls the release, drops the databases and installs the pinned chart again on an empty database (all New Dev Livestock data is lost; Commons, the connector and the Farmer registry are untouched). connector-plan prints, without changing anything, the manifest diff and server-side dry run of the livestock-connector release against ci/connector. connector-upgrade builds no images and leaves the registry release alone: it archives the connector database to the New Dev box and upgrades the livestock-connector release to ci/connector (chart + values, --reset-values), printing the manifest diff and a server-side dry run first.')
    }
    // Validation and image builds run on any build agent; only the deploy
    // stages below run on vpn-agent2, the one node that reaches the clusters.
    // Building the staff UI there starved the node (its Next build took the
    // agent offline repeatedly) and blocked every registry's deploy behind it.
    agent any

    options {
        // One run at a time: merging several PRs back to back queued
        // overlapping runs whose helm upgrades collided ("another
        // operation in progress"), which is what showed up as a failed
        // db-seed on dev on 2026-09-18.
        disableConcurrentBuilds()
    }

    environment {
        AWS_REGION       = 'ap-south-1'
        ECR_PATH         = 'openg2p/livestock-registry'
        DASHBOARD_UI_ECR = 'oan/livestock-registry/dashboard-ui'
        RP_VERSION       = '1.2.2-rc.614' // Same native platform as the immutable central profile
        HELM_RELEASE     = 'livestock-registry'
        HELM_NAMESPACE   = 'live'
        // Chart ownership: immutable COSS oan-helm profile in ci/deploy/oan-chart.json.
        // Staging stays blocked until separately rehearsed against its existing owner.

        // Dev cluster: namespace `livestock`, hosts on *.development.oanstaging.com,
        // credential bound to the livestock:livestock-ci ServiceAccount. Values come
        // from the same central commit as the chart, with complete saved release values.
        //
        // The previous dev cluster (namespace `live`, *.openg2p.test) is RETIRED; its
        // stage and values-dev.yaml were removed, so there is now one dev target.
        DEV_NAMESPACE            = 'livestock'
        DEV_KUBECONFIG_CRED_ID   = 'livestock-dev-kubeconfig'
        // GitHub App credential used by the last stage to push a branch and open the chart-pin pull request.
        PIN_PR_CRED_ID           = 'oan-ci-app'

        // livestock-registry-dashboard-api lives in its own (public) repository
        // and is built here beside the registry images. Each branch builds the
        // dashboard-api branch of the same name and falls back to develop where
        // there is none. Set DASHBOARD_API_REF on the job (a branch or tag) to
        // pin one instead.
        DASHBOARD_API_REPO = 'https://github.com/Centre-for-Open-Societal-Systems/livestock-registry-dashbaord-api.git'
        DASHBOARD_UI_REPO = 'https://github.com/Centre-for-Open-Societal-Systems/oan_livestock_dashboard.git'
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('Check chart version') {
            steps {
                sh 'bash scripts/check-chart-version.sh'
            }
        }

        stage('Validate refactor contracts') {
            steps {
                sh 'docker run --rm --network none --entrypoint python -v "$PWD:/work:ro" -w /work openg2p/openg2p-registry-staff-api:1.2.2-rc.614 ci/refactor/check_port.py . livestock'
                sh 'docker build --quiet -f ci/refactor/profile-tests.Dockerfile -t oan/livestock-reference-tests:rc557 .'
                sh 'docker run --rm --memory=768m --network none --entrypoint bash -v "$PWD:/work:ro" -w /work -e REGISTRY_EXTENSION_MODULE=openg2p_registry_livestock_extension -e PYTHONPATH=/work/livestock-extension/src oan/livestock-reference-tests:rc557 ci/validate.sh --native'
                sh 'docker run --rm --network none --entrypoint python -v "$PWD:/work:ro" -w /work oan/livestock-reference-tests:rc557 -m unittest discover -s test -p test_oan_chart.py -v'
                sh 'docker run --rm --network none --entrypoint python -v "$PWD:/work:ro" -w /work oan/livestock-reference-tests:rc557 -m unittest discover -s test -p test_deployment_guard.py -v'
                sh 'docker run --rm --network none --entrypoint python -v "$PWD:/work:ro" -w /work oan/livestock-reference-tests:rc557 -m unittest discover -s test -p test_onboarding_runner_failure.py -v'
                // Rollback of a failed deploy and the chart-pin updater, with stand-in helm/kubectl and GitHub (no cluster, no network).
                sh 'python3 -m unittest discover -s test -p test_deploy_ci.py -v'
                sh 'docker run --rm --network none --entrypoint python -v "$PWD:/work:ro" -w /work -e REGISTRY_EXTENSION_MODULE=openg2p_registry_livestock_extension -e PYTHONPATH=/work/livestock-extension/src oan/livestock-reference-tests:rc557 -m unittest discover -s livestock-extension/tests -p "test_farmer_reference_*.py" -v'
            }
        }

        stage('Validate native metadata and notification mapping') {
            steps {
                sh 'COSS_REFACTOR_TEST_IMAGE=oan/livestock-reference-tests:rc557 python3 ci/refactor/rehearse_metadata.py livestock --pipeline'
                sh 'python3 ci/refactor/compare_new_dev.py livestock'
                archiveArtifacts artifacts: 'ci-output/refactor/new-dev/seed-rehearsal.json,ci-output/refactor/new-dev/comparison.json,ci-output/refactor/NATIVE_NOTIFICATION_PIPELINE.json', allowEmptyArchive: false
            }
        }

        stage('Checkout dashboard-api') {
            // TEMPORARY: livestock-registry-dashboard-api has not been merged to its
            // staging branch yet, so staging builds skip it (Deploy to Live never
            // uses the dashboard-api image). Drop this `when` once it is merged.
            when { not { branch 'staging' } }
            steps {
                script {
                    // A PR build (BRANCH_NAME PR-<n>) matches on its source branch.
                    def ref = env.DASHBOARD_API_REF
                    if (!ref) {
                        def wanted = env.CHANGE_BRANCH ?: env.BRANCH_NAME
                        def found = wanted && sh(returnStatus: true,
                            script: "git ls-remote --exit-code --heads ${DASHBOARD_API_REPO} 'refs/heads/${wanted}' > /dev/null") == 0
                        ref = found ? wanted : 'develop'
                    }
                    // .build/ is git-ignored, so the clone never enters a commit.
                    sh "rm -rf .build/dashboard-api && git clone --quiet --depth 1 --branch '${ref}' ${DASHBOARD_API_REPO} .build/dashboard-api"
                    if (!fileExists('.build/dashboard-api/Dockerfile')) {
                        error("dashboard-api ${ref} has no Dockerfile: merge the service into that branch of ${DASHBOARD_API_REPO}, or set DASHBOARD_API_REF")
                    }
                    env.DASHBOARD_API_REF_USED = ref
                    env.DASHBOARD_API_SHA = sh(returnStdout: true, script: 'git -C .build/dashboard-api rev-parse --short=12 HEAD').trim()
                    echo "dashboard-api: ${ref} @ ${env.DASHBOARD_API_SHA}"
                    sh 'bash ci/dashboard/verify-api.sh .build/dashboard-api'
                }
            }
        }

        stage('Checkout dashboard-ui') {
            // TEMPORARY: livestock-registry-dashboard-ui has not been merged to its
            // staging branch yet, so staging builds skip it (Deploy to Live never
            // uses the dashboard-ui image). Drop this `when` once it is merged.
            when { not { branch 'staging' } }
            steps {
                script {
                    // A PR build (BRANCH_NAME PR-<n>) matches on its source branch.
                    def ref = env.DASHBOARD_UI_REF
                    if (!ref) {
                        def wanted = env.CHANGE_BRANCH ?: env.BRANCH_NAME
                        def found = wanted && sh(returnStatus: true,
                            script: "git ls-remote --exit-code --heads ${DASHBOARD_UI_REPO} 'refs/heads/${wanted}' > /dev/null") == 0
                        ref = found ? wanted : 'develop'
                    }
                    // .build/ is git-ignored, so the clone never enters a commit.
                    sh "rm -rf .build/dashboard-ui && git clone --quiet --depth 1 --branch '${ref}' ${DASHBOARD_UI_REPO} .build/dashboard-ui"
                    if (!fileExists('.build/dashboard-ui/Dockerfile')) {
                        error("dashboard-ui ${ref} has no Dockerfile: merge the service into that branch of ${DASHBOARD_UI_REPO}, or set DASHBOARD_UI_REF")
                    }
                    env.DASHBOARD_UI_REF_USED = ref
                    env.DASHBOARD_UI_SHA = sh(returnStdout: true, script: 'git -C .build/dashboard-ui rev-parse --short=12 HEAD').trim()
                    echo "dashboard-ui: ${ref} @ ${env.DASHBOARD_UI_SHA}"
                    sh 'bash ci/dashboard/verify-source.sh .build/dashboard-ui'
                }
            }
        }

        stage('Build & Push Images') {
            when { allOf { not { changeRequest() }; anyOf { branch 'develop'; branch 'staging' }
                           expression { !((params.NEW_DEV_DELIVERY_ACTION ?: 'deploy') in ['connector-plan', 'connector-upgrade']) } } }
            // Per-build Docker config: login, push and logout never touch the agent user's shared ~/.docker/config.json, so concurrent builds on one host cannot invalidate each other's ECR credentials.
            environment { DOCKER_CONFIG = "${env.WORKSPACE}/.docker-${env.BUILD_TAG}" }
            steps {
                withCredentials([
                    string(credentialsId: 'AWS_ACCOUNT_ID', variable: 'AWS_ACCOUNT_ID'),
                    [$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds']
                ]) {
                    script {
                        env.ECR_REGISTRY = "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
                        env.IMAGE_TAG    = env.GIT_COMMIT.take(12)
                    }
                    sh '''
                        echo "=== Logging in to ECR ==="
                        # Keep the buildx/compose CLI plugins visible from the per-build config dir.
                        mkdir -p "$DOCKER_CONFIG"; chmod 700 "$DOCKER_CONFIG"; if [ -d "$HOME/.docker/cli-plugins" ]; then ln -sfn "$HOME/.docker/cli-plugins" "$DOCKER_CONFIG/cli-plugins"; fi
                        aws ecr get-login-password --region ${AWS_REGION} | \
                            docker login --username AWS --password-stdin ${ECR_REGISTRY}
                    '''
                    script {
                        def components = ['staff-api', 'partner-api', 'celery', 'db-seed', 'sanity-tests', 'staff-ui', 'registry-connector', 'registry-connector-ui']
                        components.each { name ->
                            def image = "${env.ECR_REGISTRY}/${ECR_PATH}/${name}:${env.IMAGE_TAG}"
                            sh """
                                echo "=== Building and pushing ${name} ==="
                                docker build --build-arg RP_VERSION=${RP_VERSION} \
                                    -f docker/${name}/Dockerfile -t ${image} --pull .
                                # ECR does not create repositories on push (registry-connector is new).
                                aws ecr describe-repositories --region ${AWS_REGION} --repository-names ${ECR_PATH}/${name} > /dev/null 2>&1 \
                                  || aws ecr create-repository --region ${AWS_REGION} --repository-name ${ECR_PATH}/${name} > /dev/null
                                docker push ${image}
                            """
                            if (name == 'registry-connector') {
                                // The connector extension's tests, inside the image just pushed.
                                sh """
                                    docker run --rm -u 0 -v "\$PWD/docker/registry-connector:/ext:ro" -w /ext -e PYTHONDONTWRITEBYTECODE=1 \
                                      --entrypoint sh ${image} -c \
                                      'pip install --quiet --no-cache-dir --root-user-action=ignore pytest==8.4.2 && python -m pytest -q -p no:cacheprovider tests'
                                """
                            }
                        }

                        if (env.DASHBOARD_UI_SHA) {
                            def ui = "${env.ECR_REGISTRY}/${DASHBOARD_UI_ECR}:${env.DASHBOARD_UI_SHA}"
                            sh """
                                docker build --label org.opencontainers.image.revision=${env.DASHBOARD_UI_SHA} \
                                  -f .build/dashboard-ui/Dockerfile -t ${ui} .build/dashboard-ui
                                docker push ${ui}
                            """
                        }
                        // Skipped when 'Checkout dashboard-api' did not run (staging).
                        if (env.DASHBOARD_API_SHA) {
                            // The dashboard service, from its own repository (cloned
                            // by 'Checkout dashboard-api'), with its own build context.
                            // Its ECR repository is created outside CI, like the others.
                            def api = "${env.ECR_REGISTRY}/${ECR_PATH}/dashboard-api:${env.IMAGE_TAG}"
                            sh """
                                echo "=== Building and pushing dashboard-api (${env.DASHBOARD_API_REF_USED} @ ${env.DASHBOARD_API_SHA}) ==="
                                docker build \
                                    --label org.opencontainers.image.source=${DASHBOARD_API_REPO} \
                                    --label org.opencontainers.image.revision=${env.DASHBOARD_API_SHA} \
                                    --label org.opencontainers.image.ref.name=${env.DASHBOARD_API_REF_USED} \
                                    -f .build/dashboard-api/Dockerfile -t ${api} .build/dashboard-api
                                docker push ${api}
                            """
                        }

                        // The ECR digest of every image the oan-helm chart carries: the next stage writes
                        // <commit>@<digest> into the chart, and the deploy checks the manifests against these.
                        def digested = ['staff-api', 'partner-api', 'celery', 'db-seed', 'sanity-tests', 'staff-ui']
                        if (env.DASHBOARD_API_SHA) { digested << 'dashboard-api' }
                        sh """
                            set -eu
                            mkdir -p .build
                            : > .build/image-digests.tsv
                            for c in ${digested.join(' ')}; do
                                d=\$(aws ecr describe-images --region ${AWS_REGION} --repository-name ${ECR_PATH}/\$c \
                                      --image-ids imageTag=${env.IMAGE_TAG} --query 'imageDetails[0].imageDigest' --output text)
                                case "\$d" in sha256:*) ;; *) echo "no ECR digest for \$c:${env.IMAGE_TAG}" >&2; exit 1 ;; esac
                                printf '%s\\t%s\\n' "\$c" "\$d" >> .build/image-digests.tsv
                            done
                        """
                        stash name: 'image-digests', includes: '.build/image-digests.tsv'
                    }
                }
            }
        }

        stage('Stash deploy') {
            when { allOf { not { changeRequest() }; anyOf { branch 'develop'; branch 'staging' } } }
            // The deploy node gets the scripts of exactly this commit and the
            // verified central chart archive, not a checkout of its own: it
            // deploys offline, without GitHub access.
            steps {
                sh 'docker run --rm --user "$(id -u):$(id -g)" --entrypoint python -v "$PWD:/work" -w /work openg2p/openg2p-registry-staff-api:1.2.2-rc.614 ci/deploy/oan_chart.py prefetch registry'
                stash name: 'deploy', includes: 'ci/**,scripts/**,.central-charts/**'
            }
        }

        stage('Release images to oan-helm') {
            // Turns this build into a Helm release: the image tags and digests go into the oan-helm chart through a
            // pull request that is merged when its checks pass, oan-helm's release workflow publishes the next chart
            // version, and the pin of that release (.build/oan-chart-candidate.json) is what the deploy uses. If
            // anything here fails the stage is UNSTABLE and there is no candidate: the deploy stage then leaves the
            // cluster on the last working release (no upgrade, no image overrides). One release per commit merged to
            // develop: a re-run of a commit already in oan-helm, and a commit that only changes the pin, release
            // nothing (the reason is written to .build/release-note.txt).
            when {
                beforeAgent true
                branch 'develop'
                expression { !((params.NEW_DEV_DELIVERY_ACTION ?: 'deploy') in ['fresh-install', 'connector-plan', 'connector-upgrade']) }
            }
            agent { label 'vpn-agent2' }
            options { timeout(time: 75, unit: 'MINUTES') }
            steps {
                checkout scm
                sh 'mkdir -p .build && rm -f .build/oan-chart-candidate.json .build/release-note.txt .build/image-digests.tsv'
                unstash 'image-digests'
                script {
                    def subject = sh(returnStdout: true, script: 'git log -1 --pretty=%s').trim()
                    catchError(buildResult: 'UNSTABLE', stageResult: 'UNSTABLE') {
                        withCredentials([usernamePassword(credentialsId: "${PIN_PR_CRED_ID}", usernameVariable: 'GH_USER', passwordVariable: 'GH_TOKEN')]) {
                            withEnv(["COMMIT_SUBJECT=${subject}", "SOURCE_COMMIT=${env.GIT_COMMIT}", "ECR_PATH=${ECR_PATH}"]) {
                                sh '''
                                    set -eu
                                    python3 ci/deploy/release_images.py
                                    if [ -f .build/oan-chart-candidate.json ]; then
                                        # Verify the release and cache it, so the deploy node needs no GitHub access.
                                        OAN_CHART_PIN_FILE=.build/oan-chart-candidate.json python3 ci/deploy/oan_chart.py prefetch registry \
                                          || { rm -f .build/oan-chart-candidate.json; exit 1; }
                                    fi
                                '''
                            }
                        }
                    }
                    stash name: 'candidate', includes: '.build/oan-chart-candidate.json,.build/release-note.txt,.build/image-digests.tsv,.central-charts/**', allowEmpty: true
                }
            }
        }

        stage('Prepare Deploy Values') {
            // Split out 2026-09-16 so both deploy targets (dev cluster and
            // staging cluster's `live` namespace) share the exact same
            // image-tag overlay file instead of two copies that could drift.
            // Content is cluster/namespace-agnostic (image repo+tag only) —
            // safe to reuse verbatim against either kubeconfig.
            //
            // Gated on the SAME branch as "Deploy to Live" below (not
            // 'develop' anymore) — this stage exists solely to feed that
            // one. See the branch-gating header note above.
            when {
                beforeAgent true
                branch 'staging' // ASSUMPTION — confirm the real staging branch name
            }
            agent { label 'vpn-agent2' }
            steps {
                sh """
                    cat > /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml <<EOF
registry:
  staffApi:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/staff-api
      tag: "${env.IMAGE_TAG}"
  partnerApi:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/partner-api
      tag: "${env.IMAGE_TAG}"
  celeryWorker:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/celery
      tag: "${env.IMAGE_TAG}"
  celeryBeat:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/celery
      tag: "${env.IMAGE_TAG}"
  dbSeed:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/db-seed
      tag: "${env.IMAGE_TAG}"
  staffUi:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/staff-ui
      tag: "${env.IMAGE_TAG}"
  sanity:
    image:
      repository: ${env.ECR_REGISTRY}/${ECR_PATH}/sanity-tests
      tag: "${env.IMAGE_TAG}"
EOF
                """
            }
        }

        stage('Deploy to Development') {
            // Use the existing development job, namespace and credentials. The
            // chart bytes and version are immutable (ci/deploy/oan-chart.json);
            // ci/deploy/deploy-livestock-dev.sh renders, guards and upgrades.
            when {
                beforeAgent true
                branch 'develop'
                // Every develop build deploys: there is no publish-only mode. An empty value, or
                // the retired 'publish-only' that Jenkins may still pass on the first run after
                // this change, is a normal deploy. Only the connector maintenance actions skip it.
                expression { !((params.NEW_DEV_DELIVERY_ACTION ?: 'deploy') in ['connector-plan', 'connector-upgrade']) }
            }
            agent { label 'vpn-agent2' }
            options { timeout(time: 60, unit: 'MINUTES') }
            steps {
                sh 'rm -rf ci scripts .central-charts && mkdir -p .build && rm -f .build/oan-chart-candidate.json .build/release-note.txt .build/image-digests.tsv'
                unstash 'deploy'
                script {
                    // The chart release made from this build's images (stage "Release images to oan-helm"). Without one
                    // (the stage failed, or this commit was already released, or it only moves the pin) nothing is
                    // upgraded: images reach New Dev only through a chart release, so the cluster stays on the last
                    // working release. A failed release stage leaves the build UNSTABLE; a deliberate skip does not.
                    try { unstash 'candidate' } catch (ignored) { echo 'No candidate was handed over by the release stage.' }
                    env.NEWDEV_SKIP_DEPLOY = (params.NEW_DEV_DELIVERY_ACTION != 'fresh-install' && !fileExists('.build/oan-chart-candidate.json')) ? 'yes' : 'no'
                    if (env.NEWDEV_SKIP_DEPLOY == 'yes') {
                        if (fileExists('.build/release-note.txt')) {
                            echo readFile('.build/release-note.txt').trim()
                        } else {
                            unstable('No chart release was made for this build (see the stage "Release images to oan-helm"). Nothing was deployed: New Dev stays on the last working release.')
                        }
                    }
                }
                // A failed deploy is rolled back by ci/deploy/deploy-livestock-dev.sh and then fails
                // the build: no catchError, so it is never reported as a pass.
                withCredentials([file(credentialsId: "${DEV_KUBECONFIG_CRED_ID}", variable: 'KUBECONFIG')]) {
                    script {
                      if (env.NEWDEV_SKIP_DEPLOY == 'yes') {
                        echo 'Skipping the upgrade: no chart release for this build.'
                      } else {
                        def deployEnv = [
                            "DEV_NAMESPACE=${DEV_NAMESPACE}", "ECR_REGISTRY=${env.ECR_REGISTRY}", "ECR_PATH=${ECR_PATH}",
                            "IMAGE_TAG=${env.IMAGE_TAG}", "DASHBOARD_UI_ECR=${DASHBOARD_UI_ECR}", "DASHBOARD_UI_SHA=${env.DASHBOARD_UI_SHA}",
                        ]
                        if (fileExists('.build/oan-chart-candidate.json') && params.NEW_DEV_DELIVERY_ACTION != 'fresh-install') {
                            deployEnv += ["OAN_CHART_PIN_FILE=.build/oan-chart-candidate.json", "IMAGE_DIGESTS_FILE=.build/image-digests.tsv"]
                        }
                        withEnv(deployEnv) {
                            if (params.NEW_DEV_DELIVERY_ACTION == 'fresh-install') {
                                // The database archive goes to the New Dev box over the existing backup key.
                                withCredentials([sshUserPrivateKey(credentialsId: 'farmer-new-dev-backup-ssh', keyFileVariable: 'BACKUP_SSH_KEY')]) {
                                    sh 'bash ci/deploy/fresh-install-livestock-dev.sh'
                                }
                            } else {
                                sh 'bash ci/deploy/deploy-livestock-dev.sh'
                            }
                        }
                      }
                    }
                }
            }
            post {
                always {
                    archiveArtifacts artifacts: 'newdev-rendered-*.yaml, deploy-failure-*.txt', allowEmptyArchive: true
                }
            }
        }

        stage('Upgrade connector (New Dev)') {
            // The livestock-connector release from git: ci/connector/chart with
            // ci/connector/livestock-connector.values.yaml. Upgrade only; the
            // connector database is archived to the New Dev box first.
            when {
                beforeAgent true
                branch 'develop'
                expression { params.NEW_DEV_DELIVERY_ACTION in ['connector-plan', 'connector-upgrade'] }
            }
            agent { label 'vpn-agent2' }
            steps {
                sh 'rm -rf ci scripts .central-charts'
                unstash 'deploy'
                script {
                    if (params.NEW_DEV_DELIVERY_ACTION == 'connector-plan') {
                        withCredentials([file(credentialsId: "${DEV_KUBECONFIG_CRED_ID}", variable: 'KUBECONFIG')]) {
                            sh 'PLAN_ONLY=1 bash ci/connector/upgrade-livestock-connector.sh'
                        }
                    } else {
                        withCredentials([file(credentialsId: "${DEV_KUBECONFIG_CRED_ID}", variable: 'KUBECONFIG'),
                                         sshUserPrivateKey(credentialsId: 'farmer-new-dev-backup-ssh', keyFileVariable: 'BACKUP_SSH_KEY')]) {
                            sh 'bash ci/connector/upgrade-livestock-connector.sh'
                        }
                    }
                }
            }
        }

        stage('Propose chart pin update') {
            // After a successful deploy: if oan-helm main has a newer Livestock chart or changed
            // public values than ci/deploy/oan-chart.json, open a pull request to develop that moves
            // the pin (branch bot/oan-chart-pin-*). It never deploys and never changes develop; a
            // person reviews and merges it, and the next develop build deploys the new pin. A
            // failure here (no GitHub access, a refused push) only marks this stage UNSTABLE.
            when {
                beforeAgent true
                branch 'develop'
                expression { !((params.NEW_DEV_DELIVERY_ACTION ?: 'deploy') in ['fresh-install', 'connector-plan', 'connector-upgrade']) }
            }
            agent { label 'vpn-agent2' }
            options { timeout(time: 10, unit: 'MINUTES') }
            steps {
                checkout scm
                sh 'mkdir -p .build && rm -f .build/oan-chart-candidate.json .build/image-digests.tsv'
                script {
                    // With the chart release this build made and deployed, the pull request records exactly that pin.
                    try { unstash 'candidate' } catch (ignored) { echo 'No candidate was handed over by the release stage.' }
                    if (!fileExists('.build/oan-chart-candidate.json')) {
                        echo 'This build deployed no chart release: the pin is not changed.'
                        return
                    }
                    def withCandidate = ['OAN_CHART_PIN_FILE=.build/oan-chart-candidate.json']
                    catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE') {
                        withCredentials([usernamePassword(credentialsId: "${PIN_PR_CRED_ID}", usernameVariable: 'GH_USER', passwordVariable: 'GH_TOKEN')]) {
                            withEnv(withCandidate) {
                                sh 'python3 ci/deploy/propose_pin_pr.py'
                            }
                        }
                    }
                }
            }
        }

        stage('Deploy to Live') {
            // CHANGED 2026-09-22: was `when { branch 'develop' }`. The user
            // confirmed develop pushes should only touch the dev cluster —
            // this stage (staging) now only runs on pushes to
            // '<STAGING_BRANCH>' (placeholder: 'main', NOT yet confirmed by
            // name — see the header note at the top of this file). Note
            // this doesn't fix the build #41 db-seed BackoffLimitExceeded
            // failure; it just stops it from being triggered by develop.
            when {
                beforeAgent true
                branch 'staging' // ASSUMPTION — confirm the real staging branch name
            }
            agent { label 'vpn-agent2' }
            steps {
                sh 'rm -rf ci scripts .central-charts'
                unstash 'deploy'
                withCredentials([file(credentialsId: 'staging-rke2-kubeconfig', variable: 'KUBECONFIG')]) {
                    sh """
                        set -eu
                        python3 ci/deploy/oan_chart.py require-deployable registry staging
                        WORK=\$(mktemp -d)
                        trap 'rm -rf "\$WORK"' EXIT
                        CHART=\$(python3 ci/deploy/oan_chart.py fetch registry "\$WORK/chart")
                        umask 077

                        # Dry-run + diff, kept even without a human gate so there's an
                        # audit trail to look at if a deploy ever needs investigating.
                        #
                        # NOTE: `helm template` has NO --reuse-values flag -- that flag
                        # only exists on `helm upgrade`/`helm install`, which manage a
                        # stored release to reuse values from. Fixed by explicitly
                        # feeding the just-captured live values back in as a -f file
                        # instead -- this is the actual on-disk equivalent of what
                        # --reuse-values does internally on a real upgrade.
                        helm get values \${HELM_RELEASE} -n \${HELM_NAMESPACE} -a -o yaml > live-values-before-\${BUILD_NUMBER}.yaml
                        helm template \${HELM_RELEASE} "\$CHART" -n \${HELM_NAMESPACE} \
                            -f live-values-before-\${BUILD_NUMBER}.yaml \
                            -f /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml \
                            > live-rendered-\${BUILD_NUMBER}.yaml
                        echo "Rendered \$(wc -l < live-rendered-\${BUILD_NUMBER}.yaml) lines against the currently-deployed values (image tags only overridden). Archived for audit."

                        # No --atomic/--wait: `live`'s pre-existing, not-fixable-from-here
                        # id-generator GitLab-403 pull failure means its ReplicaSet can
                        # never finish rolling out, so a --wait'd upgrade would block for
                        # the full --timeout and --atomic would auto-roll-back every run.
                        # --cleanup-on-fail is kept for some safety net on a genuine hook
                        # failure; the explicit `kubectl rollout status` checks below are
                        # the real verification step, scoped to the three deployments
                        # this pipeline actually cares about.
                        helm upgrade \${HELM_RELEASE} "\$CHART" -n \${HELM_NAMESPACE} \
                            --reuse-values -f /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml \
                            --cleanup-on-fail --timeout 10m

                        kubectl rollout status deployment/\${HELM_RELEASE}-staff-portal-api -n \${HELM_NAMESPACE} --timeout=180s
                        kubectl rollout status deployment/\${HELM_RELEASE}-staff-portal-ui -n \${HELM_NAMESPACE} --timeout=180s
                        kubectl rollout status deployment/\${HELM_RELEASE}-partner-api -n \${HELM_NAMESPACE} --timeout=180s
                    """
                    archiveArtifacts artifacts: 'live-values-before-*.yaml, live-rendered-*.yaml', allowEmptyArchive: true
                }
            }
        }
    }

    post {
        success {
            script {
                def committerEmail = sh(script: "git log -1 --pretty=format:'%ae'", returnStdout: true).trim()
                def committerName  = sh(script: "git log -1 --pretty=format:'%an'", returnStdout: true).trim()
                if (committerEmail.contains('noreply')) {
                    committerEmail = 'devops@yourorg.com'
                }
                mail(
                    to: committerEmail,
                    subject: "✅ Build SUCCESS: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                    body: """
Hi ${committerName},

Livestock Registry build/deploy succeeded!

Job:    ${env.JOB_NAME}
Branch: ${env.GIT_BRANCH}
Build:  #${env.BUILD_NUMBER}
URL:    ${env.BUILD_URL}

Regards,
Jenkins
"""
                )
            }
        }
        failure {
            script {
                def committerEmail = sh(script: "git log -1 --pretty=format:'%ae'", returnStdout: true).trim()
                def committerName  = sh(script: "git log -1 --pretty=format:'%an'", returnStdout: true).trim()
                if (committerEmail.contains('noreply')) {
                    committerEmail = 'simretyibeltal@gmail.com, Pavan.ns@gmail.com'
                }
                mail(
                    to: committerEmail,
                    subject: "❌ Build FAILED: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                    body: """
Hi ${committerName},

Livestock Registry build or deployment failed. A failed New Dev deploy is
rolled back to the last deployed revision (see deploy-failure-<build>.txt in the
build's artifacts); database changes made by hooks are not undone. Check
`helm history livestock-registry -n livestock` to see which revision is running.

Job:    ${env.JOB_NAME}
Branch: ${env.GIT_BRANCH}
Build:  #${env.BUILD_NUMBER}
URL:    ${env.BUILD_URL}

Regards,
Jenkins
"""
                )
            }
        }
        always {
            sh 'docker image prune -f || true'
            sh 'rm -rf "${WORKSPACE}/.docker-${BUILD_TAG}"'
        }
    }
}

}
return this
