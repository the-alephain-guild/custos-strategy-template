# Deploying a release through ARX

A release published with `make release` ([releasing.md](releasing.md)) is
deployed through ARX, which authorizes the deployment and hands it to a runner.
This guide covers the parts of that path this template provides so far:
signing in to ARX from this repository and keeping the session, and reading a
release back from its package in the form ARX drafts a release from. Deploying
itself is not a command yet; until it is, `make start` runs a strategy from its
source directory as before.

Every call these commands make to ARX is one ARX documents in its public API
guides (*Sign-In & Sessions*, *API Conventions*, *Release & Deployment API*).
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
