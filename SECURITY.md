<!--
SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Security considerations

This document states what `agentbridge` and the `mcp-agentbridge` service
defend against, what they deliberately do not, and the limitations an operator
should know before provisioning a second user. It is written to be read by
somebody deciding whether to trust the service, so the limitations are not
softened.

## What this is

A service that lets several people each drive a fleet of unattended `claude`
agents on one host. Every agent runs with `--dangerously-skip-permissions`
inside a rootless podman container, started by a systemd user unit owned by a
single service account. Agents read untrusted material by design — repositories
they clone, web pages, other people's issue trackers.

## Threat model

**Defended against:**

- One tenant reading, writing or disrupting another tenant's bus traffic,
  workspace, credentials or workers.
- A leaf container reaching the host's own services, the LAN, or another
  tenant's resources over the network.
- A prompt-injected orchestrator escalating to create or destroy tenants.
- A tenant's credential or password being disclosed by a tool result, a log
  line or an error message.
- A tenant passing a host path, image, flag or committer identity that changes
  what the host executes.

**Not defended against:**

- **The operator.** Whoever is root on the host can read every credential,
  every workspace and every tenant record. There is no mechanism here that
  withholds anything from root, and none is attempted.
- **Any local process on the host** — see limitation L1.
- **A human relaying a hostile instruction.** If the person at the terminal is
  persuaded to run an administrative tool, nothing downstream distinguishes
  that from a legitimate request.
- **The agent's own judgement.** An agent given a task will do it. The
  permission layer is the human and the orchestrator, not the agent.

## Trust boundaries

| Boundary | Crossed by | Enforced by |
|---|---|---|
| Internet → service | HTTPS, Basic auth | nginx, per-location `auth_basic_user_file` |
| nginx → service | `X-Forwarded-User` | **trusted unconditionally** (L1) |
| tenant → tenant | nothing | vhost, bus user permissions, per-user workspace, per-user podman secret |
| service → leaf | task text on stdin | podman, rootless, no `--network=host` |
| leaf → network | TCP | `table inet agentbridge`, filtered by `meta skuid` |
| user tools → admin tools | nothing | separate port and nginx location, plus the tenant's `admin` flag |

## What is enforced

**Identity.** Every tool call is scoped to the tenant named by
`X-Forwarded-User`, which nginx sets from the authenticated Basic-auth user and
overwrites if the client supplied one. No tool takes a user name as an
argument; a tenant cannot name another tenant.

**Two endpoints (invariant I-2).** The tools that create and delete tenants
live on a separate port and nginx location from the tools an orchestrator uses.
A prompt-injected orchestrator holds no handle to them — its connector does not
list them. Reaching that endpoint is only half the check: the service also
requires the caller's tenant record to say `"admin": true`, which nginx cannot
check because it only knows logins. `ADMIN_PORT=0` removes the endpoint
entirely on hosts that do not provision.

**Logins are scoped to this service.** `auth_basic_user_file` is set per
location, so a login this service issues grants this service only — never the
server-level password file that fronts every other MCP on the host.

**Leaf egress.** Leaves use rootless default networking, so they have no route
to host loopback services. A dedicated nftables table filters their traffic by
the service account's uid — the only workable discriminator, because with
rootless podman and pasta a leaf's outbound connection is a host socket owned
by that uid. The table is loaded by its own unit and never through
`/etc/sysconfig/nftables.conf`, whose `flush ruleset` would destroy firewalld's
rules. The host's own addresses are rejected via `fib daddr type local`, and
loopback is matched by address rather than by output interface, because traffic
to the host's own LAN address also egresses via `lo`.

**No host-controlled input from tenants.** Roles, images, mounts and models are
server-side allowlists. Committer names and emails are validated before being
written, because a newline in either turns a git config file into arbitrary
configuration — `core.sshCommand`, filter drivers or `include.path` — and
therefore into code execution at the next git invocation.

**Secret material never leaves the host.** A provisioned password is returned
once, as bcrypt is all that is stored. Credential tools take a value and return
a kind; they never echo the value, a hash, or its length. `list_users` returns
credential kinds, never values. Logs record user, tool and outcome only.

## Known limitations

### L1. `X-Forwarded-User` is trusted unconditionally

The service binds loopback and believes the identity header on every request.
**Any process on the host that can open a TCP connection to `127.0.0.1:8817`
can set that header to any tenant name and act as that tenant.** No password,
credential or prior access is needed. On port 8818 the same forgery yields an
administrator, and so the ability to create tenants and to destroy them with
`purge=True`.

This is accepted, for now, for two reasons: the host has a single interactive
user who is also the operator and already has root, and the same header
convention is used by every other MCP server behind this nginx — deviating for
one service would make the fleet inconsistent without closing the hole
elsewhere.

It would be closed by having the service listen on a unix socket and identify
local callers with `SO_PEERCRED` (nginx would continue to reach it over the
socket, and the TCP port could go away), or by requiring a shared secret header
that only nginx knows. Either is a fleet-wide change, not a change to this
service alone.

**An operator adding a second tenant to a host with other local users should
treat this as disqualifying** until it is fixed.

### L2. The operator is root

Credentials are stored in podman secrets using the `file` driver, which keeps
them unencrypted on disk under the service account; bus passwords are stored in
the tenant record and in an AMQP URL file. All are mode 0600, which protects
them from other tenants, not from root. A tenant should therefore register a
dedicated, spend-capped Anthropic credential rather than their main one, and
watch its usage themselves.

### L3. Agents run without permission prompts

Every agent runs with `--dangerously-skip-permissions`, intentionally: an
unattended worker must not block on a prompt. The container is the sandbox.
Consequently the blast radius of a hostile task or a poisoned repository is
whatever that container can reach — its own tenant's workspace mount and the
network the nftables table allows. Keep mounts minimal and read-only wherever
the task does not need to write.

### L4. Leaves are isolated from each other, but not by the network rule

The egress filter matches the service account's uid, which every leaf shares,
so all leaves are subject to one egress policy and the network layer cannot
distinguish one tenant's leaf from another's. Separation between tenants rests
on distinct containers, mounts, credentials and bus vhosts, not on the firewall.

### L5. Rate limits are in-process

The per-user, per-action limits are held in memory and reset when the service
restarts. They exist to blunt a runaway or injected loop, not to withstand a
determined attacker who can restart the service — and anyone who can restart it
is root.

### L6. Prompt injection is mitigated, not solved

The endpoint split and the `admin` flag remove the most damaging tool from an
injected orchestrator's reach. They do nothing about an injected instruction
that stays within the tenant's own permitted actions — spawning workers,
writing to its own workspace, spending its own credential. Treat an agent's
output as data, never as instructions, and be especially suspicious of a
request to call an administrative tool that arrived in agent output, a
repository or a web page.

## Operator responsibilities

- Keep RabbitMQ's management API and AMQP port bound to loopback.
- Keep `/etc/sysconfig/mcp-agentbridge` mode 0600 — it holds the broker
  administrator's password.
- Ensure nginx can both traverse to and read this service's password file.
  Three things are needed, and any one of them being wrong gives the same
  confusing symptom — a 500 for requests that *carry* credentials while
  unauthenticated ones still get a clean 401:
  - `0711` on `/var/lib/mcp-agentbridge`, so nginx can traverse it;
  - **`2750` on `/var/lib/mcp-agentbridge/htpasswd`** — setgid, so a file
    created there inherits nginx's group. The service account is not a member
    of that group and therefore cannot `chgrp`, so without setgid every
    service-side rewrite (`set_password`, `provision_user`) silently drops the
    group and breaks every login;
  - `0640 agentbridge:nginx` on the file itself.

  `deploy/setup-host.sh` sets all three, and `agentbridge-admin provision`
  repairs them. Verify with one authenticated request through nginx after any
  provisioning change: a loopback test cannot see this failure, because it
  never touches Basic auth.
- Register each tenant's credential out of band. It should never pass through a
  conversation.
- Grant `--admin` to as few tenants as possible, and remember that an admin can
  deprovision every tenant but themselves.

### L7. Credentials in a URL

The fleet page is behind HTTP Basic auth, which makes the
`user:password@host` URL form tempting. Do not use it. Such a URL is kept by
the browser in history, in `document.baseURI`, and in anything that logs or
displays a URL; `fetch()` refuses it outright, so parts of the page silently
stop working. The page strips the userinfo from every URL it builds and never
logs a whole URL, but it cannot unsee a password the browser was handed. Let
the browser prompt for the credential, or use a password manager.

A credential that has been in a URL should be treated as disclosed and
rotated: `agentbridge-admin provision <user> --reset-password`.

## Reporting

Report a vulnerability privately to the maintainer
(<lemenkov@gmail.com>) rather than in a public issue.
