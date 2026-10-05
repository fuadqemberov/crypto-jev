# Paper order incident review — 5 October 2026

Input: operator-supplied six-hour dashboard and Freqtrade journals. Times below are Baku (UTC+4).
This is an execution incident review, not a backtest of JEV or proof of trading profitability.

| Trade | Open → close | Side / leverage | Entry → exit | Realized PnL | Framework exit |
|---|---|---|---|---|---|
| APT | 18:28:05 → 18:34:06 | LONG / 5× | 0.816 → 0.8038 | −9.33590424 USDT | stop_loss |
| LAB | 18:37:38 → 18:49:03 | SHORT / 6× | 0.04877 → 0.04915 | −7.19408816 USDT | trailing_stop_loss |
| SLX | 18:49:06 → 19:03:31 | SHORT / 6× | 0.06029 → 0.06061 | −4.80318125 USDT | trailing_stop_loss |

Total: −21.33317365 USDT, about 1.067% of the initial 2,000 USDT paper wallet. Stop exit loss,
including fees, was below the initial 10 USDT per-position capital-risk budget in these fills.
This does not guarantee the risk budget under gaps/slippage.

## Confirmed software defects

1. **PnL API mismatch.** `_risk_state()` used `trade.calc_profit(...).profit_abs`. In pinned Freqtrade
   2026.8 `calc_profit()` returns a float. The journal contains 1,851 executor-risk AttributeErrors
   and 18 ValueErrors. A test with a real Freqtrade Trade reproduces failed risk readiness. Use
   `calculate_profit(...).profit_abs`. Missing/stale marks continue to fail closed.
   The old test double incorrectly returned a structured result from the legacy method; it hid the bug.
   Failed risk telemetry disabled new entries; it does not by itself explain the three price losses.
2. **Stop precision ratchet.** A persisted rounded SHORT stop was converted back to a relative
   ratio on every loop; floating-point loss followed by exchange rounding could subtract another tick.
   Real Freqtrade `ft_stoploss_adjust()` tests reproduce LAB stop drift from 0.04929 to 0.04926 and
   SLX from 0.06098 to 0.06097 across 1,000 varying-price callbacks. Keep persisted protection with
   `None` once it is equal/tighter than the planned stop. Preserve tighter pre-upgrade stops.
   This is a plausible contributor to early SHORT exits, not proof of their complete price path.
   Freqtrade labels any tightened stop as trailing even when strategy `trailing_stop=False`.

## Evidence gaps and changes

- 83,087 JEV errors were logged only as `JevError`. The journal cannot distinguish HTTP authorization,
  quota, network or schema faults. Add safe machine-readable reason codes, without upstream bodies.
- Existing journals lack JEV entry confidences/context. Add structured eligible-analysis, final-entry
  and protective-plan events. Add a read-only bounded DB audit exporter for the operator's historical context.
- No threshold reductions, guessed directional rules, historical JEV backtest or promise of profitable
  entries is justified by these three trades. Direction still belongs to JEV and all risk guards remain.

## Validation and migration

New regressions first failed against the old code (two precision cases and real open-position heartbeat).
The corrected version passes these and the full suite. Existing DBs and four/five-part entry tags survive.
Restart dashboard and Freqtrade after updating; do not erase databases or widen existing stops.
Export `python -m app.audit --hours 6` for deeper analysis of the JEV decisions. This reads the latest
10,000 matching analysis rows; missing evidence beyond that limit is explicitly documented.
