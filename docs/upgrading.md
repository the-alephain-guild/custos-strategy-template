# Taking updates from the template

Two kinds of update reach a repository made from this template, and each has its
own command.

## The template's own files

Tools, scripts, docs, the example and the toolchain pins all belong to the
template, so they merge without touching your strategies:

    git fetch upstream
    git merge upstream/main
    make toolkit
    make verify

`upstream` is this template: a fork has it after `git remote add upstream
https://github.com/the-alephain-guild/custos-strategy-template.git`, and a
private copy made as the README describes already has it. Run `make toolkit`
after merging, since an update may pin a newer toolkit or NautilusTrader.

## A strategy's skeleton

Each strategy records the template commit it was made from in its
`.copier-answers.yml`. After merging an update that changed
`templates/strategy/`, commit your own work first (copier refuses a directory with
uncommitted changes), then bring the change into a strategy with:

    uvx copier update strategies/trend/my_idea

Where you and the template changed the same lines, copier marks a conflict in the
file instead of overwriting your edit; resolve it and commit. Run the strategy's
tests afterwards with `make test`.
