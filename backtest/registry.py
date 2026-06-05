from importlib import import_module
from pathlib import Path

STRATEGY_PACKAGE = "backtest.strategy"
STRATEGY_DIR = Path(__file__).with_name("strategy")


def _strategy_module_names():
    module_names = []
    for path in sorted(STRATEGY_DIR.glob("*.py")):
        if path.name == "__init__.py" or path.stem.startswith("_"):
            continue
        if not path.stem.startswith("strategy_"):
            continue
        module_names.append(f"{STRATEGY_PACKAGE}.{path.stem}")
    return module_names


def _load_strategy_modules():
    modules = [import_module(module_name) for module_name in _strategy_module_names()]
    ids = set()
    for module in modules:
        meta = getattr(module, "META", None)
        if not isinstance(meta, dict):
            raise ValueError(f"{module.__name__} must define META")
        for key in ("id", "name", "description"):
            if not meta.get(key):
                raise ValueError(f"{module.__name__} META missing {key}")
        if not hasattr(module, "generate_signals"):
            raise ValueError(f"{module.__name__} must define generate_signals")
        if meta["id"] in ids:
            raise ValueError(f"duplicate strategy id: {meta['id']}")
        ids.add(meta["id"])
    return modules


def list_strategies():
    return [module.META for module in _load_strategy_modules()]


def get_strategy(strategy_id):
    for module in _load_strategy_modules():
        if module.META["id"] == strategy_id:
            return module
    raise KeyError(f"unknown strategy: {strategy_id}")
