# Security policy

## Scope

This repository is a **demo lab**, meant to run on a single laptop. It is not
hardened and is not meant for production or for networks you don't trust:

- Every credential in it is a documented demo default (for example
  `admin` / `admin` for Grafana and NetBox, and the SR Linux demo accounts).
  See [SECRETS.md](SECRETS.md).
- The scenario console and the Argo Workflows UI have no authentication.
- The web UIs are published on host ports 8080/8443 under
  `*.127-0-0-1.nip.io` hostnames that resolve to your own machine.

Reports that amount to "the demo uses default passwords" or "the console has
no login" are expected behavior. Reports we do want include:

- a real secret committed to the repository
- a way for a web page or another host to trigger actions in the lab that
  the documented setup should prevent (for example, bypassing the console's
  allowlist or cross-origin checks)
- the AI lanes gaining write access to the network, or escaping their
  read-only tool allowlists
- vulnerable pinned dependencies or images with a practical impact on the lab

## Reporting a vulnerability

Please report privately through GitHub's
[private vulnerability reporting](https://github.com/jp2195/network-automation-demo/security/advisories/new)
(the repository's **Security** tab → **Report a vulnerability**). Don't open
a public issue for security problems.

This is a personal project maintained on a best-effort basis; expect an
acknowledgment within a few days.
