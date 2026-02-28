# 📂 Project File Organization

## Core Engine
| File               | Purpose                                      |
|--------------------|----------------------------------------------|
| `main.py`          | Entry point / live trading loop              |
| `execution.py`     | Order/exit logic, `check_exit_condition`, scalp gates |
| `entry_logic.py`   | Signal scoring (100‑pt composite), entry gating |
| `signals.py`       | Signal generation / detection                |
| `orchestration.py` | Session/trade orchestration                  |
| `position_manager.py` | Open position tracking                   |

---

## Indicators & Market Data
| File               | Purpose                                      |
|--------------------|----------------------------------------------|
| `indicators.py`    | Technical indicator calculations             |
| `market_data.py`   | Market data fetching                         |
| `data_feed.py`     | Real‑time feed adapter                       |
| `candle_builder.py`| Tick → candle aggregation                    |
| `tickdb.py`        | Tick data storage                            |

---

## Strategy Modules
| File                   | Purpose                                   |
|------------------------|-------------------------------------------|
| `option_exit_manager.py` | HFT exit engine (DTS, momentum, vol reversion) |
| `st_pullback_cci.py`   | Supertrend Pullback + CCI Rejection entries |
| `compression_detector.py` | 15m compression/expansion state machine |
| `daily_sentiment.py`   | Daily bias: CPR, Camarilla, balance zone, VAH/VAL |
| `day_type.py`          | Day type classification                   |

---

## Configuration & Broker
| File               | Purpose                                      |
|--------------------|----------------------------------------------|
| `config.py`        | All constants (MAX_TRADES=8, thresholds, etc.) |
| `setup.py`         | Session setup / initialization               |
| `broker_init.py`   | Broker connection initialization             |
| `order_utils.py`   | Order placement utilities                    |
| `trade_classes.py` | `ScalpTrade` / `TrendTrade` dataclasses       |

---

## Analysis & Dashboard
| File                   | Purpose                                   |
|------------------------|-------------------------------------------|
| `log_parser.py`        | Parse trade logs → `SessionSummary`       |
| `dashboard.py`         | Reports: CSV, JSON, charts, compare sessions |
| `analyze_trades.py`    | Post‑session trade analysis               |
| `debug.py`             | Debug utilities                           |
| `diagnose_scoring.py`  | Entry score diagnostics                   |

---

## Replay / Validation
| File                          | Purpose                             |
|-------------------------------|-------------------------------------|
| `run_replay_v7.py`            | Replay runner (v7)                  |
| `replay_analyzer_v7.py`       | Replay analysis engine              |
| `replay_option_exit_validation.py` | Option exit replay validation |
| `validate_exit_v7.py`         | Exit logic validation               |

---

## Test Suite
| File                          | Tests                               |
|-------------------------------|-------------------------------------|
| `test_exit_logic.py`          | 71 tests — exit logic               |
| `test_profitability_fixes.py` | 126 tests — P1–P4 fixes             |
| `test_compression_detector.py`| 30 tests — compression state machine |
| `test_st_pullback_cci.py`     | 35 tests — ST pullback strategy     |
| `test_dashboard.py`           | 125 tests — dashboard/log parser    |
| `test_entry_exit_refinements.py` | Entry/exit refinement tests      |
| `test_score_breakdown.py`     | Score breakdown tests               |
| `test_supertrend_alignment.py`| Supertrend alignment tests          |
| `test_exit_logic_v7.py`       | v7 exit logic tests                 |
| `conftest.py`                 | Pytest fixtures/config              |