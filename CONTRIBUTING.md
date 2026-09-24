# Contributing

Pull requests that improve the template, the tools, the example or the docs are
welcome.

## What a pull request may change

Only files that belong to the template: everything except `strategies/` and
`registry/`. A pull request that touches either of those fails the ownership
check. It would put your strategy in every fork, and it would publish it.

If your change is to `templates/strategy/`, say in the pull request what existing
strategies will see when their owners run `copier update`.

## Before you open it

    make verify

runs every check CI runs: the two disclosure checks, the ownership check, lint,
and all tests. Changes to the backtest or the local runner should also say how
they were exercised: a backtest range, or a sandbox run.

## Sign-off

Every commit carries a Developer Certificate of Origin sign-off, which
`git commit -s` adds:

    Signed-off-by: Your Name <you@example.com>

By signing off you state that you wrote the change or otherwise have the right to
contribute it under the Apache License 2.0.
