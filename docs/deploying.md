# Deploying a release through ARX

A release published with `make release` ([releasing.md](releasing.md)) is
deployed through ARX, which authorizes the deployment and hands it to a runner.
This guide covers that path as this template provides it: signing in to ARX
from this repository and keeping the session, reading a release back from its
package in the form ARX drafts a release from, previewing the DeploymentSpec a
release would be deployed with, bringing a runner into service, deploying a
release, and changing a deployment over to another release. `make start` still
runs a strategy from its source directory on this machine, without ARX.

Every call these commands make to ARX is one ARX documents in its public API
guides (*Sign-In & Sessions*, *API Conventions*, *Release & Deployment API*,
*Runner Administration API*).
They use your own ARX account: ARX has no machine credentials, so a command
acts as you, with your roles, and ARX records it against you.

## Signing in

    make arx-login ARX_URL=https://arx.example.com

`ARX_URL` is your organisation's ARX API address. It must be https; plain http
is accepted only to this machine (`localhost`, `127.0.0.1` or `::1`), for an ARX
running locally.

Signing in takes two steps, as in the ARX console:

1. your email (`ARX_EMAIL=`, or asked) and your password;
2. a code from your authenticator, or one of your backup codes.

The password and the code are read without being shown on the screen, are sent
only to ARX, and are not kept. A wrong code is asked for again, up to three
times.

If you belong to several organisations, the session acts in your default one;
`ARX_TENANT=<organisation id>` chooses another. `make arx-status` shows which
one it is.

### What is kept, and where

The session is kept in

    ${XDG_CONFIG_HOME:-~/.config}/custos-strategy/arx/hosts.json

with one entry per ARX address. The directory is created readable by you only
(mode 0700) and the file likewise (0600). If either can be opened by other
users, every command refuses to use it and says which `chmod` puts it right.

An entry holds the access token, which lasts 15 minutes, and the refresh token,
which lasts seven days from the last sign-in or refresh. When the access token
has run out, the next command refreshes it. ARX rotates the refresh token on
every refresh and voids the old one, so the file is changed under a lock: two
commands started at the same moment refresh once between them. If ARX no longer
accepts the session, because seven days passed without a refresh or you signed
out elsewhere, the entry is removed and the command asks you to sign in again.

### One code, one use

ARX accepts each 30-second authenticator code once. A command that needs a code
right after you signed in, in the same 30 seconds, waits for your authenticator
to show the next one and says so. Signing in twice within 30 seconds waits the
same way.

A kept session never stands in for a code. Actions ARX asks a fresh code for,
such as the step that starts a deployment, still ask for one each time.

### Checking and ending the session

    make arx-status [ARX_URL=https://arx.example.com]

shows the ARX, your email, the organisation, your roles there and when the
session ends. It refreshes the session if it has to and asks ARX whether it
still holds. With one session on this machine, `ARX_URL` can be left out.

    make arx-logout [ARX_URL=https://arx.example.com]

ends the session on ARX, so neither of its tokens works again, and removes it
from this machine.

## Reading a release back

    make arx-evidence STRATEGY=trend/my_idea [VERSION=0.2.0]

ARX drafts a release from the exact bytes the publisher pushed, not from
anything rebuilt here. This command reads those bytes back from your package by
digest and checks them against the receipt `make release` kept in
`.releases/<category>/<name>/<version>/`. `VERSION` defaults to the strategy's
`[project] version`. It sends nothing to ARX and changes nothing.

What it checks, and refuses the release on if any fails:

- **The receipt agrees with itself.** Its two descriptor lists hash to the
  digests it states, its artifact reference hashes to its digest and names the
  same release manifest, package and layers, the layers are in the publisher's
  order with the publisher's media types, and the publisher recorded that it
  read the release back and bound the attestation to it.
- **The manifests are the receipt's.** The release manifest and the attestation
  manifest each have the digest and size the receipt names, and list exactly
  the receipt's layers, in order, with their roles and names.
- **The attestation is for this release.** The attestation manifest's subject
  is the release manifest, by digest, size and media type.
- **Every layer is the receipt's.** Each layer of both manifests, the strategy
  package included, has the digest and size the receipt names.
- **The attestation names this release's statement and bundle**, by their
  SHA-256.

It then shows the release's trading scope (connector, pairs and leverage): a
deployment of the release must trade exactly that.

### What ARX is given

From the checked layers the tool builds what ARX's *Release & Deployment API*
asks for when a release is drafted and published: `artifact_evidence`, whose
fields are the strategy manifest, the artifact reference, the release BOM, the
release statement and the attestation reference, each as its exact bytes, its
parsed JSON where ARX asks for it and its SHA-256 in lower-case hex; and
`attestation_bundle_base64`, the attestation bundle in standard base64. The
digests are computed over the bytes as published; nothing is re-serialised.

### The GitHub login it reads with

The package is private if your repository is, so reading it needs your GitHub
login: the token `gh auth token` prints, with the `read:packages` scope. If the
token lacks it, the command stops before reading and says:

    gh auth refresh -s read:packages

The token is exchanged with GHCR for one that may only pull from this release's
package. Neither token is written to disk, shown, or passed on a command line.

## Previewing a deployment

    make deploy-preview STRATEGY=trend/my_idea MODE=sandbox \
        RUNNER=<runner id> PRODUCT=<product id> [VERSION=0.2.0]

In ARX, creating a DeploymentSpec starts its first instance, so the
authenticator code entered for it is the confirmation of exactly what will
trade. This command builds that spec here, in full, and shows what it says. It
reads the release back from its package as `make arx-evidence` does, and sends
nothing to ARX.

The spec is put together from three places:

- **The release.** Its trading scope -- connector, pairs and leverage -- comes
  from the release's own strategy manifest and from nowhere else. ARX refuses a
  deployment that trades anything other than what the release was validated
  for, so the command refuses it first: if `config.yaml` names another
  connector, other pairs or another leverage, it says which and stops.
- **`deploy.yaml`**, next to the strategy's `config.yaml`. `make new-strategy`
  writes one; `examples/trend/sma_cross/deploy.yaml` shows every field with a
  comment. It holds the risk limits, the scheduling policy, the venue source
  policy, the runner contract requirements, the `strategy_config` overrides and
  the engine's log level, and one section per mode with the engine binding, the
  credential scope in the runner's vault, and the sandbox starting balances or
  the shutdown policy. A missing field, an unknown one, or a mode without its
  section is refused.
- **The command line**: the mode, the runner the first instance starts on, and
  the product the deployment trades for. The preview neither creates the
  product nor looks it up; `make deploy` checks it.

The release's id in ARX is not chosen by hand: it is derived from the release
manifest's digest, so the preview shows the id `make deploy` drafts the release
under and sends (see [Deploying](#deploying)).

Decimals are written as strings in quotes (`"0.25"`). ARX accepts only whole
numbers in JSON and refuses a number with a fraction, so a YAML value such as
`0.25` without quotes is refused here with the field's name.

The three policy digests -- of the risk policy, the venue source policy and the
scheduling policy -- are computed as ARX's guide specifies: keys sorted at every
depth, arrays kept in order, compact UTF-8 with non-ASCII characters as they
are, then SHA-256 in lower-case hex. The risk policy's id is derived from its
digest unless `deploy.yaml` names one, so the same limits keep the same id.

What is shown: the release (strategy, version, release manifest digest, release
id, and the repository and commit it was published from), the mode, the runner,
the product, the connector, pairs and leverage, the venue source policy, the
credential scope, the `strategy_config` overrides, the sandbox starting
balances or what a stopped testnet instance does with its positions, every risk
limit, the three policy digests, and the request digest: the SHA-256 of the
request body in the same canonical form, without its idempotency key and code.
Everything shown is read from the body that would be sent; nothing is taken
from anywhere else.

On testnet, a spec without a shutdown policy is built, with a warning: a
stopped instance then keeps its open positions. Add `shutdown_policy` with
`position_policy: flatten` to the `testnet:` section to have them closed.

## Bringing a runner into service

A runner is brought into service by an ARX administrator, in the order ARX's
*Self-Hosted Execution* guide gives: an enrollment token is issued, the runner
enrolls with it on its own machine, it is named, and its message-transport
credential is authorised per mode. A runner safety policy then caps what it may
hold. Each of these commands acts with your own ARX session and asks for a
fresh authenticator code for its one write; a code ARX has just accepted is not
reused, and the command waits for the next one if it has to.

### Enrolling and naming it

    make enroll-runner RUNNER=<runner id> NAME="Trading box 1" SCOPE=<1-63> [PAPER_ONLY=0]

The command first lists the organisation's runners (no code needed) and then
does the one step that is due:

- **Not enrolled yet**: it issues an enrollment token for this runner id. The
  token is written to `.runner-enroll/<runner id>.token`, readable by you only
  (the directory is 0700, the file 0600, and git ignores both); it is never
  printed, logged or put on a command line. Copy the file to the runner's
  machine over a channel you trust and enroll the runner with it, as the
  runner's enrollment guide describes; the token expires 24 hours after it is
  issued. Then run the same command again to name the runner, and delete the
  file. `SCOPE` is required here, from 1 to 63, and is recorded with the
  runner's credential. The token is paper-only unless `PAPER_ONLY=0`: a
  paper-only runner can never obtain `live` material.
- **Enrolled but not named**: it gives the runner the display name `NAME`.
- **Already named `NAME`**: nothing is left to do, and no code is asked for.
  Named differently, it stops without asking for a code, since ARX would refuse.

### Authorising its transport credential

    make authorize-runner-transport RUNNER=<runner id> MODE=sandbox \
        [OPERATION=issue|rotate|revoke] [GENERATION=<n>]

The runner receives commands and sends reports over an authenticated message
transport, with one credential per mode, issued, rotated or revoked only
against an intent an administrator authorised. The command records that intent
and prints its id and when it expires; pass the id to the runner's transport
command on the runner's machine before then. `GENERATION` is the generation
active on the runner, needed for `rotate` and `revoke` and refused for `issue`.

### Capping what it may hold

    make runner-safety-policy ACTION=submit RUNNER=<id> MODE=sandbox FILE=policy.json
    make runner-safety-policy ACTION=approve RUNNER=<id> MODE=sandbox REQUEST=<request id> REASON="…"
    make runner-safety-policy ACTION=activate RUNNER=<id> MODE=sandbox REQUEST=<request id>

A runner safety policy caps the largest single order and the largest total the
runner may hold, in one settlement currency, for a fixed period. It is a
two-person control: an `ADMIN` or `OPERATOR` asks for it, a `FINANCE` holder
who is a different person approves it, and it takes effect when activated.

`submit` reads the request from `FILE`, a JSON object with
`settlement_currency`, `max_order_notional`, `max_total_notional`,
`effective_at`, `expires_at`, `capability` and `reason`, and optionally
`expected_current_revision`; `CURRENCY=`, `MAX_ORDER=`, `MAX_TOTAL=`,
`EFFECTIVE_AT=`, `EXPIRES_AT=`, `REASON=` and `REVISION=` give or override a
field on the command line. The two notional values are decimal strings; a
number is refused. `capability` is the capability version the runner
published: `capability_version_id`, `capability_version` and
`manifest_digest`.

`approve` reads the request first and stops, before asking for a code, if you
are the person who asked for it. `approve` and `activate` send the request's
current version, so a request that changed since it was read is refused rather
than approved blind.

## Deploying

    make deploy STRATEGY=trend/my_idea MODE=sandbox \
        RUNNER=<runner id> PRODUCT=<product id> [VERSION=0.2.0]

This takes a released version to a running instance on a runner. It builds the
spec exactly as `make deploy-preview` does -- the same function and the same
object -- and asks for one authenticator code, at the step that starts trading.

### Before the first deployment

- You are signed in (`make arx-login`) with a role that may draft and publish
  releases and create deployments: `ADMIN`, or `STRATEGIST` for drafting,
  publishing and deploying (`OPERATOR` may deploy a release already published).
- The runner is enrolled, named, has its transport credential for the mode,
  and a safety policy ([Bringing a runner into service](#bringing-a-runner-into-service)).
- The strategy's `deploy.yaml` is filled in, credential scope included.
- **The product exists.** This command does not create products yet. ARX makes a
  product for a strategy definition, and the definition is created by the first
  `make deploy` of the strategy. So the first run creates the definition and the
  release, then stops at the product and says which strategy to create one for.
  Create the product for that strategy and mode in the ARX console, with its
  capital, then run the same command again with `PRODUCT=<its id>`. A product
  serves every release of its strategy in its mode, so this is done once per
  strategy and mode.

### What it does, in order

1. **Reads the release back** from its package and checks it, as
   `make arx-evidence` does, and builds the spec from it, `config.yaml` and
   `deploy.yaml`. Anything that would be refused is refused here first.
2. **The strategy definition.** ARX's definition for the strategy is the one
   named by the release's strategy coordinate (for example `trend/my_idea`). It is
   looked up by that name and created only if ARX has none. No code is needed.
3. **The release.** Its id is derived from the release manifest digest (below).
   If ARX has it published, it is used as it is; if it is a draft, it is only
   published; if ARX does not have it, it is drafted under the strategy's next
   release number (one above the highest ARX lists) and published with its
   attestation bundle. Neither step needs a code: a release does nothing until a
   deployment names it.
4. **The product** named with `PRODUCT=` is read in this mode and must belong to
   this strategy. A product of the other mode, of another strategy, or one ARX
   does not have, is refused before any code is asked for. A product that is not
   active yet is warned about: until it holds capital and is activated, its
   value cannot be computed and risk checks cannot see the instance.
5. **The trading account.** Until money can move between trading accounts, a
   new release of a strategy must trade from the same account as the one before
   it in the same mode. The credential scope and engine binding are compared
   with the last deployment receipt of another release of the strategy in this
   mode on this machine, and a difference is refused before any code. ARX makes
   the same check; this one only saves a code.
6. **Whether the spec exists already** (below). If it does, no code is asked for.
7. **The effect point.** The full summary is shown, as `make deploy-preview`
   shows it, with the idempotency key, and one fresh authenticator code is asked
   for. Creating the spec starts its first instance on the runner, so that code
   confirms exactly what is shown. A code ARX does not accept writes nothing, and
   the next code your authenticator shows is asked for, up to three times.
8. **The first instance** is watched until ARX lists it, for up to three
   minutes. If it is not listed by then, the receipt says so; running the same
   command again goes on watching without asking for a code.
9. **The deployment receipt** is written (below), with what is left to do on the
   runner's machine.

### Running it again

Every step reads what ARX already has before it writes, and each write carries
an idempotency key derived from what it writes, so the command can be run
again at any point:

- after it stopped part-way -- a network failure, ARX unavailable, a product
  not made yet -- it carries on from there; nothing is made twice;
- after it finished, it asks for no code and sends nothing.

How the ids are derived, so the same input always gives the same id:

| Id | Derived from |
|---|---|
| The release's id in ARX (`strategy_release_id`) | UUID version 5 of the release manifest digest in the receipt (`sha256:…`), in the namespace `5b0e7c1d-3a9f-4d62-8e15-7f2c4a6b9d03` |
| The spec's `idempotency_key` | UUID version 5 of the request digest, in the namespace `3d8a6f12-7c4e-4b9a-a5d0-1e6f2b8c9d47` |
| The `Idempotency-Key` of the other writes | UUID version 5, in the same namespace, of what the write is about: the organisation and strategy name, the release id and release number, the release id and draft version, or the instance id and its version |

The request digest is the SHA-256 of the spec's body in canonical form without
its idempotency key and code, as the preview shows it. The same parameters
therefore always send the same key, and ARX answers the spec it already has.
Other parameters make another digest, another key and a new spec. For a
release already deployed on a runner, that is refused here rather than sent:
a runner runs one instance at a time, and the deployment already made is left
as it is. Stop it first with `make deploy-stop`.

If the answer to the spec's creation was lost, the next run finds the spec ARX
listed for this release and runner and takes it as created, without a code.

### The deployment receipt

`.deployments/<category>/<name>/<version>/<mode>-<runner>.json`, which git
ignores, records: the ARX address, the organisation and who deployed; the
strategy definition id; the release id, number and manifest digest; the
product id; the spec id, its digest as ARX computed it, its idempotency key and
the request digest; the credential scope and execution channel; the first
instance's id and state; and when it was created, updated and stopped.
`.progress.json` in the same directory records each step as it completes.

### On the runner's machine

The receipt and the command's last lines list what is left to do there:

1. Add the new instance (its id, the spec id and the spec digest are in the
   receipt) to the runner's capability bindings, and publish its capability
   again with `custos publish-capability`, as the runner's documentation
   describes.
2. **Then restart the runner.** It reads its capability receipt only when it
   starts; until it restarts, the new instance's commands wait for a binding.
   The `restart_required` in the publication's receipt refers to the runner.
3. A runner process runs one instance at a time. Before another release goes to
   the same runner, stop the one running there (next section).

## Changing release

A strategy runs one release at a time in each mode. Changing to a new release
is stop, then deploy:

    make deploy-stop STRATEGY=trend/my_idea MODE=sandbox [VERSION=0.1.0] [RUNNER=<runner id>]
    make deploy STRATEGY=trend/my_idea MODE=sandbox RUNNER=<runner id> PRODUCT=<product id>

`make deploy-stop` reads the instance the deployment receipt names, stops it
with one fresh authenticator code, waits until ARX lists it as stopped, and
records that in the receipt; an instance already stopped needs no code.
`VERSION` and `RUNNER` are needed only when this machine's receipts name more
than one deployment still running in the mode. Stopping needs the `ADMIN` or
`OPERATOR` role. What a stopped instance does with its open positions is the
shutdown policy in `deploy.yaml`.

The new release then deploys as above, for the same product, with no product
or capital steps. It must keep the same trading account: `credential_scope`
and `engine_binding_id` in `deploy.yaml` stay as they were.

If the new release is deployed while the old one still runs, ARX refuses it and
nothing is created; the command says so and gives the `make deploy-stop` to
run. Deploying the same release again with the same parameters after stopping
it does not start it again, since ARX answers the stopped spec; start a further
instance of it in the ARX console, or change `deploy.yaml` (its reason, for
one) to create a new spec.
