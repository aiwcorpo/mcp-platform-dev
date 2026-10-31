# Image of CI for this repository: the tools ci/run.sh needs. Build from the repository root:
#   docker build -f ci/Dockerfile -t mcp-ci:local .
# The base image is the one the mocks run on (same digest as docker-compose.yml). The Docker CLI and the compose plugin talk to
# the daemon of the host through its socket (steps proxy and lb); every other step needs no daemon.
FROM python:3.12-alpine@sha256:0687a6bc9716edc2a6ee0fbfb0f87e7ee358b262b67c9215de91bc9b2d38ba71
RUN apk add --no-cache bash git make jq curl openssl shellcheck docker-cli docker-cli-compose
WORKDIR /work
# No ENTRYPOINT: CI systems start their own shell in the image; run steps with ci/run.sh <steps>.
CMD ["bash"]
