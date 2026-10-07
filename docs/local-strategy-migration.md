# Local strategy migration — 2026-10-06

The user explicitly replaces the former “JEV decides direction” requirement with local
deterministic decisions. Paper-only execution, fail-closed behavior, risk budgets,
Azerbaijani UI, and the two-process boundary remain.

## File-level changes

- `app/strategy.py`: versioned trend-continuation evaluator with reclaim, pullback and breakout setups; all gates explicit;
  position-bound trend invalidation exits; finite/fresh input validation.
- `app/indicators.py`: previous close/EMA and EMA50 slope inputs from closed bars only.
- `app/config.py`: remove API/model/confidence/cache settings; validate `EntryRules`.
- `app/engine.py`: remove AI network and semantic-answer cache, evaluate locally from
  one validated market snapshot; prioritize recent trend candidates and open positions.
- `app/jev.py`, `app/cache.py`: deleted. `Store` no longer accesses AI cache tables;
  existing databases are not altered destructively.
- `app/execution.py`: accept only current local strategy results; local exit intents;
  unchanged risk and freshness vetoes. No entry on telemetry/disk failures.
- `app/bridge.py`: protocol v4, preventing mixed old/new processes from entering.
- `SignalBridgeStrategy.py`: renamed executor, `rule:` entry tags, `thesis_exit` reason;
  fixed stop rounding and absolute PnL correction from the branch retained.
- `JevBridgeStrategy.py`: compatibility subclass ONLY for installed service names.
  Legacy configuration fields and trade tags remain readable for migration safety.
- `app/paper.py`: migrate config keys/class while preserving credentials, wallet and DB.
- UI: remove model probabilities, key warnings and 90% display; show rule outcomes.
- Tests: remove obsolete external-model contract/cache tests; retain market/security/
  risk/execution regressions and add local-rule, no-network and migration coverage.

## Safety semantics

Trend candidates are NOT executable signals. A 15m reclaim, confirmed pullback, or 20-candle breakout with all strategy gates passes to
bridge checks, then Freqtrade confirms funding-adjusted risk, spread, slippage, daily
loss, cooldown and duplicate history. Risk sizing still caps planned capital loss at
0.5% and margin at 7%, subject to costs and exchange minimums. Stops are fixed entry
plans, not recalculated from subsequent ATR. Both legacy/new trade tags are supported.

No historical backtest or production latency benchmark was performed. Unit tests
cannot prove expected return. The official Freqtrade strategy repository was reviewed;
no code was copied because its examples are expressly educational and need independent
validation. This change removes API latency and nondeterministic confidence gates,
not market risk.

See README for exact service migration commands. No database wipe is needed. Restart
both services; v3/v4 mismatch deliberately blocks entries. The old AI cache table may
remain inert on disk. Existing environment keys are ignored, never read for requests.

## Validation on this revision

- 210 Python tests passed with real Freqtrade 2026.8 installed.
- Offline Playwright/Chromium desktop and mobile checks passed, including strict
  ready-signal filtering, WAIT on blocked entries, removed confidence UI and offline controls.
- JavaScript syntax, Python compilation and git whitespace checks passed.
- Synthetic blocked 500-symbol radar test published the priority result in under
  1 ms in this run; this is a scheduler regression check, not a network benchmark.
