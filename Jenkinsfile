// Thin wrapper: all logic is in ci/run.sh. Image of ci/Dockerfile; no credentials are bound. The Docker socket is mounted
// only for the main branch, never for a change request. Agents: the pool labelled ci-style has no Docker socket and runs the
// static stage; the node labelled docker accepts jobs of main only and is a throw-away agent (reimaged or recreated after each
// build), because the socket gives its holder the whole daemon. Pull requests take this file from the trusted branch.
pipeline {
  agent none
  options { timeout(time: 30, unit: 'MINUTES') }
  environment { MCP_CI_IMAGE = 'mcp-ci:local' }
  stages {
    stage('static') {
      agent { docker { image 'mcp-ci:local'; label 'ci-style' } }
      steps { sh 'ci/run.sh lint mocks compose style' }
    }
    stage('docker-tests') {
      when { branch 'main' }
      agent { docker { image 'mcp-ci:local'; label 'docker'; args '-v /var/run/docker.sock:/var/run/docker.sock' } }
      environment { CI_REQUIRE_DOCKER = '1' }
      steps { sh 'ci/run.sh unit proxy lb start' }
    }
  }
}
