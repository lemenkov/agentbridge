<!--
SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Getting a GitHub token for a private repository

`clone_repo` and `fetch_repo` can work on private repositories, but only with
a forge token registered for the tenant. This is how to get one and register
it.

**Registering it is the operator's job at a root shell, and that is
deliberate.** There is no MCP tool for it, because a token passed as a tool
argument becomes a line in the conversation that called it — and a credential
that has appeared in a transcript is disclosed, so it would have to be
rotated immediately (`SECURITY.md`, L7). The same reasoning rules out the
command line: argv is world-readable in `ps` while the process runs, and a
value typed as an argument lands in the shell's history. The token therefore
arrives on **stdin**, unechoed.

So: do not paste a token into a chat with an agent, including Claude. Nobody
needs to see it but you and the service.

## 1. Create the token at GitHub

A **fine-grained** token, not a classic one. A classic token cannot be scoped
to a single repository, which is the whole point here.

1. Go to <https://github.com/settings/personal-access-tokens/new>. (The long
   way round: profile picture → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new
   token**.)
2. **Token name** — name it after the job, not after you: `agentbridge
   read <repo>`. You will be reading this list in six months wondering what
   each one was for.
3. **Expiration** — GitHub requires one. 90 days is a reasonable default.
   Shorter is safer and more chores; see *When it expires* below for why this
   is temporary anyway.
4. **Resource owner** — your own account for a personal repository. If you
   pick an organisation that requires approval, the token stays *pending* and
   can read only public repositories until an org admin approves it, which
   looks exactly like a broken token if you do not know to expect it.
5. **Repository access** — **three** options, and GitHub preselects the one
   that cannot work:
   - **Public repositories** — the default, and **useless here**: it grants
     read-only access to public repositories and nothing else. Worse, while
     it is selected GitHub offers *no repository permissions at all*, so the
     form looks complete. A token left on this setting passes every test
     against a public repository and fails on the first private one. See
     *The half-working token*.
   - **Only select repositories** → one repository, or a few. Narrowest
     blast radius, and the right default for a token going onto a host that
     runs unattended agents.
   - **All repositories** → every repository the resource owner has, now and
     in future. One token to manage instead of one per repository, at the
     cost of a wider blast radius — and note "in future": a repository you
     create next month is covered without you deciding anything.

   Pick deliberately; the last two are both supported end to end, and the
   `--match` argument in step 2 has to agree with what you picked.
6. **Permissions** → **Repository permissions** → **Contents: Read-only**.
   That is the only one needed for clone and fetch. GitHub adds
   **Metadata: Read-only** automatically and will not let you remove it; that
   is expected.

   **This is a second, separate step, and it is the one people miss.** The
   permission list only appears once *Repository access* is off *Public
   repositories*, and everything in it then starts at **No access** — so
   moving a token to *All repositories* grants reach without granting the
   right to read code. Both settings have to be made, and saved. A token with
   access but no `Contents` fails exactly like a token with no access:
   `403 remote: Write access to repository not granted`.
7. **Generate token**, then copy it. GitHub shows it exactly once. It starts
   with `github_pat_`.

Two GitHub behaviours worth knowing rather than discovering:

- **Every fine-grained token can read all public repositories**, whatever you
  selected. That is GitHub's design, not ours. "Only select repositories"
  constrains *private* access.
- An account is limited to **50** fine-grained tokens.

## 2. Register it on the host

As root, on the host running the service:

```bash
cd /opt/mcp-servers/agentbridge

# a token scoped to one repository
sudo venv/bin/agentbridge-admin git-token claude \
    --host github.com --match lemenkov/myrepo

# a token scoped to every repository you own
sudo venv/bin/agentbridge-admin git-token claude \
    --host github.com --match 'lemenkov/*'
```

**Quote the `owner/*` form.** Unquoted, the shell can expand the `*` before
the command ever sees it, and the credential is then registered against
something you did not type. This is the same class of mistake as an unquoted
`safe.directory=*`, which bit this project once already.

Note there is no `add` in that command — the subcommands are `git-token`,
`git-token-list` and `git-token-remove`.

It prints `Paste the token, then press Enter (it is not echoed back):` and
reads one line from the terminal without echoing it. Paste, press Enter, and
it answers:

```
registered <id>: github fine-grained for github.com/lemenkov/myrepo (read)
```

Arguments worth getting right:

| Argument | What to pass |
|---|---|
| `claude` | the **tenant** the credential belongs to, not your GitHub login |
| `--host` | `github.com` (also allowed: `gitlab.com`, `codeberg.org`) |
| `--match` | `owner/name` for one repository, or `'owner/*'` (quoted) for every repository of that owner. The wildcard prints a note — it is a confirmation that you meant it, not a warning that something is wrong. See *Matching several repositories* |
| `--scope` | `read` by default, which is what `clone_repo` and `fetch_repo` use. `write` is accepted but no tool reaches it yet |

The token's prefix is checked at registration (`github_pat_`, `ghp_`, `ghs_`,
`glpat-`, …) so a typo or a pasted password fails here, with a clear message,
rather than as a baffling 401 an hour later. The prefix says nothing about
permissions — only GitHub knows those.

It can also be piped, for a script: `printf %s "$TOKEN" | sudo ...`. Prefer
the interactive paste. A pipe means the token was in a variable or a file
first, and now you have a second copy to remember to destroy.

## Matching several repositories

`--match 'lemenkov/*'` offers the credential for every repository under that
owner. It is one entry to register, rotate and revoke instead of one per
repository, which is the honest argument for it: a per-repository token you
never get round to renewing is worse than a wildcard you actually maintain.

What it does **not** do:

- **It does not widen the token.** Our match decides which repositories this
  service will *offer* the credential for; GitHub decides what the credential
  can actually reach. If the token was created with *Only select
  repositories*, a wildcard match still fails on everything else — with a
  404, because GitHub does not confirm that a repository it cannot show you
  exists. The two have to agree, and GitHub is the one enforcing it.
- **It does not cover other owners.** `lemenkov/*` means repositories owned
  by `lemenkov`. An organisation's repository needs its own entry, with that
  organisation as the match and — at GitHub — as the token's resource owner.
  Each resource owner is a separate token.
- **It is not a pattern language.** The only wildcard is a whole `*` in place
  of the repository name. `lemen*/foo` and `lemenkov/pro*` are rejected at
  registration.

**Both can coexist, and the narrower one wins.** Register
`lemenkov/*` and `lemenkov/myrepo` and the exact match is selected for that
repository, the wildcard for everything else. That is how to give one
repository a different scope — a `write` credential, say — without widening
the rest.

## 3. Verify it

From the host, the registration is listed without any value:

```bash
sudo venv/bin/agentbridge-admin git-token-list claude
```

From the tenant's side — in a conversation with the service — two read-only
tools do the same without ever seeing a token:

- `list_git_credentials()` — what is registered: host, match, scope, kind.
- `test_git_credential("https://github.com/lemenkov/myrepo")` — runs
  `git ls-remote`, which authenticates and fetches no objects. Worth doing
  **before** `clone_repo` on a private repository, so a failure is a clear
  answer about the credential instead of a confusing clone error.

Then `clone_repo` works on that repository with no further arguments: the
credential is selected by host and repository name.

## What the service does with it

Briefly, because `README.md` and `SECURITY.md` carry the detail: the token
stays on the host. It is never written into the clone URL (where it would
persist in `.git/config` for every agent to read), never passed in argv, and
never mounted into a container. It reaches git through `GIT_CONFIG_*`
environment variables, per invocation, as an `Authorization: Basic` header
with the username `x-access-token`.

`fetch_repo` goes further: the authenticated fetch runs on the host into a
bare mirror the service owns, and a sandboxed container with **no network**
then fetches from that mirror into the workspace. So the credential never
touches a repository an agent can write — and since a git repository is a
program (`core.fsmonitor`, hooks, filter drivers), that repository's config is
only ever interpreted inside the sandbox.

## When it expires

It will, and nothing warns you first: a clone simply starts failing with a
401. Re-run the same `git-token` command with a fresh token — registering
over an existing match replaces it — and **revoke the old one at GitHub**.
Deleting a credential here (`git-token-remove`) only stops this service using
it; the token stays live at GitHub until revoked there.

This chore is the reason phase 2 exists. A **GitHub App** issues installation
tokens that expire after an hour and are minted on demand, so there is no
long-lived secret to rotate and no expiry to be surprised by. The fine-grained
PAT is the deliberate interim: it works today, with one short root command,
and nothing in the design has to change when the App replaces it.

## If it does not work

**Read the status code, not the message.** GitHub's git endpoint says
`remote: Write access to repository not granted` for causes that have nothing
to do with write access, and says it for `ls-remote`, `clone` and `fetch`
alike. The code is the part that carries information:

| Code | What it means |
|---|---|
| **401** | the credential was **not accepted** — invalid, expired, revoked, or not the token you think is registered |
| **403** | the credential **was accepted**, then refused — it is live, but lacks `Contents: Read` or the repository is outside its selection |
| **404** | the repository is **not visible** to this credential at all — wrong resource owner, or not in its selection |

A 403 therefore *proves the registered token is live*. If a token you have just
confirmed works returns 403 here, the question is not what it grants — it is
whether the host is holding it. See *Which token is the host holding?*

| Symptom | Likely cause |
|---|---|
| `unrecognised token prefix` | a password, a truncated paste, or a classic token typed where a fine-grained one was meant |
| `no credential for github.com/owner/name` | `--match` does not match the repository being cloned — check `git-token-list`. With a wildcard, check the *owner* matches: `lemenkov/*` does not cover an organisation's repository |
| registered against a shell-expanded name | the `owner/*` match was unquoted; `git-token-list` shows what was actually stored |
| 401 on a repository that used to work | the token expired, or was revoked at GitHub |
| 404 on a private repository | the token is valid but cannot see it: wrong resource owner, repository not selected under *Only select repositories*, or an org approval still pending |
| `remote: Write access to repository not granted` + **403** on a *private* repository | **missing Contents: Read-only**, or that repository is not in the token's selection — or the host is holding an **older token** than the one you are looking at. All three produce this exact message. See *The half-working token* and *Which token is the host holding?* |
| **403** on a repository whose token you have just verified by hand | the registered token is **not** the one you verified. Re-register it |
| `Resource not accessible` | missing **Contents: Read-only** (the REST API's wording for the same cause) |
| works for public, fails for private | **Contents: Read-only** was never granted, or the token is pending org approval — both permit public reads only. See *The half-working token* |

A 404 rather than a 403 is normal for a private repository a token cannot
see — GitHub does not confirm that it exists.

### The half-working token

**Every fine-grained token can read public repositories with no permissions
granted at all.** So a token created without **Contents: Read-only** is not
obviously broken — it is *selectively* broken, in the most confusing way
available:

| | Public repository | Private repository |
|---|---|---|
| `test_git_credential` | succeeds | **403, "Write access … not granted"** |
| `clone_repo` | succeeds, `authenticated: true` | 403 |

The public success is not meaningless, but it proves less than it appears to.
GitHub answers 401 for a public repository if the token is invalid or expired,
so a success does prove that **some live token is registered** and that the
`GIT_CONFIG_*` Authorization path reaches the forge.

It says nothing about private access, and — the part that is easy to miss —
nothing about *which* token is registered. **Any** live token passes, including
one you replaced weeks ago. Treating a public success as proof that the right
credential is in place cost this project four failed attempts and three wrong
diagnoses.

**So test against a private repository, or you have tested almost nothing.**
Observed for real on 2026-10-30: a token passed every public check, including
an authenticated clone, and failed on the first private repository it was
pointed at.

The fix is at GitHub, not here. Open the token at
<https://github.com/settings/personal-access-tokens> and check two things:

1. **Permissions → Repository permissions → Contents** is `Read-only`. If the
   Permissions step was skipped at creation, this is the cause.
2. **Repository access** covers the repository — either *All repositories*, or
   that repository listed under *Only select repositories*.

Editing an existing fine-grained token's **permissions** does not invalidate
it, so nothing needs re-registering afterwards: the same token simply starts
working.

**Regenerating** it is a different thing entirely — it mints a new value, and
the old one keeps working until you revoke it. If you regenerate, you *must*
re-register here, or the host goes on using the old token. That is the trap in
the next section.

## Which token is the host holding?

**Nothing on this side can tell you.** A registration records the token's
*kind*, inferred from its prefix, and nothing that distinguishes one
`github_pat_` from another. The id shown by `git-token-list` ends in random
hex generated at registration time, so a changed id means a registration was
written — not that the value differs.

So this failure mode is invisible: you edit a token at GitHub, confirm it
works, and the host keeps using an **older token you forgot you replaced**. If
that older token had repository access *Public repositories*, it is still live
and still refuses every private repository — with the same misleading
`Write access to repository not granted` as a missing permission. Observed for
real on 2026-10-30, over four attempts.

**Check the token itself before suspecting its grants.** On your own machine,
with the value you believe is registered:

```bash
read -rs -p 'paste token: ' TOK; echo
curl -s -D- -o /dev/null \
     -H "Authorization: Bearer $TOK" \
     https://api.github.com/repos/OWNER/REPO/contents/ \
  | grep -iE '^HTTP/|^x-accepted-github-permissions'
unset TOK
```

`read -rs` keeps the value off your screen and out of shell history. GitHub
names the permission it wants in the response header:

| Result | Meaning |
|---|---|
| `200` + `x-accepted-github-permissions: contents=read` | this token **can** read that repository. If the service still fails, the service is holding a different token — re-register it |
| `403` + the same header | the grant really is missing. Fix it at GitHub |
| `404` | this token cannot see the repository: wrong resource owner, or not in its selection |

Re-registering is one command and replaces the entry for that match in place:

```bash
sudo venv/bin/agentbridge-admin git-token claude \
    --host github.com --match 'lemenkov/*'
```

Two asides, both measured rather than assumed, in case they come up:

- **The basic-auth username does not matter.** `x-access-token`, your login,
  `git`, `oauth2` — all work. GitHub reads the password and ignores the
  username; `x-access-token` is simply its documented convention for App
  installation tokens. A username mismatch is never the cause.
- **`Authorization: Bearer` does not work for git.** It is correct for
  `api.github.com` and returns 401 on the git endpoint, so a Bearer-based
  hand-test can fail while git succeeds. Use Basic when testing git, Bearer
  when testing the REST API.
