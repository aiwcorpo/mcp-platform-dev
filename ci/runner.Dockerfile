# Local GitLab CI runner without a GitLab server: gitlab-ci-local reads .gitlab-ci.yml and runs the jobs on this machine. Nothing is
# registered anywhere and no token is needed (a decision for the CI setup). Built on the CI image, so jobs without an image: run in the
# shell of this container with every tool of ci/run.sh. Build (needs network for npm):
#   docker build -f ci/runner.Dockerfile -t mcp-ci-runner:local .
FROM mcp-ci:local
RUN apk add --no-cache nodejs npm rsync && npm install --global gitlab-ci-local@4.76.1 && npm cache clean --force
ENTRYPOINT ["gitlab-ci-local"]
