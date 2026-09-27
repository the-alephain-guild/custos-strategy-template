# Modeling

Prototypes and analysis that come before the code live here: run the rule once in
plain Python or a notebook to see roughly how it behaves on past data, write the
rule you settle on into `design.md`, and only then implement it in `refinement/`.

- `prototype.py` — a prototype that depends on no trading engine. It reads the
  parameters from the same `config.yaml`, computes the signal and prints what it
  would have done.
- `analysis/` — notebooks, charts and intermediate results. Raw market data stays
  out of the repository.
- A TradingView Pine prototype can go in `prototype.pine`; add it yourself, it is
  not generated.

What comes out of here is a study, not a result. Results that count come from
`make backtest` and are recorded in `backtests/README.md`.
