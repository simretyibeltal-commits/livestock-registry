pipeline {
    agent any

    parameters {
     
        booleanParam(name: 'DEPLOY_TO_LIVE', defaultValue: false,
            description: 'Also run the Deploy stage against the live namespace. Leave OFF until the first chart-switch cutover has been done deliberately.')
    }

    environment {
        AWS_ACCOUNT_ID = "${env.AWS_ACCOUNT_ID}"
        AWS_REGION     = "ap-south-1"
        ECR_REGISTRY   = "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
        ECR_PATH       = "openg2p/livestock-registry"

        RP_VERSION     = "0.0.0-develop.296"

        HELM_RELEASE   = "livestock-registry"
        HELM_NAMESPACE = "live"
        HELM_CHART_DIR = "helm/openg2p-livestock-registry"
    }

    stages {
        stage('Checkout') {
            steps { checkout scm }
        }

        stage('ECR Login') {
            steps {
                withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-ecr-creds']]) {
                    sh "aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR_REGISTRY}"
                }
            }
        }

        stage('Build & Push Images') {
          
            steps {
                script {
                    env.IMAGE_TAG = env.GIT_COMMIT.take(12)

                    def components = ['staff-api', 'partner-api', 'celery', 'db-seed', 'sanity-tests']

                    components.each { name ->
                        def image  = "${ECR_REGISTRY}/${ECR_PATH}/${name}:${env.IMAGE_TAG}"
                        def latest = "${ECR_REGISTRY}/${ECR_PATH}/${name}:develop"
                        sh """
                            docker build --build-arg RP_VERSION=${RP_VERSION} \
                                -f docker/${name}/Dockerfile -t ${image} -t ${latest} .
                            docker push ${image}
                            docker push ${latest}
                        """
                    }
                }
            }
        }

        stage('Stash chart') {
            
            steps {
                stash name: 'livestock-chart', includes: "${HELM_CHART_DIR}/**"
            }
        }

        stage('Deploy to Staging (live namespace)') {
            when {
                allOf {
                    branch 'develop'
                    expression { return params.DEPLOY_TO_LIVE }
                }
            }
           
            agent { label 'vpn-agent2' }
            steps {
                unstash 'livestock-chart'
                withCredentials([file(credentialsId: 'staging-rke2-kubeconfig', variable: 'KUBECONFIG')]) {
                    sh """
                        helm dependency build ${HELM_CHART_DIR}

                        cat > /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml <<EOF
registry:
  staffApi:
    image:
      repository: ${ECR_REGISTRY}/${ECR_PATH}/staff-api
      tag: "${env.IMAGE_TAG}"
  partnerApi:
    image:
      repository: ${ECR_REGISTRY}/${ECR_PATH}/partner-api
      tag: "${env.IMAGE_TAG}"
  celeryWorker:
    image:
      repository: ${ECR_REGISTRY}/${ECR_PATH}/celery
      tag: "${env.IMAGE_TAG}"
  celeryBeat:
    image:
      repository: ${ECR_REGISTRY}/${ECR_PATH}/celery
      tag: "${env.IMAGE_TAG}"
  dbSeed:
    image:
      repository: ${ECR_REGISTRY}/${ECR_PATH}/db-seed
      tag: "${env.IMAGE_TAG}"
  sanity:
    image:
      repository: ${ECR_REGISTRY}/${ECR_PATH}/sanity-tests
      tag: "${env.IMAGE_TAG}"
EOF

                        # Dry-run render + diff BEFORE the real upgrade/install.
                        
                        helm get values ${HELM_RELEASE} -n ${HELM_NAMESPACE} -a -o yaml > /tmp/live-values-current-\${BUILD_NUMBER}.yaml || true
                        helm template ${HELM_RELEASE} ${HELM_CHART_DIR} -n ${HELM_NAMESPACE} -f /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml > /tmp/live-new-\${BUILD_NUMBER}.yaml
                        echo "Rendered \$(wc -l < /tmp/live-new-\${BUILD_NUMBER}.yaml) lines from this repo's own chart -- review against /tmp/live-values-current-\${BUILD_NUMBER}.yaml before trusting an automatic first run."

                        helm upgrade --install ${HELM_RELEASE} ${HELM_CHART_DIR} -n ${HELM_NAMESPACE} \
                            -f /tmp/values-live-cicd-\${BUILD_NUMBER}.yaml --timeout 20m
                        kubectl rollout status deployment/livestock-registry-staff-portal-api -n ${HELM_NAMESPACE} --timeout=180s
                        kubectl rollout status deployment/livestock-registry-partner-api -n ${HELM_NAMESPACE} --timeout=180s
                    """
                }
            }
        }
    }

    post {
        always {
            sh 'docker image prune -f || true'
            sh "docker logout ${ECR_REGISTRY} || true"
        }
    }
}