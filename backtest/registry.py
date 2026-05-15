from importlib import import_module

STRATEGY_MODULES = [
    "backtest.strategy_ma_cross",
    "backtest.strategy_macd_cross",
    "backtest.strategy_rsi_reversal",
    "backtest.strategy_bollinger_reversion",
    "backtest.strategy_breakout_20",
    "backtest.strategy_dual_ma_trend",
    "backtest.strategy_volume_breakout",
    "backtest.strategy_kdj_cross",
    "backtest.strategy_atr_trailing",
    "backtest.strategy_momentum_60",
    "backtest.strategy_event_volatility",
    "backtest.strategy_theme_phase",
]


def list_strategies():
    items = []
    for module_name in STRATEGY_MODULES:
        module = import_module(module_name)
        items.append(module.META)
    return items


def get_strategy(strategy_id):
    for module_name in STRATEGY_MODULES:
        module = import_module(module_name)
        if module.META["id"] == strategy_id:
            return module
    raise KeyError(f"unknown strategy: {strategy_id}")
