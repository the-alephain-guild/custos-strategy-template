# Releasing a strategy

`make release STRATEGY=trend/my_idea` publishes a signed release of one
strategy: an immutable, versioned package that a Custos runner can verify and
run without seeing your source tree. Running a strategy locally with
`make start` does not need a release; a release is what you hand to a
deployment.

## What happens

1. `make release` checks that the release can be built from what GitHub has:
   - the checkout has no uncommitted changes;
   - the current branch's commit is on `origin` (push it first; `make release`
     pushes nothing);
   - `gh` is logged in with the `workflow` scope;
   - `strategies/trend/my_idea/pyproject.toml` has a `[project] version`.
2. It starts `.github/workflows/release-strategy.yml` on that branch. The
   workflow calls Custos's reusable publishing workflow, pinned under
   `[publisher]` in `toolchain.lock.toml`. That workflow:
   - builds the strategy twice from the pushed commit and refuses to go on
     unless the two builds are byte for byte the same;
   - type-checks it strictly against the pinned toolkit and engine;
   - signs the release and pushes it to this repository's package,
     `ghcr.io/<owner>/<repo>/strategy-releases`, then reads it back by digest.
3. `make release` follows the run in your terminal and downloads its receipt
   into `.releases/trend/my_idea/<version>/`. The receipt names the package, the
   release's tag and digests, the commit and the workflow run; a deployment
   refers to the release through it. `.releases/` is not committed.

## Versions

A release's version is the strategy's `[project] version`. A version is
published once: releasing it again fails in the publishing job, and
`make release` says so. Raise the version, commit, push and release again.

## What is public

The package inherits this repository's visibility, so a private repository's
releases stay private, and only accounts you give read access can pull them.

The signature is not private. Releases are signed keylessly through Sigstore's
public instance: the signing certificate names this repository, the commit and
the publishing workflow, and Sigstore records the certificate and the signed
statement's digest in its public transparency log. The log holds no strategy
code, configuration or parameters, but anyone can see that this repository
published a release at that commit.

## Who trusts a release

A release is trusted by name, not by where it came from: a runner, and the
service that deploys to it, are configured with the publishing workflow at its
tag and with your repository. Both check that the release's certificate names
exactly that pair, so another repository calling the same workflow cannot
publish in your name. Upgrading the pinned `[publisher]` tag changes the
signing identity, and whoever trusts your releases has to add the new tag.

## If the workflow cannot start

- **`gh workflow run` says the workflow does not exist**: the workflow file is
  on the default branch only once it has been pushed there; push it first.
- **The run fails at once, before any job starts**: the repository's Actions
  settings may restrict which workflows it may call. Under *Settings → Actions →
  General*, allow `the-alephain-guild/custos/.github/workflows/publish-strategy-release.yml`,
  or all actions and reusable workflows.
- **`gh` lacks the `workflow` scope**: `gh auth refresh -s workflow`.

## Next: deploying it

A release does nothing until it is deployed. [deploying.md](deploying.md) takes
it through ARX to a running instance on a runner, starting with signing in and
reading the release back from its package:

    make arx-login ARX_URL=https://arx.example.com
    make arx-evidence STRATEGY=trend/my_idea VERSION=<version>
