r"""Обновление общей базы документации MP API на QNAP.

Запускает все парсеры (Lamoda / Ozon / WB / YA / CRPT / Деловые Линии / Байкал Сервис) и складывает результат в
общую базу:

    \\Qnap\MEDIA\! Бек учёт\База MD\<Маркетплейс>\md\...

Каждый парсер читает env-переменную DOCS_OUT_ROOT — если она задана,
парсер пишет туда (а не в локальную ./md рядом со своим скриптом).

По умолчанию все парсеры запускаются ПАРАЛЛЕЛЬНО (они независимы: разные
папки и разные DOCS_OUT_ROOT). Вывод каждого пишется в свой лог-файл
``_run_<slug>.log`` рядом с этим скриптом, в консоль идут только статусы
и итоги. Для отладки есть ``--sequential`` — тогда вывод парсеров идёт
прямо в консоль по очереди.

Использование:
    python update_docs_bases.py                     # все парсеры, параллельно
    python update_docs_bases.py wb ozon             # только выбранные
    python update_docs_bases.py --sequential        # по очереди, лог в консоль
    python update_docs_bases.py --dry-run           # показать команды, не запускать
    python update_docs_bases.py --root D:\backup\MD # перекрыть базовый путь
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def _ts() -> str:
    """Текущее время HH:MM:SS для префикса строк-статусов."""
    return datetime.now().strftime("%H:%M:%S")

# Рамки печатаются символами ═; в консоли cp1251 это упало бы с UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).parent

DEFAULT_BASE_ROOT = Path(r"\\Qnap\MEDIA\! Бек учёт\База MD")

# slug → (директория парсера, имя скрипта, отображаемое имя)
PARSERS: dict[str, tuple[Path, str, str]] = {
    "lamoda": (HERE / "Lamoda Docs Parser", "lamoda_docs_parser.py", "Lamoda"),
    "ozon":   (HERE / "Ozon Docs Parser",   "ozon_docs_parser.py",   "Ozon"),
    "wb":     (HERE / "WB Docs Parser",     "wb_docs_parser.py",     "WB"),
    "ya":     (HERE / "YA Docs Parser",     "ya_docs_parser.py",     "YA"),
    "crpt":   (HERE / "CRPT Docs Parser",   "crpt_docs_parser.py",   "CRPT"),
    "dellin": (HERE / "Dellin Docs Parser", "dellin_docs_parser.py", "Dellin"),
    "baikal": (HERE / "Baikal Docs Parser", "baikal_docs_parser.py", "Baikal"),
}


def _prepare(slug: str, base_root: Path) -> tuple[Path, list[str], str, dict[str, str]]:
    """Готовит окружение запуска: cwd, команду, отображаемое имя, env."""
    parser_dir, script, display = PARSERS[slug]
    out_root = base_root / display / "md"
    out_root.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["DOCS_OUT_ROOT"] = str(out_root)
    # На случай кириллицы в путях логов.
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    cmd = [sys.executable, script]
    print(f"\n{'═' * 72}")
    print(f"  [{display}]  →  {out_root}")
    print(f"  cwd: {parser_dir}")
    print(f"  cmd: {' '.join(cmd)}")
    print(f"{'═' * 72}", flush=True)
    return parser_dir, cmd, display, env


def run_sequential(slug: str, base_root: Path, dry_run: bool) -> tuple[str, int, float]:
    """Запуск одного парсера с выводом прямо в консоль (для отладки)."""
    parser_dir, cmd, display, env = _prepare(slug, base_root)
    print(flush=True)
    if dry_run:
        return display, 0, 0.0

    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(parser_dir), env=env, check=False)
        rc = proc.returncode
    except FileNotFoundError as e:
        print(f"  ! Не найден скрипт: {e}", file=sys.stderr)
        rc = 127
    elapsed = time.monotonic() - t0
    return display, rc, elapsed


def run_parallel(selected: list[str], base_root: Path, dry_run: bool) -> list[tuple[str, int, float]]:
    """Запускает все выбранные парсеры разом, вывод каждого — в свой лог-файл."""
    jobs: list[tuple[str, subprocess.Popen, object, Path, float]] = []
    for slug in selected:
        parser_dir, cmd, display, env = _prepare(slug, base_root)
        if dry_run:
            jobs.append((display, None, None, None, 0.0))  # type: ignore[arg-type]
            continue
        log_path = HERE / f"_run_{slug}_{datetime.now():%Y-%m-%d}.log"
        log_fh = open(log_path, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(parser_dir), env=env,
                stdout=log_fh, stderr=subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            log_fh.write(f"! Не найден скрипт: {e}\n")
            log_fh.close()
            jobs.append((display, None, None, log_path, -127.0))  # type: ignore[arg-type]
            continue
        print(f"  [{_ts()}] старт {display:<8} → лог: {log_path}", flush=True)
        jobs.append((display, proc, log_fh, log_path, time.monotonic()))

    if dry_run:
        return [(d, 0, 0.0) for d, *_ in jobs]

    live = [j for j in jobs if j[1] is not None]
    print(
        f"\n  Запущено процессов: {len(live)}. Логи пишутся вживую, можно смотреть:",
        flush=True,
    )
    for display, _proc, _fh, log_path, _t0 in live:
        print(f"    {display:<8} → {log_path}", flush=True)
    print("  Жду завершения (печатаю по мере готовности)…\n", flush=True)

    results: list[tuple[str, int, float]] = []
    # Сразу учитываем те, что не стартовали (не нашли скрипт).
    for display, proc, _fh, _lp, _t0 in jobs:
        if proc is None:
            results.append((display, 127, 0.0))

    pending = list(live)
    while pending:
        still: list[tuple] = []
        for display, proc, log_fh, log_path, t0 in pending:
            rc = proc.poll()
            if rc is None:  # ещё работает
                still.append((display, proc, log_fh, log_path, t0))
                continue
            log_fh.close()
            elapsed = time.monotonic() - t0
            mark = "ok " if rc == 0 else "FAIL"
            print(f"  [{_ts()}] [{mark}] {display:<8}  rc={rc:<3}  {elapsed:6.1f} s  → {log_path}", flush=True)
            if rc != 0:
                _print_log_tail(log_path)
            results.append((display, rc, elapsed))
        pending = still
        if pending:
            time.sleep(0.5)
    return results


def _print_log_tail(log_path: Path, lines: int = 20) -> None:
    """Печатает хвост лог-файла упавшего парсера для быстрой диагностики."""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return
    print(f"  ┌─ хвост {log_path.name} (последние {lines} строк) " + "─" * 20)
    for line in tail:
        print(f"  │ {line}")
    print("  └" + "─" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(description="Обновить общую базу документации MP API.")
    ap.add_argument(
        "parsers",
        nargs="*",
        default=list(PARSERS.keys()),
        help=f"подмножество: {', '.join(PARSERS.keys())} (по умолчанию — все)",
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_BASE_ROOT,
        help=f"корень общей базы (по умолчанию {DEFAULT_BASE_ROOT})",
    )
    ap.add_argument("--dry-run", action="store_true", help="только показать команды")
    ap.add_argument(
        "--sequential",
        action="store_true",
        help="запускать по очереди с выводом в консоль (по умолчанию — параллельно)",
    )
    args = ap.parse_args()

    selected = [s.lower() for s in args.parsers]
    unknown = [s for s in selected if s not in PARSERS]
    if unknown:
        print(f"Неизвестные парсеры: {unknown}. Доступны: {list(PARSERS)}", file=sys.stderr)
        return 2

    if not args.root.exists():
        print(f"! Базовый путь не доступен: {args.root}", file=sys.stderr)
        print("  Проверь подключение к QNAP или передай --root <путь>", file=sys.stderr)
        return 1

    mode = "последовательно" if args.sequential else "параллельно"
    print(f"База: {args.root}")
    print(f"Парсеры: {selected}  (режим: {mode})")

    if args.sequential:
        results = [run_sequential(slug, args.root, args.dry_run) for slug in selected]
    else:
        results = run_parallel(selected, args.root, args.dry_run)

    print(f"\n{'═' * 72}\n  ИТОГИ\n{'═' * 72}")
    fail = 0
    for name, rc, elapsed in results:
        mark = "ok " if rc == 0 else "FAIL"
        print(f"  [{mark}] {name:<8}  rc={rc:<3}  {elapsed:6.1f} s")
        if rc != 0:
            fail += 1
    print(f"{'─' * 72}")
    print(f"  Всего: {len(results)}, ошибок: {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
