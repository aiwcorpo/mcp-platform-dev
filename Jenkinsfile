// Thin wrapper: all logic is in ci/run.sh. Image of ci/Dockerfile; no credentials are bound. The Docker socket is mounted
// only for the main branch, never for a change request.
pipeline {
  agent none
  options { timeout(time: 30, unit: 'MINUTES') }
  environment { MCP_CI_IMAGE = 'mcp-ci:local' }
  stages {
    stage('static') {
      agent { docker { image 'mcp-ci:local' } }
      steps { sh 'ci/run.sh lint mocks compose style' }
    }
    stage('docker-tests') {
      when { branch 'main' }
      agent { docker { image 'mcp-ci:local'; args '-v /var/run/docker.sock:/var/run/docker.sock' } }
      environment { CI_REQUIRE_DOCKER = '1' }
      steps { sh 'ci/run.sh unit proxy lb' }
    }
  }
}
