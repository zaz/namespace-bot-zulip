# Namespace APIs & tools for managing the fleet bot

What Namespace provides for running this bot *properly* — scoped credentials,
roles, quotas, secrets, network policy, audit — and what we recommend using.
Written 2026-09-01 against the public docs; source pages linked per section.

**Context:** the bot lets fairly-trusted Zulip users drive a Claude Code agent
that can spawn ephemeral worker devboxes. The concern (Tobias): the credential
the agent holds is too coarse — today it's a full workspace token that could
create/delete anything. The asks: broad *spawning* stays allowed, blast radius
shrinks, and secrets never travel via Zulip.

## TL;DR recommendations

1. **Give the agent a scoped, short-lived token** minted with
   `nsc token create --grant …` instead of the workspace login token. Rotate on
   a timer; revoke instantly if abused. *(Available now, no plan upgrade.)*
2. **Keep the two-box split**: Zulip credentials on the small head box, the
   agent + its Namespace token on the workhorse. A wedged/compromised agent
   can't touch the Zulip key.
3. **Deliver secrets via Vault + egress `INJECT` rules or direct `scp` to the
   box — never through Zulip.** `INJECT` is the standout: the workload's HTTP
   requests get the secret header added *outside* the box, so the agent (and
   anyone prompting it) can never read the value.
4. **Put an egress policy on worker devboxes** (`ADVISORY` first, then `BLOCK`)
   so a prompt-injected worker can't exfiltrate to arbitrary hosts.
5. Longer term: ask Namespace about a **"Devbox User"-role service identity**
   (own-boxes-only) and per-profile resource caps (via support@namespace.so).

---

## 1. Scoped tokens — `nsc token create`

Docs: `reference/cli/token-create.md`

Namespace supports minting **named, scoped, revocable bearer tokens**:

```bash
nsc token create \
  --name zulip-fleet-agent \
  --expires_in 7d \
  --grant '{"resource_type":"devbox","actions":["create","use","expire"]}'
```

- `--grant` takes JSON grants: `resource_type`, optional `resource_id`,
  `actions`. Multiple grants allowed. Without `--grant` the token inherits the
  creator's full permissions — that's what we're moving away from.
- `--expires_in` defaults to 24h, max 365d. Short + auto-rotated beats long.
- `--user` binds the token to a member's permissions (so it can never exceed
  what that member could do).
- Consume via `NSC_TOKEN_FILE` (a file path, so the value never has to appear
  in a command line or env listing).
- Tokens are listed/revoked with `nsc token list` / `nsc token revoke`.

**For the bot:** a token granting devbox `create`/`use`/`expire` (no
workspace-admin actions) is exactly "broad spawn permission, small blast
radius". Whether Tobias's plan honors fine-grained grants needs a live test —
mint one, try to do something out of scope, confirm it 403s.

## 2. Roles — who can do what

Docs: `workspaces/access.md`

Fixed roles: **Owner, Admin, Editor, Devbox User, Accountant, Reader.**
The interesting one: **Devbox User** can *create, use and manage only their own
devboxes* — precisely the shape we want for an agent identity. Two catches:

- Roles attach to workspace *members* (humans/SSO), not to tokens; a service
  account with a role needs Tobias to add a member for the bot.
- Custom roles / full RBAC are business/enterprise-tier.

Practical path today: emulate Devbox User with a scoped token (§1); revisit a
real member identity if the org tier allows it.

## 3. Secrets — Vault + egress injection

Docs: `architecture/storage/secrets.md`, `security/egress-policy.md`

- **Vault** is Namespace's standard secrets store; secret *reads are
  audit-logged*. Secrets can be mounted into instances at create time instead
  of being pasted anywhere.
- **Egress `INJECT` rules** (see §5) go further: an egress policy rule can set
  an HTTP header on matching outbound requests **from a Vault secret**, with
  the value resolved *outside the workload* — "its value is never exposed to
  it." A worker can call an API that needs a key without the key ever existing
  on the box. This is the strongest possible answer to "secrets must not go
  via Zulip": they don't even go via the *agent*.

**Incident that motivates this:** a user pasted a live GitHub PAT into a Zulip
channel to give the bot repo access. The right flow instead: owner puts the
PAT in Vault (or scp's it to the box), the bot's docs tell users "never paste
secrets in chat; ask an admin to provision them."

## 4. Resource limits & quotas

Docs: `architecture/compute/resource-limits.md`

- Limits are a **workspace-wide concurrent vCPU/RAM pool** (e.g. 64 vCPU /
  128 GB on Team). Exceeding it fails instance creation with
  `ResourceLimitsError` / `INSTANCE_COUNT_LIMIT` — the bot should catch this
  and report "fleet is at capacity" instead of a stack trace.
- There are **no self-serve per-profile or per-token caps**; finer caps go
  through support@namespace.so.
- Consequence: a runaway agent can exhaust the *whole workspace's* pool even
  with a scoped token. Mitigate in the bot: cap workers per request and name
  them `w-*` so `devbox list` cleanup is trivial (already our convention).

## 5. Egress policies — network containment

Docs: `security/egress-policy.md` (nsc ≥ v0.0.554)

Workspace-level, reusable policies applied to devboxes/instances:

- Modes: `ADVISORY` (log-only — start here, build the allowlist from observed
  traffic) → `BLOCK` (deny anything not allowed).
- Rules in order: `ALLOW` (match_domains), `INJECT` (header from Vault secret;
  needs `deep_packet_inspection`), `PROXY` (route via another domain).
- Managed ruleset `devbox` covers the traffic devboxes themselves need.
- Apply via blueprint network-policy settings, or per-instance
  `network_policy.egress_policy_tag`; the TypeScript SDK exposes it directly
  at create time as `networkPolicy: { allowedDomains, advisory }`.
- Observability: workspace Egress Filtering dashboard + per-instance Egress
  tab; `nsc egress logs` from the CLI.

```bash
nsc egress policy create --spec_file egress-policy.json
```

**For the bot:** workers spawned for untrusted-ish tasks should get a policy
allowing the Eden gateway, GitHub, package registries — and nothing else.
That's real prompt-injection containment, not vibes.

## 6. TypeScript SDK — programmatic fleet control

Docs: `reference/typescript-sdk/devboxes.md`

`@namespacelabs/sdk/devbox` wraps the whole lifecycle, if we ever outgrow
shelling out to the `devbox` CLI:

```ts
const client = createDevboxClient();
const box = await client.devboxes.create({
  name: "w-render-1",
  imageName: "builtin:agents",
  size: "s",
  ephemeral: true,
  networkPolicy: { allowedDomains: ["api.edenai.run"], advisory: false },
});
```

- `create` / `get` / `list` / `iterate` / `start` / `stop` / `delete` /
  `update`; `ephemeral: {stoppedRetentionMs}` for retention;
  connection-backed command/file APIs auto-start stopped boxes.
- Python isn't offered — the bot is Python, so we'd either keep the CLI
  (fine; it's what works today) or add a thin Node sidecar. **Recommendation:
  stay on the CLI for the hackathon.**

## 7. Managed Agents (Claude-on-Namespace integration)

Docs: `integrations/claude` — this is the bot's `%` mode.

Anthropic runs the agent loop; each session gets a throwaway isolated devbox;
the agent holds **no tenant credential at all**. Maximum isolation, but no
fleet capability (the agent can't spawn siblings), and setup needs the
workspace owner to run `devbox claude managed-agents setup-environment` plus
an Anthropic-console webhook dance. Good for "run untrusted code safely",
wrong shape for "orchestrate N workers". We keep it as the `%` mode option.

## 8. Audit log

Docs: `security/auditlog.md`

Covers devbox create/update/expire and **SSH session initiation**, with actor
attribution. With a named token per bot (§1), every box the agent creates is
attributable to the bot rather than to whoever's login token it borrowed —
that alone is worth the switch.

## What we actually tested (2026-09-01, live against the workspace)

Findings from minting and using real scoped tokens:

- **Non-admins can mint user-scoped tokens** (`--user`); tenant-wide tokens
  need a workspace admin ("Only workspace administrators can create
  tenant-wide tokens"). `nsc token list`/`revoke` also need admin
  (`token/revokable:list|revoke`) — so ask Tobias to mint/manage the
  production token, or accept that user-minted ones can't be self-revoked
  (keep expiry short).
- **Wildcard actions are rejected** in revocable tokens
  (`requested permissions are not allowed in revokable tokens: [instance:*]`)
  — enumerate actions explicitly. Full vocabulary: docs/security/permissions
  (`devbox`: activate/create/expire/fetch/list/update; `instance`: create/
  destroy/dial_host/exec/get/list/refresh/release/resume/ssh/suspend/wait).
- **Control plane works under a scoped token**: `nsc create` (ephemeral
  instance), `nsc list`, `nsc destroy --force` all succeeded with a token
  granting only instance/devbox actions, and `nsc token list` was correctly
  denied. Use `NSC_TOKEN_FILE=<path>`; mind `-o json`/`--force` — several
  commands otherwise want a TTY.
- **The data plane rejects revocable tokens**: `nsc ssh` and
  `nsc instance upload` fail with `websocket: bad handshake` against
  `wss://gate.<site>.nscluster.cloud/<id>/22` while the same commands succeed
  with the full login credential, same instance, same moment. Injected SSH
  keys don't help — the websocket *transport* is what authenticates. **Ask
  Namespace support whether the ssh gate can accept revocable tokens.**
- The `devbox` CLI has no token-file support at all (browser `devbox login`
  only), so scoped tokens currently mean driving workers with `nsc`
  (instances), not `devbox`.

**Practical consequence for the bot today**: the scoped token cleanly covers
spawn/list/expire (the blast-radius concern), but executing commands on
workers still needs a full credential somewhere. Interim: keep the full
workload credential on the execution box for ssh, use the scoped token for
lifecycle operations, and revisit once support answers on the gate.

## Defense-in-depth summary

| Layer | Mechanism | Status |
|---|---|---|
| Identity | Named scoped token, short expiry, revocable | to implement (§1) |
| Privilege | devbox create/use/expire only | to implement (§1) |
| Blast-radius | head box (Zulip key) ≠ workhorse (ns token) | boxes ready, flip pending |
| Secrets | Vault + egress INJECT; never via Zulip | to adopt (§3) |
| Network | egress policy on workers, ADVISORY→BLOCK | to adopt (§5) |
| Quota | workspace pool + bot-side worker cap, `w-*` naming | naming done; cap in refactor |
| Audit | Namespace audit log + bot's own command log | audit log free with §1 |
| App-level | Zulip sender-ID allowlist | done |
