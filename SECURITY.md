# Security policy

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Use the private vulnerability reporting of the repository (the
"Security" tab, "Report a vulnerability"). Include the commit, the file or script involved and the steps that show the
problem. Do not attach real secrets or tokens.

This repository has no bug bounty.

## What counts

- A script, target or compose file that publishes a port beyond the loopback interface, or exposes a secret in a
  process argument, a container environment visible to other services, a log or a tracked file.
- A generated secret, key or certificate that is predictable, created with weak permissions, or committed by the
  default flow (`.secrets/`, `certs/` and `out/` must stay git-ignored).
- A script that deletes, resets or changes shared data (accounts, volumes, OpenBao) without the explicit confirmation
  the README describes.
- A configuration of the realm, Redis ACL, OpenBao policy or mTLS setup that gives a component more access than its
  documented role.

## Not in scope

This is a development stack. The generated test credentials and the fixed test users are not production material, and
using the stack as a production deployment is a configuration error. Vulnerabilities of the third-party images belong
to their projects. Findings about the gateway, broker, portal or servers belong to their own repositories.

## Supported versions

The `main` branch.
