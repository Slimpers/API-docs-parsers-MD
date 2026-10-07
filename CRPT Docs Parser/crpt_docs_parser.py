r"""Парсер документации ГИС МТ (Честный знак, ЦРПТ) → база MD.

Тянет статические страницы docs.crpt.ru/gismt/<Документ>/ (это НЕ redoc/SPA —
контент лежит прямо в HTML, парсится через BeautifulSoup без playwright) и
раскладывает по методам в markdown-файлы.

Документы:
    API_НК   — API Национального каталога (методы карточек: product-list,
               feed-product, short-product, подпись, субаккаунты, справочники)
    True_API — True API ГИС МТ (оборот, вывод, документы, НК-фиды)

Раскладка:
    <OUT_ROOT>/<Документ>/NNN_<slug>.md   — один файл на секцию (метод/раздел)
    <OUT_ROOT>/<Документ>/_index.md       — оглавление со ссылками

OUT_ROOT берётся из env DOCS_OUT_ROOT (для общей базы на QNAP), иначе ./md
рядом со скриптом. Запуск отдельно или через update_docs_bases.py (slug crpt).

    python crpt_docs_parser.py                  # оба документа
    python crpt_docs_parser.py API_НК           # только один
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).parent

# OUT_ROOT можно переопределить через env DOCS_OUT_ROOT (для общей базы на QNAP).
OUT_ROOT = Path(os.environ["DOCS_OUT_ROOT"]) if os.environ.get("DOCS_OUT_ROOT") else HERE / "md"

BASE = "https://docs.crpt.ru/gismt"

# Документ → URL. slug документа = ключ (используется как имя подпапки).
# Набор — API-документы ГИС МТ, релевантные обороту легпрома и карточкам НК.
# Товарно-специфичные API (табак/мех/ЭДО-Лайт) намеренно не тянем.
DOCUMENTS: dict[str, str] = {
    "True_API": f"{BASE}/True_API/",
    "API_НК": f"{BASE}/API_%D0%9D%D0%9A/",   # percent-encoding для надёжности
    "Инструкция_по_работе_с_API": f"{BASE}/Инструкция_по_работе_с_API/",
    "Получение_токена": f"{BASE}/Инструкция_по_получению_динамического_клиентского_токена/",
    "Выгрузки_True_API": f"{BASE}/Инструкция_по_формированию_выгрузок_данных_через_True_API/",
    "Отключение_устаревших_методов_True_API": f"{BASE}/Отключение_устаревших_методов_True_API/",
    "Exchange_True_API": f"{BASE}/Exchange/",
    "Анонс_API_НК": f"{BASE}/Анонс_API_НК/",
}

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (docs-parser)"})


# ---------------------------------------------------------------------------
#  HTML → Markdown (компактный конвертер фрагментов)
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    t = (text or "").replace("\x00", "").replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def _cell_md(cell: Tag) -> str:
    txt = _clean(cell.get_text(" ", strip=True))
    return txt.replace("|", "\\|").replace("\n", " ")


def _table_md(table: Tag) -> str:
    rows = table.find_all("tr")
    if not rows:
        return ""
    grid: list[list[str]] = []
    for tr in rows:
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        grid.append([_cell_md(c) for c in cells])
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    # Дедуп: адаптивная вёрстка ЦРПТ часто дублирует значение в двух соседних
    # колонках ("apikey apikey", "string string"). Схлопываем такие пары.
    def _dedup_row(r: list[str]) -> list[str]:
        out = [r[0]] if r else []
        for x in r[1:]:
            if x != out[-1]:
                out.append(x)
        return out
    grid = [_dedup_row(r) for r in grid]
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]

    header = grid[0]
    body = grid[1:]
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for r in body:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _node_md(node, depth: int = 0) -> str:
    """Рекурсивная конвертация узла в markdown."""
    if isinstance(node, NavigableString):
        return _clean(str(node))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()

    if name in ("script", "style", "nav", "svg"):
        return ""
    if name in HEADING_TAGS:
        lvl = int(name[1])
        return f"\n{'#' * lvl} {_clean(node.get_text(' ', strip=True))}\n"
    if name == "table":
        md = _table_md(node)
        return f"\n{md}\n" if md else ""
    if name in ("pre",):
        code = node.get_text("\n", strip=False).rstrip()
        return f"\n```\n{code}\n```\n"
    if name == "code" and node.parent and node.parent.name != "pre":
        return f"`{_clean(node.get_text(' ', strip=True))}`"
    if name in ("ul", "ol"):
        out = ["\n"]
        for i, li in enumerate(node.find_all("li", recursive=False), start=1):
            bullet = f"{i}." if name == "ol" else "-"
            out.append(f"{bullet} {_clean(li.get_text(' ', strip=True))}")
        out.append("")
        return "\n".join(out)
    if name in ("strong", "b"):
        t = _clean(node.get_text(" ", strip=True))
        return f"**{t}**" if t else ""
    if name in ("em", "i"):
        t = _clean(node.get_text(" ", strip=True))
        return f"*{t}*" if t else ""
    if name == "a":
        t = _clean(node.get_text(" ", strip=True))
        href = node.get("href", "")
        return f"[{t}]({href})" if href and t else t
    if name == "br":
        return "\n"
    if name in ("p", "div", "section", "article", "span", "td", "th", "tr", "tbody", "thead"):
        parts = [_node_md(c, depth + 1) for c in node.children]
        joined = "".join(parts)
        if name in ("p", "div", "section", "article"):
            joined = joined.strip()
            return f"\n{joined}\n" if joined else ""
        return joined
    # прочее — просто текст детей
    return "".join(_node_md(c, depth + 1) for c in node.children)


def _collapse(md: str) -> str:
    """Схлопывает лишние пустые строки и подряд идущие одинаковые строки (адаптив-дубли)."""
    lines = [ln.rstrip() for ln in md.replace("\x00", "").splitlines()]
    out: list[str] = []
    prev_nonempty = None
    blanks = 0
    for ln in lines:
        if not ln.strip():
            blanks += 1
            if blanks <= 1:
                out.append("")
            continue
        blanks = 0
        # дедуп подряд идущих одинаковых непустых строк (не для таблиц/кода)
        if ln == prev_nonempty and not ln.startswith(("|", "```", "-", "#")):
            continue
        out.append(ln)
        prev_nonempty = ln
    return "\n".join(out).strip() + "\n"


# ---------------------------------------------------------------------------
#  Разбор документа на секции
# ---------------------------------------------------------------------------

def _slugify(title: str, idx: int) -> str:
    t = title.strip()
    t = re.sub(r"[\\/:*?\"<>|]", "", t)          # запрещённые в именах файлов Windows
    t = re.sub(r"\s+", "_", t)
    t = t.strip("._")
    if len(t) > 80:
        t = t[:80].rstrip("._")
    return f"{idx:03d}_{t}" if t else f"{idx:03d}_section"


def _content_root(soup: BeautifulSoup) -> Tag:
    for sel in ("main", "article", "body"):
        el = soup.find(sel)
        if el:
            return el
    return soup


def parse_document(doc_slug: str, url: str) -> list[dict]:
    """Скачивает документ и режет на секции по заголовкам."""
    print(f"  GET {url}", flush=True)
    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    root = _content_root(soup)

    # Виджет отзыва «Помогите нам стать лучше» есть на каждой странице. На странице
    # без своих заголовков он был единственным заголовком, и текст страницы терялся.
    headings = [h for h in root.find_all(HEADING_TAGS)
                if _clean(h.get_text(" ", strip=True)) != "Помогите нам стать лучше"]
    if not headings:
        # Страница без заголовков (напр. одна большая таблица) — сохраняем целиком.
        body_md = _collapse(_node_md(root))
        return [{"idx": 1, "level": 1, "title": doc_slug, "body": body_md, "url": url}]

    sections: list[dict] = []
    for i, h in enumerate(headings):
        title = _clean(h.get_text(" ", strip=True))
        if not title:
            continue
        level = int(h.name[1])
        # контент секции = узлы до следующего заголовка любого уровня
        body_parts: list[str] = []
        for sib in h.next_siblings:
            if isinstance(sib, Tag) and sib.name in HEADING_TAGS:
                break
            body_parts.append(_node_md(sib))
        body_md = _collapse("".join(body_parts))
        sections.append({"idx": len(sections) + 1, "level": level, "title": title, "body": body_md, "url": url})
    return sections


def write_sections(doc_slug: str, url: str, sections: list[dict]) -> int:
    out_dir = OUT_ROOT / doc_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    # Номера секций сдвигаются, заголовки «Что нового в v.X» меняются каждый релиз —
    # без чистки старые файлы копятся рядом с новыми. Зовётся только после успешного разбора.
    for old in out_dir.glob("[0-9][0-9][0-9]_*.md"):
        old.unlink()
    today = datetime.now().strftime("%Y-%m-%d")

    index_lines = [f"# {doc_slug} — оглавление", "", f"Источник: {url}", f"Извлечено: {today}", ""]
    written = 0
    for sec in sections:
        slug = _slugify(sec["title"], sec["idx"])
        fname = f"{slug}.md"
        fpath = out_dir / fname
        front = (
            "---\n"
            f"document: {doc_slug}\n"
            f"section: \"{sec['title'].replace(chr(34), chr(39))}\"\n"
            f"level: {sec['level']}\n"
            f"source: {url}\n"
            f"extracted: {today}\n"
            "---\n\n"
        )
        heading = f"{'#' * min(sec['level'], 6)} {sec['title']}\n\n"
        fpath.write_text(front + heading + sec["body"], encoding="utf-8")
        written += 1
        indent = "  " * (sec["level"] - 1)
        index_lines.append(f"{indent}- [{sec['title']}]({fname})")

    (out_dir / "_index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return written


def main(argv: list[str]) -> int:
    selected = [a for a in argv if not a.startswith("-")] or list(DOCUMENTS.keys())
    unknown = [s for s in selected if s not in DOCUMENTS]
    if unknown:
        print(f"Неизвестные документы: {unknown}. Доступны: {list(DOCUMENTS)}", file=sys.stderr)
        return 2

    print(f"OUT_ROOT: {OUT_ROOT}")
    total = 0
    fail = 0
    for doc in selected:
        url = DOCUMENTS[doc]
        try:
            sections = parse_document(doc, url)
            if not sections:
                print(f"  ! {doc}: не найдено секций", file=sys.stderr)
                fail += 1
                continue
            n = write_sections(doc, url, sections)
            print(f"  [ok] {doc}: {n} секций → {OUT_ROOT / doc}")
            total += n
        except Exception as e:
            print(f"  [FAIL] {doc}: {type(e).__name__}: {e}", file=sys.stderr)
            fail += 1
    print(f"Итого секций: {total}, ошибок: {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
