# Alpha Factor Lab Notes

Date: 2026-07-06

## Scope

ETF pool, start date `2022-05-06`, latest local ETF data through `2026-07-06`.

The lab now supports two modes:

- `single`: single-factor ETF rotation, top factor scores every rebalance window.
- `alpha042_filter`: keep ETF Alpha042 entry/exit logic, but filter and sort buy signals by factor rank.

## Single-Factor Findings

Best raw single-factor candidates:

| Factor | Meaning | Return | Max DD | PF |
|---|---|---:|---:|---:|
| `alpha053` | 12-day up-day ratio | +205.76% | 24.70% | 1.78 |
| `alpha058` | 20-day up-day ratio | +157.79% | 37.00% | 1.82 |
| `alpha040` | up-volume / down-volume | +145.89% | 26.41% | 1.61 |
| `alpha043` | 6-day OBV strength | +178.05% | 30.60% | 1.48 |
| `alpha161` | low ATR preference | +80.56% | 9.06% | 2.12 |

Read: raw momentum factors can produce high return, but drawdown is much higher than the current ETF Alpha042. `alpha161` is the notable defensive factor.

## Alpha042 + Factor Filter

Baseline ETF Alpha042 on the same latest local data:

| Variant | Return | Max DD | PF | Trades |
|---|---:|---:|---:|---:|
| `base` | +138.52% | 8.74% | 4.23 | 157 |

Best `alpha042_filter` candidates using top 70% factor rank:

| Variant | Return | Max DD | PF | Trades |
|---|---:|---:|---:|---:|
| `alpha040_top70` | +148.23% | 9.78% | 5.15 | 142 |
| `alpha084_top70` | +140.12% | 9.85% | 4.95 | 136 |
| `alpha176_top70` | +126.37% | 9.71% | 4.70 | 136 |
| `alpha127_top70` | +130.06% | 11.42% | 4.45 | 136 |
| `alpha161_top70` | +94.55% | 9.71% | 4.23 | 127 |

## Current Takeaway

`alpha040_top70` is the best candidate for an Alpha042 enhancement:

- Improves full-period return: `+138.52% -> +148.23%`.
- Improves PF: `4.23 -> 5.15`.
- Raises drawdown slightly: `8.74% -> 9.78%`.
- Reduces trades: `157 -> 142`.

It is not yet a production replacement, because period tests show it is weaker in `2022_2023`, while stronger from `2023` onward. Treat it as a promising candidate for a new strategy variant, not as an automatic replacement for ETF Alpha042.

`alpha161` is better as a defensive overlay than a return enhancer. It can reduce noise and improve win rate in weak regimes, but it suppresses long-run return when used as a hard filter.

## Key Output Files

- `alpha_factor_lab_20260706_190219.csv`: expanded 27-factor single mode results.
- `alpha_factor_lab_20260706_190903.csv`: focused Alpha042 filter results at top 70%.
- `alpha042_filter_period_compare_20260706.csv`: multi-period comparison for base and top filter candidates.

