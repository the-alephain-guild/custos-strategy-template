# Deploying a release through ARX

A release published with `make release` ([releasing.md](releasing.md)) is
deployed through ARX, which authorizes the deployment and hands it to a runner.
This guide covers the parts of that path this template provides so far:
signing in to ARX from this repository and keeping the session. Deploying
itself is not a command yet; until it is, `make start` runs a strategy from its
source directory as before.

Every call these commands make is one ARX documents in its public API guides
(*Sign-In & Sessions*, *API Conventions*). They use your own ARX account: ARX has
no machine credentials, so a command acts as you, with your roles, and ARX
records it against you.

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
