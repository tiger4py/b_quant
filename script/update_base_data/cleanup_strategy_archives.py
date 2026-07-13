#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clean historical strategy archives under data/strategy.

Default behavior is dry-run: print what would be deleted, but do not delete.
Use --apply to actually remove files. Non-JSON analysis artifacts are cleaned
by default; use --keep-artifacts to preserve them.
"""

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STRATEGY_ROOT = ROOT_DIR / "data" / "strategy"


@dataclass(frozen=True)
class ArchiveFile:
    path: Path
    rank_key: tuple


def _date_from_parts(path: Path):
    """Return a sortable date tuple found in filename or parent names."""
    text = "/".join(path.parts)

    # Prefer YYYY-MM-DD in filename/path.
    matches = re.findall(r"(20\d{2})-(\d{2})-(\d{2})", text)
    if matches:
        y, m, d = matches[-1]
        return int(y), int(m), int(d), 0, 0, 0

    # Support timestamp-style files such as 20260712_233057.json.
    match = re.search(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})", path.stem)
    if match:
        y, m, d, hh, mm, ss = match.groups()
        return int(y), int(m), int(d), int(hh), int(mm), int(ss)

    # Support compact date files such as 20260712.json.
    match = re.search(r"(20\d{2})(\d{2})(\d{2})", path.stem)
    if match:
        y, m, d = match.groups()
        return int(y), int(m), int(d), 0, 0, 0

    return 0, 0, 0, 0, 0, 0


def _sequence_from_name(path: Path) -> int:
    """Return trailing sequence from archive name, e.g. 2026-07-10_02 -> 2."""
    match = re.search(r"_(\d+)$", path.stem)
    return int(match.group(1)) if match else 0


def _archive_rank_key(path: Path):
    stat = path.stat()
    return (
        stat.st_mtime,
        _date_from_parts(path),
        _sequence_from_name(path),
        path.as_posix(),
    )


def _iter_strategy_dirs(strategy_root: Path, selected):
    if selected:
        for name in selected:
            path = strategy_root / name
            if path.is_dir():
                yield path
            else:
                print(f"[skip] strategy not found: {name}")
        return

    for path in sorted(strategy_root.iterdir()):
        if path.is_dir():
            yield path


def _list_json_archives(strategy_dir: Path):
    archives = []
    for path in strategy_dir.rglob("*.json"):
        archives.append(ArchiveFile(path=path, rank_key=_archive_rank_key(path)))
    return sorted(archives, key=lambda item: item.rank_key, reverse=True)


def _list_artifacts(strategy_dir: Path):
    files = []
    for path in strategy_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() == ".json":
            continue
        files.append(path)
    return sorted(files)


def _remove_empty_dirs(strategy_dir: Path, apply: bool):
    removed = []
    for path in sorted(
        [p for p in strategy_dir.rglob("*") if p.is_dir()],
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            next(path.iterdir())
        except StopIteration:
            removed.append(path)
            if apply:
                path.rmdir()
    return removed


def cleanup(strategy_root: Path, keep: int, selected, clean_artifacts: bool, apply: bool):
    strategy_root = strategy_root.resolve()
    if not strategy_root.exists():
        raise FileNotFoundError(f"strategy root not found: {strategy_root}")
    if keep < 1:
        raise ValueError("--keep must be >= 1")

    total_delete = 0
    total_keep = 0
    total_artifacts = 0

    for strategy_dir in _iter_strategy_dirs(strategy_root, selected):
        archives = _list_json_archives(strategy_dir)
        keep_files = archives[:keep]
        delete_files = archives[keep:]
        artifact_files = _list_artifacts(strategy_dir) if clean_artifacts else []

        total_keep += len(keep_files)
        total_delete += len(delete_files)
        total_artifacts += len(artifact_files)

        rel_strategy = strategy_dir.relative_to(strategy_root)
        print(f"\n[{rel_strategy}] json={len(archives)} keep={len(keep_files)} delete={len(delete_files)}")
        for item in keep_files:
            print(f"  keep   {item.path.relative_to(strategy_root)}")
        for item in delete_files:
            print(f"  delete {item.path.relative_to(strategy_root)}")
        for path in artifact_files:
            print(f"  delete {path.relative_to(strategy_root)}")

        if apply:
            for item in delete_files:
                item.path.unlink()
            for path in artifact_files:
                path.unlink()

        empty_dirs = _remove_empty_dirs(strategy_dir, apply=apply)
        for path in empty_dirs:
            action = "removed-dir" if apply else "empty-dir"
            print(f"  {action} {path.relative_to(strategy_root)}")

    action = "deleted" if apply else "would delete"
    print(
        f"\nDone: kept {total_keep} JSON archive(s), "
        f"{action} {total_delete} old JSON archive(s)"
        + (f" and {total_artifacts} artifact file(s)" if clean_artifacts else "")
        + "."
    )
    if not apply:
        print("Dry-run only. Re-run with --apply to delete files.")


def main():
    parser = argparse.ArgumentParser(description="Clean historical strategy archives.")
    parser.add_argument(
        "--strategy-root",
        type=Path,
        default=DEFAULT_STRATEGY_ROOT,
        help="Strategy archive root. Default: data/strategy",
    )
    parser.add_argument("--keep", type=int, default=1, help="JSON archives to keep per strategy.")
    parser.add_argument(
        "--strategy",
        action="append",
        default=[],
        help="Only clean this strategy directory. Can be repeated.",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Preserve non-JSON files such as CSV/XLSX analysis artifacts.",
    )
    parser.add_argument(
        "--include-artifacts",
        action="store_true",
        help="Deprecated: artifacts are cleaned by default.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually delete files.")
    args = parser.parse_args()

    cleanup(
        strategy_root=args.strategy_root,
        keep=args.keep,
        selected=args.strategy,
        clean_artifacts=not args.keep_artifacts,
        apply=args.apply,
    )


if __name__ == "__main__":
    main()
