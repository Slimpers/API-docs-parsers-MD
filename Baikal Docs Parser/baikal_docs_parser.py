r"""Парсер документации REST API v2 «Байкал Сервис» → база MD.

Документация отдаётся одним PDF по адресу https://api.baikalsr.ru/restapi
(Word → PDF, ~60 страниц). Оглавления-закладок в PDF нет, поэтому разделы
режутся по размеру шрифта заголовков: 16pt — глава, 13pt — раздел.
Таблицы параметров собираются из строк с одинаковой Y-координатой,
JSON-примеры — по балансу скобок, и выводятся code-блоком с переотступом.

Раскладка:
    <OUT_ROOT>/NNN_<заголовок>.md   — один файл на раздел (метод)
    <OUT_ROOT>/_index.md            — оглавление со ссылками
    <OUT_ROOT>/restapi_v2.pdf       — исходный PDF

OUT_ROOT берётся из env DOCS_OUT_ROOT (общая база на QNAP), иначе ./md
рядом со скриптом. Запуск отдельно или через update_docs_bases.py (slug baikal).
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
import requests

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).parent
OUT_ROOT = Path(os.environ["DOCS_OUT_ROOT"]) if os.environ.get("DOCS_OUT_ROOT") else HERE / "md"

URL = "https://api.baikalsr.ru/restapi"

H1_SIZE = 15.5   # >= — глава («4 Справочники»)
H2_SIZE = 12.5   # >= — раздел («4.1 ФИАС»)
TOP, BOTTOM = 45, 790   # колонтитулы: номер страницы и «REST API v2 | ...»
ROW_TOL = 6      # строки с разницей Y меньше — одна строка таблицы


def _clean(t: str) -> str:
    return re.sub(r"[ \t\xa0]+", " ", t).strip()


def _slugify(title: str, idx: int) -> str:
    t = re.sub(r"[\\/:*?\"<>|«»]", "", title.strip())
    t = re.sub(r"\s+", "_", t).strip("._")[:80].rstrip("._")
    return f"{idx:03d}_{t}" if t else f"{idx:03d}_section"


def _page_lines(page: fitz.Page) -> list[dict]:
    """Непустые строки страницы без колонтитулов: {"y", "y1", "x", "size", "text"}."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            text = _clean("".join(s["text"] for s in ln["spans"]))
            x0, y0, _x1, y1 = ln["bbox"]
            if not text or y0 < TOP or y0 > BOTTOM:
                continue
            size = max(s["size"] for s in ln["spans"] if s["text"].strip())
            out.append({"y": y0, "y1": y1, "x": x0, "size": size, "text": text})
    out.sort(key=lambda r: (r["y"], r["x"]))
    return out


def _table_regions(page: fitz.Page) -> list[tuple[fitz.Rect, list[float]]]:
    """Регионы таблиц: (прямоугольник, X-границы колонок).

    Таблицы в PDF — «зебра» без рамок, find_tables видит только залитые строки.
    Фрагменты с одинаковыми X-границами, идущие подряд, склеиваем в один регион.
    """
    regions: list[list] = []
    for t in page.find_tables().tables:
        r = fitz.Rect(t.bbox)
        cols = sorted({round(c[0]) for c in t.cells if c})
        last = regions[-1] if regions else None
        if last and abs(last[0].x0 - r.x0) < 3 and abs(last[0].x1 - r.x1) < 3 and r.y0 - last[0].y1 < 60:
            last[0] |= r
            if len(cols) < len(last[1]):   # «лишние» колонки у фрагментов с объединёнными ячейками
                last[1] = cols
        else:
            regions.append([r, cols])
    return [(r, c) for r, c in regions]


def _build_table(lines: list[dict], cols: list[float]) -> list[list[str]]:
    """Строки региона → ячейки. Строка таблицы = якорь в 1-й колонке,
    остальные куски цепляются к ближайшему по Y якорю (ячейки с переносом
    отцентрированы по вертикали)."""
    def col_of(x: float) -> int:
        return max([i for i, c in enumerate(cols) if c <= x + 3] or [0])

    anchors = [ln for ln in lines if col_of(ln["x"]) == 0]
    if not anchors:
        return []
    rows = [[""] * len(cols) for _ in anchors]
    for ln in lines:
        mid = (ln["y"] + ln["y1"]) / 2
        i = min(range(len(anchors)), key=lambda k: abs((anchors[k]["y"] + anchors[k]["y1"]) / 2 - mid))
        if ln is not anchors[i] and col_of(ln["x"]) == 0:
            # перенос в первой колонке — к предыдущему якорю
            i = max(k for k, a in enumerate(anchors) if a["y"] <= ln["y"])
        c = col_of(ln["x"])
        rows[i][c] = (rows[i][c] + " " + ln["text"]).strip()
    return rows


def _rows(doc: fitz.Document) -> list[dict]:
    """Все строки документа по порядку + таблицы как отдельные ряды.

    Текст: {"page", "y", "size", "text"}; таблица: {"page", "y", "size": 0, "table": [[ячейки]]}.
    """
    out: list[dict] = []
    for page in doc:
        lines = _page_lines(page)
        items: list[dict] = []
        used: set[int] = set()
        for rect, cols in _table_regions(page):
            inside = [i for i, ln in enumerate(lines)
                      if rect.y0 - 3 <= ln["y"] <= rect.y1 and ln["x"] >= rect.x0 - 3]
            # крайние строки «зебры» могут быть незалитыми — дотягиваем вниз и вверх
            def joins(a: dict, b: dict) -> bool:   # b идёт сразу под a
                return b["y"] - a["y1"] <= 24 and b["size"] < H2_SIZE and a["size"] < H2_SIZE \
                    and not (a["text"].endswith(":") or b["text"].endswith(":"))
            while inside and inside[-1] + 1 < len(lines) and joins(lines[inside[-1]], lines[inside[-1] + 1]):
                inside.append(inside[-1] + 1)
            while inside and inside[0] > 0 and inside[0] - 1 not in used \
                    and joins(lines[inside[0] - 1], lines[inside[0]]):
                inside.insert(0, inside[0] - 1)
            table = _build_table([lines[i] for i in inside], cols)
            if table:
                used.update(inside)
                items.append({"page": page.number, "y": rect.y0, "size": 0, "table": table})
        for i, ln in enumerate(lines):
            if i not in used:
                items.append({"page": page.number, **ln})
        items.sort(key=lambda r: r["y"])
        # куски одной визуальной строки (разные блоки на одной Y) — склеиваем
        for it in items:
            prev = out[-1] if out else None
            if ("text" in it and prev and "text" in prev and prev["page"] == it["page"]
                    and abs(prev["y"] - it["y"]) < ROW_TOL):
                prev["text"] += " " + it["text"]
                prev["size"] = max(prev["size"], it["size"])
            else:
                out.append(it)
    return out


def _json_depth(text: str) -> int:
    s = re.sub(r'"[^"]*"', "", text)
    return s.count("{") + s.count("[") - s.count("}") - s.count("]")


def _table_md(rows: list[list[str]]) -> list[str]:
    def line(r: list[str]) -> str:
        return "| " + " | ".join(c.replace("|", r"\|") for c in r) + " |"
    return [line(rows[0]), "|" + "---|" * len(rows[0]), *map(line, rows[1:]), ""]


def _render(rows: list[dict]) -> list[str]:
    """Ряды одного раздела → строки markdown."""
    md: list[str] = []
    depth = 0          # > 0 — внутри JSON-примера
    code: list[str] = []
    table: list[list[str]] | None = None

    def flush_table():
        nonlocal table
        if table:
            md.extend(_table_md(table))
        table = None

    for r in rows:
        if "table" in r:
            # шапка и тело (или продолжение на след. странице) — отдельные таблицы в PDF
            if not (table and len(table[0]) == len(r["table"][0])):
                flush_table()
                table = []
            for row in r["table"]:
                if row[0].endswith(":") and not any(row[1:]):
                    # подпись между таблицами затянуло в регион — рвём таблицу
                    flush_table()
                    md += [f"**{row[0]}**", ""]
                    table = []
                else:
                    table.append(row)
            continue
        t = r["text"]

        # JSON-пример: от строки, начинающейся с { или [, до баланса скобок.
        if depth > 0 or t[:1] in "{[":
            flush_table()
            indent = max(depth - (1 if t[:1] in "}]" else 0), 0)
            code.append("  " * indent + t)
            depth += _json_depth(t)
            if depth <= 0:
                depth = 0
                md += ["```json", *code, "```", ""]
                code = []
            continue

        flush_table()
        if md[-2:] in ([t, ""], [f"**{t}**", ""]):
            continue   # Word иногда дублирует строку на стыке страниц
        if re.match(r"^(GET|POST|PUT|DELETE|PATCH)\s+\S", t):
            md += [f"`{t}`", ""]
        elif t.endswith(":"):
            md += [f"**{t}**", ""]
        elif re.match(r"^[\w.\[\]]+\s*[–-]\s", t):
            # «name – описание»: вложенность в PDF задана только отступом (шаг ~35pt)
            level = max(round((r["x"] - 36) / 35), 0)
            if md and md[-1] == "" and md[-2:-1] and md[-2].lstrip().startswith("- "):
                md.pop()   # пункты списка — без пустых строк между ними
            md += ["  " * level + f"- {t}", ""]
        else:
            md += [t, ""]

    flush_table()
    if code:
        md += ["```json", *code, "```", ""]
    return md


def parse(pdf: bytes) -> list[dict]:
    """PDF → [{"level", "title", "rows"}] по заголовкам."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    sections: list[dict] = []
    for r in _rows(doc):
        text = r.get("text", "")
        if r["size"] >= H2_SIZE and r["page"] >= 2:   # стр. 1–2 — титул и оглавление
            level = 1 if r["size"] >= H1_SIZE else 2
            prev = sections[-1] if sections else None
            # «4.1» и «ФИАС» иногда разъезжаются на две строки
            if prev and not prev["rows"] and re.fullmatch(r"[\d.]+", prev["title"]):
                prev["title"] += " " + text
                continue
            sections.append({"level": level, "title": text, "rows": []})
        elif sections:
            sections[-1]["rows"].append(r)
    return sections


def write(sections: list[dict], pdf: bytes) -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    for old in OUT_ROOT.glob("[0-9][0-9][0-9]_*.md"):
        old.unlink()
    (OUT_ROOT / "restapi_v2.pdf").write_bytes(pdf)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    index = [f"# Байкал Сервис — REST API v2", "",
             f"Источник: {URL} (PDF, копия — `restapi_v2.pdf`)  ",
             f"Обновлено: {stamp}", ""]
    n = 0
    for i, s in enumerate(sections, 1):
        body = _render(s["rows"])
        if not any(line.strip() for line in body):
            index += ["", f"## {s['title']}", ""]   # глава без текста — только группа в индексе
            continue
        name = _slugify(s["title"], i) + ".md"
        (OUT_ROOT / name).write_text(f"# {s['title']}\n\n" + "\n".join(body).rstrip() + "\n",
                                     encoding="utf-8")
        index.append(f"- [{s['title']}]({name})")
        n += 1
    (OUT_ROOT / "_index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return n


def main() -> int:
    print(f"OUT_ROOT: {OUT_ROOT}")
    resp = requests.get(URL, timeout=60, headers={"User-Agent": "Mozilla/5.0 (docs-parser)"})
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF"):
        print(f"! {URL} отдал не PDF ({resp.headers.get('Content-Type')})", file=sys.stderr)
        return 1
    sections = parse(resp.content)
    if not sections:
        print("! Не найдено ни одного раздела — поменялась вёрстка PDF?", file=sys.stderr)
        return 1
    n = write(sections, resp.content)
    print(f"[ok] Байкал Сервис: {n} разделов → {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
