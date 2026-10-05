# Deploying a release through ARX

A release published with `make release` ([releasing.md](releasing.md)) is
deployed through ARX, which authorizes the deployment and hands it to a runner.
This guide follows that path in the order you take it: signing in to ARX from
this repository, reading the release back from its package, bringing a runner
into service, previewing the DeploymentSpec, deploying, what is left to do on
the runner afterwards, and changing over to another release. `make start` still
runs a strategy from its source directory on this machine, without ARX.

Every call these commands make to ARX is one ARX documents in its public API
guides (*Sign-In & Sessions*, *API Conventions*, *Release & Deployment API*,
*Runner Administration API*).
They use your own ARX account: ARX has no machine credentials, so a command
acts as you, with your roles, and ARX records it against you.

## The whole path

Three places are involved: **this repository** on your machine, where the
`make` commands run; **the ARX console**, the web interface of your ARX; and
**the runner's machine**, where the runner process and its secrets live.

| # | Step | Who | Where |
|---|---|---|---|
| 1 | Sign in: `make arx-login` | you | this repository |
| 2 | Read the release back and check it: `make arx-evidence` | you | this repository |
| 3a | Issue the runner's enrollment token: `make enroll-runner` | `ADMIN` | this repository |
| 3b | Enroll the runner with the token: `arx-runner enroll` | the runner's operator | the runner's machine |
| 3c | Name the runner: `make enroll-runner` again | `ADMIN` | this repository |
| 3d | Authorise its transport credential for a mode: `make authorize-runner-transport` | `ADMIN` | this repository |
| 3e | Complete the transport credential: `arx-runner nats-transport enroll` | the runner's operator | the runner's machine |
| 3f | Store the exchange key: `arx-runner vault put` | the runner's operator | the runner's machine |
| 3g | Publish the runner's capability: `arx-runner publish-capability` | the runner's operator | the runner's machine |
| 3h | Ask for a runner safety policy: `make runner-safety-policy ACTION=submit` | `ADMIN` or `OPERATOR` | this repository |
| 3i | Approve it, then activate it: `ACTION=approve`, then `ACTION=activate` | a `FINANCE` holder who did not ask for it | this repository |
| 4 | Fill in `deploy.yaml` and preview the spec: `make deploy-preview` | you | this repository |
| 5a | First `make deploy` in a mode: creates the strategy definition, the release and, with one authenticator code, the strategy's product, then stops | `ADMIN` or `STRATEGIST` | this repository |
| 5b | Put capital into the product, approve it, activate the product | see [The product](#the-product) | the ARX console |
| 5c | `make deploy` again: one authenticator code creates the spec and starts the first instance | `ADMIN`, `STRATEGIST` or `OPERATOR` | this repository |
| 5d | Allocate the approved contribution to the first instance | `FINANCE` or `ADMIN` | the ARX console |
| 6 | Bind the new instance, publish the capability again, restart the runner, while `make deploy` waits for the runner (unless the runner is bound for you) | the runner's operator | the runner's machine |
| 6b | Only if `make deploy` ended with exit status 3 before the runner answered: `make deploy` again; no code, it waits until the runner says it started the instance | you | this repository |
| 7 | Change release later: `make deploy-stop`, then `make deploy` | `ADMIN` or `OPERATOR` to stop | this repository |
| 7b | After a start the runner refused, once that is put right: `make deploy-stop`, then `make deploy` with the same variables says the instance ended and starts the same spec as a new instance | `ADMIN` or `OPERATOR` | this repository |

Step 3 is done once per runner (3d, 3e, 3h and 3i once per mode it trades in),
steps 5b and 5d once per strategy and mode (5d again for every further
contribution), and steps 2 and 4 to 6 for each release.
Every write to ARX, from this repository or from the console, asks for a fresh
authenticator code from the person making it; the sections below say which
steps need none. The runner-side steps are done with the runner's own command
line, `arx-runner`, as the runner's documentation describes; this repository
does not run them for you.

## 1. Signing in

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
still holds. It then lists every deployment `make deploy` made from this
directory to that ARX and organisation whose receipt does not say it was
stopped, each read from ARX again, with what its runner said about it (see
[Whether the runner started it](#whether-the-runner-started-it)). A deployment
ARX will not show is listed as not read, and the others are still shown. With one session on this machine, `ARX_URL` can be left out; the
same holds for every command below.

    make arx-logout [ARX_URL=https://arx.example.com]

ends the session on ARX, so neither of its tokens works again, and removes it
from this machine.

## 2. Reading a release back

    make arx-evidence STRATEGY=trend/my_idea [VERSION=0.2.0]

ARX drafts a release from the exact bytes the publisher pushed, not from
anything rebuilt here. This command reads those bytes back from your package by
digest and checks them against the receipt `make release` kept in
`.releases/<category>/<name>/<version>/`. `VERSION` defaults to the strategy's
`[project] version`. It sends nothing to ARX and changes nothing.
`make deploy-preview` and `make deploy` read the release back in the same way
themselves; running this first tells you, before anything else, that the
release and your GitHub login are in order.

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

## 3. Bringing a runner into service

A deployment runs on a runner enrolled with ARX. The runner identity
`make setup-runner` creates for local runs is not one: ARX does not know it,
and it cannot take ARX deployments. A runner is brought into service in the
order ARX's *Self-Hosted Execution* guide gives, partly from this repository by
ARX administrators and partly on the runner's own machine by whoever operates
it. Each `make` command here acts with your own ARX session and asks for a
fresh authenticator code for its one write; a code ARX has just accepted is not
reused, and the command waits for the next one if it has to.

A runner is named by its id, a UUID you choose when it is first enrolled
(`uuidgen` prints one). Every command below, the runner's own enrollment
included, takes that same id.

### 3a–3c. Enrolling and naming it

    make enroll-runner RUNNER=<runner id> NAME="Trading box 1" SCOPE=<1-63> [PAPER_ONLY=0]

This needs the `ADMIN` role. The command first lists the organisation's runners
(no code needed) and then does the one step that is due:

- **Not enrolled yet**: it issues an enrollment token for this runner id. The
  token is written to `.runner-enroll/<runner id>.token`, readable by you only
  (the directory is 0700, the file 0600, and git ignores both); it is never
  printed, logged or put on a command line. `SCOPE` is required here, from 1
  to 63, and is recorded with the runner's credential. The token is paper-only
  unless `PAPER_ONLY=0`: a paper-only runner can never obtain `live` material.
  The token expires 24 hours after it is issued.
- **Enrolled but not named**: it gives the runner the display name `NAME`.
- **Already named `NAME`**: nothing is left to do, and no code is asked for.
  Named differently, it stops without asking for a code, since ARX would refuse.

So the command runs twice, with the runner's enrollment in between:

1. `make enroll-runner RUNNER=<runner id> NAME="Trading box 1" SCOPE=3` issues
   the token.
2. Copy the token file to the runner's machine over a channel you trust. There,
   the runner's operator enrolls the runner with `arx-runner enroll`, giving it
   the token file, the ARX address, the organisation id and the same runner id.
   The runner makes its own key pair, and only public material leaves the
   machine; the runner's enrollment guide has the details.
3. `make enroll-runner RUNNER=<runner id> NAME="Trading box 1"` names it. Then
   delete the token file, here and on the runner's machine.

### 3d–3e. Authorising its transport credential

    make authorize-runner-transport RUNNER=<runner id> MODE=sandbox \
        [OPERATION=issue|rotate|revoke] [GENERATION=<n>]

The runner receives commands and sends reports over an authenticated message
transport, with one credential per mode, issued, rotated or revoked only
against an intent an administrator authorised. This command, an `ADMIN`'s,
records that intent and prints its `authorization_intent_id` and when it
expires. Before then, the runner's operator passes the id to
`arx-runner nats-transport enroll` on the runner's machine, with the transport
settings your ARX administrator gives; the runner's guide to its first signed
sandbox run lists them. `GENERATION` is the generation active on the runner,
needed for `rotate` and `revoke` and refused for `issue`. Do this for each mode
the runner will trade in.

### 3f–3g. The runner's own side

Two more steps happen only on the runner's machine, with the runner's commands:

- **The exchange key.** The key a deployment trades with is stored in the
  runner's vault with `arx-runner vault put`, under a key id (a UUID) and a
  scope digest. Those two are the mode's `credential_scope` in the strategy's
  `deploy.yaml` (`scope_id` and `scope_digest`); the runner refuses to start a
  deployment whose scope does not match its vault entry. ARX's guides do not
  yet say where the scope digest and the `engine_binding_id` in `deploy.yaml`
  come from; ask your ARX administrator for them.
- **The capability.** The runner publishes what it can run with
  `arx-runner publish-capability`, which keeps its receipt in
  `~/.arx/runner-capability.json` on that machine. The receipt's
  `capability_version_id`, `capability_version` and `manifest_digest` are what
  the safety policy below is bound to.

### 3h–3i. Capping what it may hold

    make runner-safety-policy ACTION=submit RUNNER=<id> MODE=sandbox FILE=policy.json
    make runner-safety-policy ACTION=approve RUNNER=<id> MODE=sandbox REQUEST=<request id> REASON="…"
    make runner-safety-policy ACTION=activate RUNNER=<id> MODE=sandbox REQUEST=<request id>

A runner safety policy caps the largest single order and the largest total the
runner may hold, in one settlement currency, for a fixed period. It is a
two-person control: an `ADMIN` or `OPERATOR` asks for it with `submit`, and a
`FINANCE` holder who is a different person approves it with `approve` and puts
it into effect with `activate`. Each person runs the command from their own
copy of this repository, signed in as themselves.

`submit` reads the request from `FILE`, a JSON object with
`settlement_currency`, `max_order_notional`, `max_total_notional`,
`effective_at`, `expires_at`, `capability` and `reason`, and optionally
`expected_current_revision`; `CURRENCY=`, `MAX_ORDER=`, `MAX_TOTAL=`,
`EFFECTIVE_AT=`, `EXPIRES_AT=`, `REASON=` and `REVISION=` give or override a
field on the command line. The two notional values are decimal strings; a
number is refused. `capability` is the runner's published capability version,
copied from its capability receipt (3g). For example:

    {
      "settlement_currency": "USDT",
      "max_order_notional": "1000",
      "max_total_notional": "5000",
      "effective_at": "2026-01-01T00:00:00Z",
      "expires_at": "2026-04-01T00:00:00Z",
      "capability": {
        "capability_version_id": "<from the runner's capability receipt>",
        "capability_version": 1,
        "manifest_digest": "<from the runner's capability receipt>"
      },
      "reason": "Cap the sandbox runner for its first trials"
    }

`submit` prints the request id, which the approver needs. `approve` reads the
request first and stops, before asking for a code, if you are the person who
asked for it. `approve` and `activate` send the request's current version, so a
request that changed since it was read is refused rather than approved blind.

### After the runner moves to a new Custos release

A runner's capability names the runtime it runs: the image digest, the source
revision and the engine version. From Custos 0.7.0 on, a capability receipt
published by an earlier release no longer starts the runner, so after the
runner is upgraded its operator does this on the runner's machine:

1. Publish the capability again with `arx-runner publish-capability` (3g),
   then restart the runner. Do the same after changing the runner's image or
   engine.
2. A runner run from the container image is given the image's multi-platform
   index digest in `CUSTOS_RUNTIME_IMAGE_DIGEST` (`sha256:` and 64 lower-case
   hexadecimal digits): the part after `@` in the image reference, which for the
   image this repository pins is in `[runner].image` of `toolchain.lock.toml`.
   Without it the runner declares a development runtime. `make start` here does
   not need it: it runs the local lane, not the one that publishes a capability.
3. Publishing again gives the capability a new version, and the safety policy
   of 3h–3i names the capability version it was granted for. Whether it has to
   be asked for again is as ARX says when the runner is next used; follow its
   prompts.

Releases published after `[publisher]` in `toolchain.lock.toml` moves to a new
tag are signed under that tag ([releasing.md](releasing.md), Who trusts a
release): a runner, and the service that deploys to it, accept them once they
list the new tag, and releases published before keep the tag they were signed
under.

## 4. Previewing a deployment

    make deploy-preview STRATEGY=trend/my_idea MODE=sandbox \
        RUNNER=<runner id> [VERSION=0.2.0]

In ARX, creating a DeploymentSpec starts its first instance, so the
authenticator code entered for it is the confirmation of exactly what will
trade. This command builds that spec here, in full, and shows what it says. It
reads the release back from its package as `make arx-evidence` does, and looks
the strategy and its product up in ARX: it only reads, writes nothing and asks
for no code.

The product line shows the strategy's product in this mode and its state, or
the product `make deploy` would create, with the name and currency it would be
created with ([The product](#the-product)). Its id is known before it exists,
since it is derived (see [Running it again](#running-it-again)), so the request
digest is shown as well. Only when ARX does not have the strategy definition
yet is there no product id: the line says both will be created, and the spec
is marked as one that cannot be sent.

The spec is put together from three places:

- **The release.** Its trading scope -- connector, pairs and leverage -- comes
  from the release's own strategy manifest and from nowhere else. ARX refuses a
  deployment that trades anything other than what the release was validated
  for, so the command refuses it first: if `config.yaml` names another
  connector, other pairs or another leverage, it says which and stops.
- **`deploy.yaml`**, next to the strategy's `config.yaml`. `make new-strategy`
  writes one; for a strategy made before it did, the command says to copy
  `examples/trend/sma_cross/deploy.yaml`, which shows every field with a
  comment. It holds the reason recorded with the deployment, the risk limits,
  the scheduling policy, the venue source policy, the runner contract
  requirements, the `strategy_config` overrides, the engine settings and the
  engine's log level, and one section per mode with the engine binding, the
  credential scope in the runner's vault (3f), and the sandbox starting
  balances or the shutdown policy. A missing field, an unknown one, or a mode
  without its section is refused, and so is an engine binding or credential
  scope still left `null`.

  Two of these ARX requires in full, so they are refused here, before
  anything is sent, exactly as ARX would refuse them:

  - **The runner contract requirements** name all five sections -- `risk`,
    `settlement`, `reconciliation`, `health` and `deployment_lifecycle` --
    each report at `v1`; only `valuation_checkpoint` in `reconciliation` may
    be left out.
  - **The venue source policy** names at least one venue whose ledger is
    reconciled, each once, read from `venue_api` or `drop_copy`. The venue is
    the runner's name for the exchange the connector trades on: `BINANCE` for
    `binance` and `binance_perpetual`, `OKX` for `okx` and `okx_perpetual`,
    `SODEX_SPOT` for `sodex` and `SODEX_PERPS` for `sodex_perpetual`.

  `make new-strategy` writes both filled in for the strategy's connector. A
  `deploy.yaml` written before it did has only the `health` contract and an
  empty venue list; the preview then says which sections are missing and
  prints the lines to add.
- **The command line**: the mode and the runner the first instance starts on.
  The product is not chosen on the command line: it is the one ARX has for the
  strategy in that mode. `PRODUCT=<id>` may still name it, to make sure; any
  other product is refused.

The release's id in ARX is not chosen by hand: it is derived from your
organisation and the release manifest's digest, so the preview shows the id
`make deploy` drafts the release under and sends (see
[Running it again](#running-it-again)). The organisation is read from the
session `make arx-login` keeps, so the preview needs you signed in; with
sessions for several ARX addresses, `ARX_URL=` names the one.

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

## 5. Deploying

    make deploy STRATEGY=trend/my_idea MODE=sandbox \
        RUNNER=<runner id> [VERSION=0.2.0]

This takes a released version to a running instance on a runner. It builds the
spec exactly as `make deploy-preview` does -- the same function and the same
object -- and asks for one authenticator code, at the step that starts trading.

### Before the first deployment

- You are signed in (`make arx-login`) with a role that may draft and publish
  releases and create deployments: `ADMIN`, or `STRATEGIST` for drafting,
  publishing and deploying (`OPERATOR` may deploy a release already published).
- The runner is enrolled, named, has its transport credential for the mode, and
  an active safety policy ([step 3](#3-bringing-a-runner-into-service)).
- The strategy's `deploy.yaml` is filled in, credential scope and product name
  included, and `make deploy-preview` shows what you mean to trade.

### The product

A deployment trades for a **product**, the unit that holds capital and that
value and risk are computed over. ARX keeps one product per strategy and mode,
and every release of the strategy deploys to that same product; its value and
high-water mark carry over from one release to the next.

`make deploy` finds the strategy's product in ARX's product list for the mode.
What happens next depends on what it finds:

- **None yet**, as on a strategy's first deployment in a mode: it shows the
  product it will create -- the strategy, the mode, the product id, and the
  display name and currency from the `product:` section of `deploy.yaml` -- and
  creates it with one fresh authenticator code. A missing name or currency is
  refused before any code, with the field to fill in. The new product is a
  draft, so the command stops there: nothing is deployed and no deployment
  receipt is written.
- **Not active yet** (a draft): it stops the same way, without asking for a
  code.
- **Retired**: it is refused. ARX keeps one product per strategy and mode, so
  a strategy whose product was retired cannot be deployed in that mode again.
- **Active**: the deployment goes on, to that product.

A draft product becomes active in **the ARX console**, each step with a fresh
authenticator code from the person taking it:

1. an investor holding `CAPITAL` asks to put capital into the product;
2. an `ADMIN` or `FINANCE` holder who is not that investor approves it (ARX's
   *Capital & Settlement* guide describes both);
3. an `ADMIN`, `OPERATOR` or `STRATEGIST` activates the product. ARX activates
   a product only once it holds capital.

Then run `make deploy` again. It creates the spec and starts the first
instance, and one console step remains:

4. once `make deploy` lists the first instance, a `FINANCE` or `ADMIN` holder
   allocates the approved contribution to that instance, in the console's
   capital and allocation pages, with a reason and a fresh authenticator code.
   The allocation names the instance, so it cannot be made before the instance
   exists.

**Do not leave this step out.** An approved contribution that is not allocated
stays blocked, and while it is blocked ARX stops updating the risk figures of
the deployments of the product it was made to, in that mode. Deployments of
your other products carry on as before. The `make deploy` run that first
lists the instance names the product and the instance to allocate to, to be
done once the runner confirms the start.

Steps 1 to 3 are done once per strategy and mode; later releases deploy to the
same product with no product steps. Every further contribution to the product
is allocated in the same way once it is approved. ARX's guides do not yet say
whether capital allocated to an instance follows the strategy to the instance
of its next release, so after changing release, check the product's
allocations in the console.

Creating the product needs the `ADMIN`, `OPERATOR` or `STRATEGIST` role. ARX
records, as the product's **origin**, the version of the strategy that could
run in the mode when it was created; the origin is a record only and does not
decide what a deployment runs. If ARX refuses the creation, nothing is created
and the command says why: the strategy definition was not found in your
organisation, or the strategy has nothing that can run in the mode yet (no
published release). `make deploy` publishes the release before this step, so
both mean something changed in ARX meanwhile; running it again looks
everything up afresh.

`PRODUCT=<product id>` is accepted, to name the product explicitly. It must be
the strategy's product in this mode; another product, or one given while the
strategy has none, is refused before any code is asked for.

### What it does, in order

1. **Reads the release back** from its package and checks it, as
   `make arx-evidence` does, and builds the spec from it, `config.yaml` and
   `deploy.yaml`. Anything that would be refused is refused here first.
2. **The strategy definition.** ARX's definition for the strategy is named by
   the release's strategy coordinate without its version: a release receipt
   names the release
   `strategy://github.com/<owner>/<repo>/trend/my_idea@0.2.0`, and the
   definition is `strategy://github.com/<owner>/<repo>/trend/my_idea`. Every
   release of the strategy therefore goes under the same definition, and so to
   the same product in each mode. It is looked up by that name and created
   only if ARX has none. No code is needed.

   Earlier versions of this command named the definition with the version, one
   definition and one product per release. Such definitions are left as they
   are: nothing is moved or retired for you. `make deploy` and
   `make deploy-preview` list them, and once nothing runs under them their
   releases and products can be retired in the ARX console. A release ARX
   already holds under one of them stays there, since a release stays under the
   definition it was drafted under; `make deploy` says so and asks for a new
   version (`make release`), which goes under the definition without a version.
3. **The release.** Its id is derived from your organisation and the release
   manifest digest (see [Running it again](#running-it-again)). If ARX has it
   published, it is used as it is; if it is a draft, it is only published; if
   ARX does not have it, it is drafted under the strategy's next release number
   (one above the highest ARX lists) and published with its attestation
   bundle. Neither step needs a code: a release does nothing until a deployment
   names it.
4. **The product.** The strategy's product in this mode is found, or created
   with one code; the command goes on only if it is active, and a product whose
   state ARX does not give counts as not active
   ([The product](#the-product)).
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
   command again goes on watching without asking for a code. Once it is
   listed, the product is read again: the release it says runs in this mode
   must be the one just deployed. If it names another release, the command
   says so and ends with an error; if it names none yet, the receipt records
   that. Then the instance is read until its runner says whether it started
   it, for up to as long again
   ([Whether the runner started it](#whether-the-runner-started-it)).
9. **The deployment receipt** is written (below), with what is left to do on the
   runner's machine ([step 6](#6-on-the-runners-machine-after-a-deployment)),
   once the instance is listed and again once the runner has been read.
   The run that first lists the instance also names the product and the
   instance whose capital is to be allocated in the console once the runner
   confirms the start (step 4 of [The product](#the-product)).

### Whether the runner started it

ARX's state for an instance (`lifecycle_state`) is the state ARX wants it in,
not proof that it runs. What the runner itself reported is the instance's
`runner_observation`, which ARX keeps for the instance's current generation and
the command it was last given. `make deploy` reads it and ends accordingly:

| `runner_observation` | What `make deploy` does |
|---|---|
| `status: running_confirmed` | The deployment is done: it says when the runner confirmed it. Its next steps are only what can be done from then on: `make arx-status`, and `make deploy-stop` before another release of the strategy is deployed in this mode. The runner-side steps and the allocation, which the run that first listed the instance named, are not listed again; if that same run saw the start confirmed, it names the allocation too |
| `status: start_rejected` | Ends with an error at once, with the runner's `outcome` (`conflict`: something on the runner conflicts with the start, such as another instance already running on it; `retry_exhausted`: the runner tried and gave up), the runner's `reason_code` when ARX reports one, when it was observed and the event id to look for in the runner's log. A rejected start holds for this instance: put right what the runner refused, stop it with `make deploy-stop`, and run `make deploy` again with the same variables: it starts the same spec as a new instance ([below](#starting-the-same-spec-again)); `deploy.yaml` stays as it is |
| `status: awaiting_runner` | Read again every three seconds until the runner answers or the wait ends. The runner answers only once the instance is bound to it. If this run is the one that first listed the instance, it shows the runner-side steps ([step 6](#6-on-the-runners-machine-after-a-deployment)) once, as the wait begins, and goes on waiting while they are done: an answer within the wait ends the command as above, in the same run. If the runner has still not answered by the end of the wait, a run that first listed the instance ends with exit status 3 and lists the steps again; a later run ends with an error. Running the command again asks for no code and waits again |
| `null` | ARX does not want the instance running (it is paused or stopped, say), so no runner is asked to run it: an error, and the instance is to be looked at in the console |
| not there at all | ARX is older than this tool: it does not report what the runner said, so whether the instance started cannot be told. An error, never taken as a start; ask an ARX admin to upgrade ARX |

ARX does not let this repository read which instances a runner's capability
is bound to, nor does its `runner_observation` say whether the runner is
waiting for a binding, so "first listed by this run" stands in for "may not be
bound yet": that run names the runner-side steps, in case nothing binds the
instance for the runner. The receipt records which it was
(`first_listed_by_this_run`, `runner_check_basis`).

The wait lasts `TIMEOUT` seconds, 180 unless given, and the same again first
for the instance to be listed:

    make deploy STRATEGY=trend/my_idea MODE=sandbox RUNNER=<runner id> TIMEOUT=600

So a deployment is one run when the runner-side steps are done within the
wait, or when something binds new instances for the runner; otherwise the run
ends with exit status 3, and a second run, once the steps are done, confirms
that the runner started it. `make arx-status` shows what each runner said at any time,
apart from the state ARX wants the instance in, which says what ARX asks of the
runner and not that the instance runs: for example
`instance <id>: ARX wants it running; runner <id> refused the start at <time> (outcome conflict, event <id>)`.

The command's exit status says how it ended:

| Status | Meaning |
|---|---|
| 0 | Deployed and confirmed by the runner, or stopped at the product, which then needs capital and activating |
| 1 | It failed: the runner refused the start, did not answer within the wait, or ARX does not report or gives an unreadable observation; also any refusal or error before that, a product that says another release runs, and an instance ARX does not want running |
| 2 | The command was used wrongly |
| 3 | This run listed the instance first, and the runner did not answer its start within the wait: the runner-side steps are due; run `make deploy` again once they are done |
| 130 | Cancelled |

These are the statuses of `tools/arx/deploy.py`. `make` itself exits 2 whenever
the command fails, and shows the command's status as `Error 1` or `Error 3`; a
script that needs the status runs `uv run python tools/arx/deploy.py deploy …`
with the arguments `make -n deploy …` prints.

### Starting the same spec again

A start the runner refused holds for that instance: ARX keeps wanting it
running, and the runner does not try it again. The spec is usually not at
fault: the runner's engine was busy with another instance, say, or the runner
lacked something it needed. What it refused is in the runner's log, under the
event id `make deploy` shows; ARX does not report the runner's reason code yet,
and once it does `make deploy` shows it. Once that is put right:

    make deploy-stop STRATEGY=trend/my_idea MODE=sandbox [VERSION=0.1.0] [RUNNER=<runner id>]
    make deploy STRATEGY=trend/my_idea MODE=sandbox [VERSION=0.1.0] RUNNER=<runner id>

`make deploy` with the same variables finds the deployment receipt and the
spec it names, reads the receipt's instance from ARX and, once ARX lists it as
stopped or archived, says so before anything is asked: which instance ended,
as what, and, if its runner refused its start, when and why. It then starts a
new instance of that spec with one fresh authenticator code: the same spec,
its digest and its runner, so nothing in `deploy.yaml` changes and no new spec
is made. Starting a further instance needs the `ADMIN` or `OPERATOR` role
(`STRATEGIST` may create a spec, but not start a further instance of one).

- **The old instance is stopped first.** While ARX still wants the receipt's
  instance running, `make deploy` only reads what ARX and the runner say about
  it, as for any deployment already made: it asks no code, starts nothing, and
  for a refused start ends with the `make deploy-stop` to run. A runner runs
  one instance at a time, and an instance ARX wants running is one it may
  still be asked to run. A stop asks for its own code, so starting again after
  a refused start is two codes: one to stop, one to start.
- **The new instance is followed from then on.** The receipt names it in
  `instance_id`; the instances it replaced are in `earlier_instance_ids`, and
  `first_instance_id` stays the spec's first. `make deploy`, `make deploy-stop`
  and `make arx-status` act on `instance_id`.
- **The runner-side steps come again.** The runner has not had the new
  instance's id, so, as for a first instance, the command shows the steps of
  [step 6](#6-on-the-runners-machine-after-a-deployment) for the new id once
  and waits up to `TIMEOUT` seconds (180 unless given) for the runner. If the
  runner does not answer by then, it ends with exit status 3, and
  `make deploy`, with the same variables, asks no code and waits for the
  runner again. The product's approved contribution is allocated to the new instance
  in the console, as for a first one. Whether a new instance of a spec needs
  its own allocation when the contribution went in full to an earlier one has
  not been tried yet: allocate it as for a first one, and check in the console
  that the new instance's risk figures are not held back.
- **Running it again is safe.** The new instance's `Idempotency-Key` is
  derived from the spec and the instance it follows, so after an answer that
  was lost, running `make deploy` again finds the instance ARX started instead
  of starting a second. If ARX refuses it, nothing is started and the receipt
  is left as it was: a refused code is asked again (and once the attempts run
  out, the command says to wait for the next code and run `make deploy`
  again), and a missing role, a spec ARX does not have (404), another request
  under the same key (409) or a request ARX cannot read (422) each say what to
  look at.
- **Not for a spec ARX gave no instance.** When ARX refuses a spec an instance
  at all, `make deploy` ends with "ARX recorded the spec but did not start it"
  and still says to change `deploy.yaml` for a new spec: whether ARX's
  materialize request can start such a spec has not been tried yet, so
  `make deploy` does not send it there until it has been.

### Running it again

Every step reads what ARX already has before it writes, and each write carries
an idempotency key derived from what it writes, so the command can be run
again at any point:

- after it stopped part-way -- a network failure, ARX unavailable, or a
  product that is not active yet -- it carries on from there; nothing is made
  twice, and a product already created is found, not created again, so no
  code is asked for it again;
- after it finished, while ARX still wants the instance running, it asks for
  no code and sends nothing; it reads again what the runner says about the
  instance. Once ARX lists the instance as ended, it says so and starts the
  same spec as a new instance with one code
  ([Starting the same spec again](#starting-the-same-spec-again)).

How the ids are derived, so the same input always gives the same id:

| Id | Derived from |
|---|---|
| The release's id in ARX (`strategy_release_id`) | UUID version 5 of `<organisation id>:<release manifest digest>` (the digest as the receipt has it, `sha256:…`), in the namespace `5b0e7c1d-3a9f-4d62-8e15-7f2c4a6b9d03`. ARX keys releases by id across organisations, so two organisations deploying the same release get two ids; an organisation id holds no `:`, so the joined text is unambiguous |
| The strategy definition's name | The release's strategy coordinate without its `@<version>`: `strategy://github.com/<owner>/<repo>/<category>/<name>`, the same for every release of the strategy |
| The product's id (`product_id`) | UUID version 5 of `<organisation id>:<mode>:<strategy definition id>`, in the namespace `8f3b2a61-5c7d-4e9f-a1b2-6d4c8e0f7a35`: one per strategy and mode, as ARX allows. The definition is the same for every release, so the product is too |
| The spec's `idempotency_key` | UUID version 5 of the request digest, in the namespace `3d8a6f12-7c4e-4b9a-a5d0-1e6f2b8c9d47` |
| The `Idempotency-Key` of the other writes | UUID version 5, in the same namespace, of what the write is about: the organisation and the strategy definition's name (without a version, so the same for every release), the release id and release number, the release id and draft version, the organisation, mode and strategy definition id for the product, the instance id and its version for a stop, or `materialize`, the spec id and the instance it follows for a new instance of a spec whose instance has ended |

The request digest is the SHA-256 of the spec's body in canonical form without
its idempotency key and code, as the preview shows it. The same parameters
therefore always send the same key, and ARX answers the spec it already has.
Other parameters make another digest, another key and a new spec. For a
release already deployed on a runner, that is refused here rather than sent:
a runner runs one instance at a time, and the deployment already made is left
as it is. Stop it first with `make deploy-stop`.

Whether a deployment still runs is read from ARX each time, never taken from
the receipt alone. An instance stopped in the ARX console, or one that
`make deploy-stop` stopped waiting for, is recorded in the receipt as ARX
lists it, with `stopped_at` set to when this machine first saw it ended, and
holds nothing back. A receipt that names no instance yet is looked up by its
spec; a spec ARX refused an instance holds nothing back either. The same
parameters after the instance has ended return the same spec, whose ended
instance does not start again: the command says which instance ended and how,
then starts the same spec as a new instance with one authenticator code
([Starting the same spec again](#starting-the-same-spec-again)).

If the answer to the spec's creation was lost, the next run finds the spec ARX
listed for this release and runner and takes it as created, without a code. A
product whose creation went unanswered is found the same way, in the product
list.

When the product is created and the spec follows soon after -- the product
given capital and activated within the same half minute -- the second code
waits for your authenticator's next one: ARX takes each code once.

### The deployment receipt

`.deployments/<category>/<name>/<version>/<mode>-<runner>.json`, which git
ignores, records: the ARX address, the organisation and who deployed; the
strategy definition id; the release id, number and manifest digest; the
product id, the product's origin (`origin_artifact_source`) as ARX recorded
it, the release the product said was running once the first instance was
listed (`running_release`) and whether that is the release just deployed; the
spec id, its digest as ARX computed it, its idempotency key and
the request digest; the credential scope and execution channel; the spec's
first instance (`first_instance_id`), the instance the receipt follows now
(`instance_id`, the same until `make deploy` starts another of the same spec once it has ended) and those
it replaced (`earlier_instance_ids`), and its state as ARX last listed it; the runner observation last read
(`runner_observation`), what it means (`runner_check`: `confirmed`,
`rejected`, `awaiting`, `not running`, `unsupported` or `unreadable`), when
it was read (`runner_checked_at`), whether it was waited for
(`runner_waited`), whether this run was the first to list the instance
(`first_listed_by_this_run`) and how long it waited (`runner_check_basis`);
what is left to do on the runner's machine (`runner_todo`: binding the instance
and restarting the runner until the runner confirms the start, which it does
only once it is bound, and stopping this release before another is deployed);
and when it was created, updated and stopped. `.progress.json` in the same directory
records each step as it completes.

## 6. On the runner's machine, after a deployment

The command shows these steps once, as it begins to wait for the runner, and
the receipt lists them; if the runner does not answer within the wait, the
command's last lines list them again. Unless something binds new instances for
the runner, its operator does this there:

1. Add the new instance (its id, the spec id and the spec digest are in the
   receipt) to the runner's capability bindings, and publish its capability
   again with `arx-runner publish-capability`, as the runner's documentation
   describes.
2. **Then restart the runner.** It reads its capability receipt only when it
   starts; until it restarts, the new instance's commands wait for a binding.
   The `restart_required` in the publication's receipt refers to the runner.
   If this is done while `make deploy` waits, the same run sees the runner
   confirm the start. If it ended first, with exit status 3, run `make deploy`
   again, with the same variables: it asks for no code and waits until the
   runner says it started the instance.
3. A runner process runs one instance at a time, and a strategy runs one
   release per mode. Before another release of the strategy is deployed in the
   mode, on this runner or another, stop this one (next section).

## 7. Changing release

A strategy runs one release at a time in each mode. Changing to a new release
is stop, then deploy:

    make deploy-stop STRATEGY=trend/my_idea MODE=sandbox [VERSION=0.1.0] [RUNNER=<runner id>]
    make deploy STRATEGY=trend/my_idea MODE=sandbox RUNNER=<runner id>

`make deploy-stop` reads the instance the deployment receipt names, stops it
with one fresh authenticator code, waits until ARX lists it as stopped, and
records that in the receipt; an instance already stopped needs no code. It
ends by giving the `make deploy` for the next release, with the runner the
stopped one used; the next release finds the same product.
`VERSION` and `RUNNER` are needed only when this machine's receipts name more
than one deployment still running in the mode. Stopping needs the `ADMIN` or
`OPERATOR` role. What a stopped instance does with its open positions is the
shutdown policy in `deploy.yaml`.

The new release then deploys as in [step 5](#5-deploying), to the same
product, with no product steps (check its allocations, as
[The product](#the-product) says), and the runner-side steps of
[step 6](#6-on-the-runners-machine-after-a-deployment) follow it again. It must
keep the same trading account: `credential_scope` and `engine_binding_id` in
`deploy.yaml` stay as they were.

If the new release is deployed while the old one still runs, ARX refuses it and
nothing is created; the command says so and gives the `make deploy-stop` to
run. Deploying the same release again with the same parameters after stopping
it does not start the stopped instance again, since ARX answers the stopped
spec; the command says so and starts that spec as a new instance, with one
authenticator code ([Starting the same spec again](#starting-the-same-spec-again)).

`make next STRATEGY=trend/my_idea` follows this path as well: once a version is
released and you are signed in, it points at the preview and the deployment,
at `make deploy-stop` while a deployment made from this machine still runs, at
`make deploy` once the released version's deployment is stopped (it starts its
spec as a new instance), and at
stopping the older release first once a newer one is released. Whether a
deployment still runs is read from ARX, as `make deploy` reads it, so one
stopped in the ARX console is not offered a stop; when ARX cannot be read, the
receipt's state is used and the report says so. None of this
waits for a local runner: it is offered whether or not this machine has run
`make setup-runner`.
