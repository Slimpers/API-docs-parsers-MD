"""Lamoda Academy API docs parser.

Скачивает документацию API с портала `academy.lamoda.ru/articles/api/` и
раскладывает её в локальную базу markdown-файлов:

    md/_README.md                                  — глобальный индекс
    md/articles/_README.md                         — индекс разделов статей
    md/articles/<раздел>/_README.md                — индекс раздела
    md/articles/<раздел>/<статья>.md               — одна статья = один файл
    md/api/_README.md                              — индекс OpenAPI-спек
    md/api/<spec>/_README.md                       — индекс одной спеки
    md/api/<spec>/openapi.json                     — сырой OpenAPI 3
    md/api/<spec>/tags/<tag>.md                    — описание раздела (теги)
    md/api/<spec>/operations/<tag>/<METHOD путь>.md — один файл = один эндпоинт

Lamoda Academy состоит из двух частей:
  • Текстовые статьи (~20 разделов) — рендерятся через HTML→markdown.
  • Три OpenAPI-спеки (B2B Platform API, Seller Partner API, Seller Partner
    REST) — Swagger UI на странице грузит YAML; мы забираем YAML напрямую
    и рендерим его в per-endpoint md (логика как в Ozon Docs Parser).

Особенности (отличия от Ozon-парсера):
  • Сайт server-rendered (Bitrix), антибота нет — хватает `requests`,
    playwright не нужен.
  • Контент дерева обходится BFS от `/articles/api/`: страница это либо
    листинг (раздел), либо статья, либо Swagger-страница.
  • Внутренние ссылки между статьями переписываются в относительные
    md-ссылки; ссылки на нескраулённые страницы остаются абсолютными.

Использование:
    python lamoda_docs_parser.py            # всё: статьи + 3 OpenAPI-спеки
    python lamoda_docs_parser.py articles   # только статьи
    python lamoda_docs_parser.py api        # только OpenAPI-спеки
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import posixpath
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

try:
    import requests
    import yaml
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError as e:  # pragma: no cover
    sys.exit(
        f"Не хватает зависимости: {e.name}. "
        "Установите: pip install -r requirements.txt"
    )


# ───────────────────────────── константы ──────────────────────────────

BASE = "https://academy.lamoda.ru"
API_ROOT = BASE + "/articles/api/"
HERE = Path(__file__).parent
# OUT_ROOT можно переопределить через env DOCS_OUT_ROOT (для общей базы на QNAP).
OUT_ROOT = Path(os.environ["DOCS_OUT_ROOT"]) if os.environ.get("DOCS_OUT_ROOT") else HERE / "md"
OUT_ARTICLES = OUT_ROOT / "articles"
OUT_API = OUT_ROOT / "api"

REQUEST_DELAY = 0.25  # пауза между запросами, чтобы не злить сервер
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ─────────────────────────── имена файлов ─────────────────────────────

_FORBIDDEN_RE = re.compile(r'[<>:"|?*\x00-\x1f]+')


def safe_name(s: str, maxlen: int = 120) -> str:
    """Превращаем строку в безопасное для Windows имя файла, сохраняя кириллицу."""
    s = (s or "").strip()
    s = s.replace("/", "-").replace("\\", "-")
    s = _FORBIDDEN_RE.sub("_", s)
    s = re.sub(r"[-_]{2,}", "-", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .-_")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(" .-_")
    return s or "_"


def md_link(path: str) -> str:
    """URL-encode пробелы/амперсанды в md-ссылке, оставив `/` как разделитель."""
    return quote(path, safe="/#")


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение числительных. Пример: _plural_ru(2, 'тег', 'тега', 'тегов')."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _ops(n: int) -> str:
    return f"{n} {_plural_ru(n, 'операция', 'операции', 'операций')}"


def _arts(n: int) -> str:
    return f"{n} {_plural_ru(n, 'статья', 'статьи', 'статей')}"


def _natkey(s: str) -> list:
    """Ключ натуральной сортировки: '2_10' идёт после '2_2'."""
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s or "")]


# ═══════════════════════════════════════════════════════════════════════
#                       ЧАСТЬ 1. OpenAPI → markdown
#  (логика портирована из Ozon Docs Parser — OpenAPI 3 рендерится так же)
# ═══════════════════════════════════════════════════════════════════════

_CURRENT_SPEC: dict | None = None


def _resolve_ref(ref: str) -> Any:
    spec = _CURRENT_SPEC
    if not spec or not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _resolve_example(ex: Any) -> Any:
    if isinstance(ex, dict):
        if "$ref" in ex:
            target = _resolve_ref(ex["$ref"])
            if target is None:
                return None
            return _resolve_example(target)
        if "value" in ex:
            return ex["value"]
    return ex


def _resolve_top_ref(node: Any, _seen: set | None = None) -> Any:
    seen = _seen or set()
    while isinstance(node, dict) and "$ref" in node and len(seen) < 8:
        ref = node["$ref"]
        if ref in seen:
            break
        seen.add(ref)
        target = _resolve_ref(ref)
        if target is None:
            break
        extras = {k: v for k, v in node.items() if k != "$ref"}
        node = {**target, **extras}
    return node


_FORMAT_DEFAULTS: dict[str, Any] = {
    "date": "2022-01-01",
    "date-time": "2022-01-01T00:00:00Z",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "user@example.com",
    "uri": "https://example.com",
    "url": "https://example.com",
    "hostname": "example.com",
    "ipv4": "0.0.0.0",
    "byte": "U3RyaW5n",
    "binary": "string",
    "password": "string",
    "int32": 0,
    "int64": 0,
    "uint32": 0,
    "uint64": 0,
    "float": 0.0,
    "double": 0.0,
}


def _build_example_from_schema(schema: Any, depth: int = 0, _seen: set | None = None) -> Any:
    """Собираем JSON-пример из schema. inline example > enum[0] > дефолт по типу."""
    if depth > 8 or not isinstance(schema, dict):
        return None
    seen = _seen or set()
    schema = _resolve_top_ref(schema, set(seen))
    if not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]

    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for sub in schema["allOf"]:
            sub = _resolve_top_ref(sub, set(seen))
            if not isinstance(sub, dict):
                continue
            merged["properties"].update(sub.get("properties") or {})
            merged["required"].extend(sub.get("required") or [])
        if merged["properties"]:
            schema = merged

    for combiner in ("oneOf", "anyOf"):
        if combiner in schema and isinstance(schema[combiner], list) and schema[combiner]:
            v = _build_example_from_schema(schema[combiner][0], depth + 1, seen)
            if v is not None:
                return v

    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]

    t = schema.get("type")

    if t == "object" or "properties" in schema:
        out: dict[str, Any] = {}
        for name, sub in (schema.get("properties") or {}).items():
            v = _build_example_from_schema(sub, depth + 1, seen)
            if v is None:
                v = _scalar_default(sub)
            if v is not None:
                out[name] = v
        return out or None

    if t == "array" or (t is None and "items" in schema):
        items = schema.get("items") or {}
        item = _build_example_from_schema(items, depth + 1, seen)
        if item is None:
            item = _scalar_default(items)
        return [item] if item is not None else []

    return _scalar_default(schema)


def _scalar_default(schema: Any) -> Any:
    """Дефолт для скалярного OpenAPI-типа (string/integer/number/boolean)."""
    if not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        target = _resolve_ref(schema["$ref"])
        if isinstance(target, dict):
            schema = target
    if "example" in schema:
        return schema["example"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    fmt = schema.get("format") or ""
    if fmt in _FORMAT_DEFAULTS:
        return _FORMAT_DEFAULTS[fmt]
    t = schema.get("type")
    if t == "string":
        return "string"
    if t == "integer":
        return 0
    if t == "number":
        return 0
    if t == "boolean":
        return True
    return None


def _md_escape_pipe(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


_HTML_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_HTML_OPLINK_RE = re.compile(
    r'<a\s+href\s*=\s*\\?["\']?#operation/[^"\'\s>\\]+\\?["\']?[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_ASIDE_RE = re.compile(r"</?aside\b[^>]*>", re.IGNORECASE)
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")
_TRIPLE_NL_RE = re.compile(r"\n{3,}")


def _clean_doc(s: str | None) -> str:
    """Чистит HTML-обёртки в описаниях OpenAPI, не трогая полезный контент."""
    if not s:
        return ""
    s = _HTML_STYLE_RE.sub("", s)
    s = _HTML_OPLINK_RE.sub(r"\1", s)
    s = _HTML_ASIDE_RE.sub("", s)
    s = _HTML_BR_RE.sub("\n", s)
    s = _TRAILING_WS_RE.sub("\n", s)
    s = _TRIPLE_NL_RE.sub("\n\n", s)
    return s.strip()


def schema_to_brief(schema: dict | None) -> str:
    if not isinstance(schema, dict):
        return ""
    if "$ref" in schema:
        ref = schema["$ref"].rsplit("/", 1)[-1]
        return f"$ref: {ref}"
    parts = []
    t = schema.get("type")
    fmt = schema.get("format")
    if t == "array" or (t is None and "items" in schema):
        items = schema.get("items") or {}
        parts.append(f"array<{schema_to_brief(items) or 'any'}>")
    elif t is None and "properties" in schema:
        parts.append("object")
    elif t:
        parts.append(f"{t}" + (f" ({fmt})" if fmt else ""))
    if "enum" in schema:
        enum = ", ".join(json.dumps(x, ensure_ascii=False) for x in schema["enum"][:10])
        parts.append(f"enum: [{enum}]")
    if "example" in schema:
        ex = schema["example"]
        if isinstance(ex, (str, int, float, bool)):
            parts.append(f"пример: `{ex}`")
    return "; ".join(parts)


def render_schema_block(schema: dict | None, depth: int = 0, _seen: set | None = None) -> str:
    if _seen is None:
        _seen = set()
    if not isinstance(schema, dict):
        return ""
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in _seen:
            return f"{'  ' * depth}- $ref: `{ref.rsplit('/', 1)[-1]}`\n"
        _seen = _seen | {ref}
        target = _resolve_ref(ref)
        if isinstance(target, dict):
            schema = target
        else:
            return f"{'  ' * depth}- $ref: `{ref.rsplit('/', 1)[-1]}`\n"
    out = []
    t = schema.get("type")
    desc = _clean_doc(schema.get("description"))
    if desc and depth == 0:
        out.append(desc + "\n")

    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for sub in schema["allOf"]:
            sub = _resolve_top_ref(sub, set(_seen))
            if not isinstance(sub, dict):
                continue
            merged["properties"].update(sub.get("properties") or {})
            merged["required"].extend(sub.get("required") or [])
        if merged["properties"]:
            return render_schema_block(merged, depth, _seen)

    if t == "object" or "properties" in schema:
        required = set(schema.get("required") or [])
        props = schema.get("properties") or {}
        if not props:
            out.append(f"{'  ' * depth}- *(пустой object)*\n")
        for name, sub in props.items():
            req = " **(required)**" if name in required else ""
            brief = schema_to_brief(sub)
            sub_desc = (sub.get("description") if isinstance(sub, dict) else "") or ""
            sub_desc = _clean_doc(sub_desc)
            sub_desc = sub_desc.splitlines()[0] if sub_desc else ""
            line = f"{'  ' * depth}- `{name}`{req}"
            if brief:
                line += f" — {brief}"
            if sub_desc:
                line += f". {sub_desc}"
            out.append(line + "\n")
            if depth < 3 and isinstance(sub, dict):
                if sub.get("type") == "object" or "properties" in sub:
                    out.append(render_schema_block(sub, depth + 1, _seen))
                elif sub.get("type") == "array" or (sub.get("type") is None and "items" in sub):
                    items = sub.get("items") or {}
                    items_resolved = (
                        _resolve_top_ref(items, set(_seen))
                        if isinstance(items, dict) else items
                    )
                    if isinstance(items_resolved, dict) and (
                        items_resolved.get("type") == "object"
                        or "properties" in items_resolved
                    ):
                        out.append(f"{'  ' * (depth + 1)}- *(элементы)*\n")
                        out.append(render_schema_block(items, depth + 2, _seen))
    elif t == "array" or (t is None and "items" in schema):
        items = schema.get("items") or {}
        out.append(f"{'  ' * depth}- array of: {schema_to_brief(items) or 'any'}\n")
        items_resolved = _resolve_top_ref(items, set(_seen)) if isinstance(items, dict) else items
        if isinstance(items_resolved, dict) and (
            items_resolved.get("type") == "object" or "properties" in items_resolved
        ):
            out.append(render_schema_block(items, depth + 1, _seen))
    else:
        brief = schema_to_brief(schema)
        if brief:
            out.append(f"{'  ' * depth}- {brief}\n")
    return "".join(out)


def render_parameters(params: list[dict]) -> str:
    if not params:
        return ""
    rows = ["| Имя | В | Тип | Обязательный | Описание |", "|---|---|---|---|---|"]
    for p in params:
        p = _resolve_top_ref(p)
        if not isinstance(p, dict):
            continue
        sch = p.get("schema") or {}
        rows.append(
            "| `{name}` | {loc} | {t} | {req} | {desc} |".format(
                name=_md_escape_pipe(p.get("name", "")),
                loc=p.get("in", ""),
                t=_md_escape_pipe(schema_to_brief(sch)),
                req="да" if p.get("required") else "нет",
                desc=_md_escape_pipe(_clean_doc(p.get("description", ""))),
            )
        )
    return "\n".join(rows) + "\n"


def render_request_body(body: dict | None) -> str:
    if not body:
        return ""
    body = _resolve_top_ref(body)
    out = ["### Тело запроса\n"]
    if body.get("required"):
        out.append("*Обязательное.*\n")
    desc = _clean_doc(body.get("description"))
    if desc:
        out.append(desc + "\n")
    content = body.get("content") or {}
    for ct, payload in content.items():
        out.append(f"\n**Content-Type:** `{ct}`\n\n")
        schema = payload.get("schema") if isinstance(payload, dict) else None
        out.append(render_schema_block(schema))
        example = payload.get("example") if isinstance(payload, dict) else None
        examples = payload.get("examples") if isinstance(payload, dict) else None
        if example is not None:
            out.append("\n**Пример:**\n\n```json\n")
            out.append(json.dumps(example, ensure_ascii=False, indent=2))
            out.append("\n```\n")
        elif examples:
            for ex_name, ex in examples.items():
                val = _resolve_example(ex)
                out.append(f"\n**Пример «{ex_name}»:**\n\n```json\n")
                out.append(json.dumps(val, ensure_ascii=False, indent=2))
                out.append("\n```\n")
        else:
            synth = _build_example_from_schema(schema)
            if synth is not None and synth != {}:
                out.append("\n**Пример:**\n\n```json\n")
                out.append(json.dumps(synth, ensure_ascii=False, indent=2))
                out.append("\n```\n")
    return "".join(out)


def render_responses(responses: dict) -> str:
    if not responses:
        return ""
    out = []
    for code, resp in responses.items():
        if not isinstance(resp, dict):
            continue
        local_desc = resp.get("description")
        merged = _resolve_top_ref(resp)
        if not isinstance(merged, dict):
            merged = resp
        desc = local_desc or merged.get("description") or ""
        desc = _clean_doc(desc)

        out.append(f"\n#### {code}")
        if desc:
            out.append(f" — {desc}")
        out.append("\n\n")

        content = merged.get("content") or {}
        for ct, payload in content.items():
            out.append(f"**Content-Type:** `{ct}`\n\n")
            schema = payload.get("schema") if isinstance(payload, dict) else None
            out.append(render_schema_block(schema))
            example = payload.get("example") if isinstance(payload, dict) else None
            examples = payload.get("examples") if isinstance(payload, dict) else None
            if example is not None:
                out.append("\n```json\n")
                out.append(json.dumps(example, ensure_ascii=False, indent=2))
                out.append("\n```\n")
            elif examples:
                for ex_name, ex in examples.items():
                    val = _resolve_example(ex)
                    out.append(f"\n*{ex_name}:*\n\n```json\n")
                    out.append(json.dumps(val, ensure_ascii=False, indent=2))
                    out.append("\n```\n")
            else:
                synth = _build_example_from_schema(schema)
                if synth is not None and synth != {}:
                    out.append("\n```json\n")
                    out.append(json.dumps(synth, ensure_ascii=False, indent=2))
                    out.append("\n```\n")
    return "".join(out)


def render_operation_md(method: str, path: str, op: dict, *, server: str = "") -> str:
    summary = _clean_doc(op.get("summary") or "")
    description = _clean_doc(op.get("description") or "")
    op_id = op.get("operationId") or ""
    tags = op.get("tags") or []
    deprecated = op.get("deprecated", False)

    out = []
    out.append(f"# {method.upper()} {path}\n")
    if summary:
        out.append(f"\n**{summary}**\n")
    if deprecated:
        out.append("\n> ⚠️ DEPRECATED\n")
    if op_id or tags:
        meta = []
        if op_id:
            meta.append(f"`operationId`: {op_id}")
        if tags:
            meta.append("теги: " + ", ".join(f"`{t}`" for t in tags))
        out.append("\n" + "  \n".join(meta) + "\n")
    if server:
        out.append(f"\n**Базовый URL:** `{server}`\n")
    full = server.rstrip("/") + path if server else path
    out.append(f"\n**Полный путь:** `{method.upper()} {full}`\n")
    if description:
        out.append("\n## Описание\n\n")
        out.append(description + "\n")

    sec = op.get("security")
    if sec is not None:
        out.append("\n## Авторизация\n\n")
        if not sec:
            out.append("Без авторизации.\n")
        else:
            for s in sec:
                if isinstance(s, dict):
                    for name, scopes in s.items():
                        sc = ", ".join(scopes) if scopes else "—"
                        out.append(f"- `{name}` (scopes: {sc})\n")

    params = op.get("parameters") or []
    if params:
        out.append("\n## Параметры\n\n")
        out.append(render_parameters(params))

    rb = op.get("requestBody")
    if rb:
        out.append("\n## Запрос\n\n")
        out.append(render_request_body(rb))

    responses = op.get("responses") or {}
    if responses:
        out.append("\n## Ответы\n\n")
        out.append(render_responses(responses))

    return "".join(out)


def render_tag_md(tag: dict) -> str:
    name = tag.get("name") or "—"
    desc = _clean_doc(tag.get("description"))
    out = [f"# {name}\n"]
    if desc:
        out.append("\n" + desc + "\n")
    return "".join(out)


def _anchor(prefix: str, name: str) -> str:
    """HTML-якорь, безопасный для GitHub-markdown."""
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", (name or "")).strip("-").lower()
    return f"{prefix}-{s}" if s else prefix


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_HEADER_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)


def _first_sentence(text: str | None, maxlen: int = 200) -> str:
    """Берём первое содержательное предложение из markdown-описания."""
    if not text:
        return ""
    cleaned = _MD_HEADER_RE.sub("", text)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    line = ""
    for raw in cleaned.splitlines():
        s = raw.strip()
        if s:
            line = s
            break
    if not line:
        return ""
    m = re.search(r"^(.+?[.!?])\s", line)
    s = (m.group(1) if m else line).strip()
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(" ,;:") + "…"
    return s


def render_spec_index_md(slug: str, info: dict, servers: list[dict],
                         tags: list[dict], paths: dict, tag_groups: list[dict],
                         op_files: dict[str, list[dict]], source_url: str) -> str:
    """Навигационный индекс одной OpenAPI-спеки."""
    title = info.get("title") or slug
    version = info.get("version") or ""
    description = _clean_doc(info.get("description") or "")

    tag_desc = {t.get("name"): (t.get("description") or "") for t in tags if t.get("name")}

    grouped_tags: set[str] = set()
    for g in tag_groups:
        for tn in g.get("tags") or []:
            grouped_tags.add(tn)
    orphan_tags = [t for t in op_files if t not in grouped_tags]

    total_ops = sum(len(v) for v in op_files.values())
    server_url = ""
    if servers:
        server_url = (servers[0] or {}).get("url") or ""
        if server_url.startswith("//"):
            server_url = "https:" + server_url

    out: list[str] = []
    out.append(f"# {title}\n")
    out.append(f"\n**Спека:** `{slug}` — версия `{version}`\n")
    if server_url:
        out.append(f"\n**Базовый URL:** `{server_url}`\n")
    if source_url:
        out.append(f"\n**Источник:** [{source_url}]({source_url})\n")
    n_tags = len(op_files)
    n_paths = len(paths)
    out.append(
        f"\n**Сводка:** {_ops(total_ops)} · "
        f"{n_tags} {_plural_ru(n_tags, 'тег', 'тега', 'тегов')} · "
        f"{n_paths} {_plural_ru(n_paths, 'путь', 'пути', 'путей')}.\n"
    )
    out.append(
        "\n> **Как ориентироваться:** см. [Содержание](#содержание) ниже → "
        "выбираете раздел (тег) → ссылку на эндпоинт. Файлы эндпоинтов лежат "
        "в `operations/<TagName>/<METHOD путь>.md`.\n"
    )
    if description:
        out.append("\n" + description + "\n")

    if servers and len(servers) > 1:
        out.append("\n## Серверы\n")
        for s in servers:
            url = s.get("url") or ""
            if url.startswith("//"):
                url = "https:" + url
            d = _clean_doc(s.get("description") or "")
            out.append(f"- `{url}`" + (f" — {d}" if d else "") + "\n")

    out.append("\n## Содержание\n")
    if tag_groups:
        for g in tag_groups:
            gname = g.get("name") or "—"
            n = sum(len(op_files.get(tn) or []) for tn in (g.get("tags") or []))
            out.append(f"- [{gname}](#{_anchor('group', gname)}) — {_ops(n)}\n")
    if orphan_tags:
        for tn in sorted(orphan_tags, key=lambda s: s.lower()):
            out.append(f"- [{tn}](#{_anchor('tag', tn)}) — {_ops(len(op_files[tn]))}\n")

    def _emit_tag(tname: str) -> None:
        ops = op_files.get(tname) or []
        out.append(f'\n<a id="{_anchor("tag", tname)}"></a>\n')
        out.append(f"### {tname} — {_ops(len(ops))}\n")
        link_tag = md_link(safe_name(tname))
        out.append(f"\n*Полная карточка:* [tags/{tname}.md](tags/{link_tag}.md)\n")
        desc = _first_sentence(tag_desc.get(tname, ""), maxlen=400)
        if desc:
            out.append(f"\n{desc}\n")
        if not ops:
            out.append("\n*Нет операций.*\n")
            return
        out.append("\n")
        for op in sorted(ops, key=lambda x: (x["path"], x["method"])):
            method = op["method"].upper()
            summary = op.get("summary") or ""
            dep = " ⚠️ deprecated" if op.get("deprecated") else ""
            line = f"- [`{method} {op['path']}`]({md_link(op['rel'])})"
            if summary:
                line += f" — {summary}"
            out.append(line + dep + "\n")

    if tag_groups:
        for g in tag_groups:
            gname = g.get("name") or "—"
            tnames = g.get("tags") or []
            n = sum(len(op_files.get(tn) or []) for tn in tnames)
            out.append(f'\n<a id="{_anchor("group", gname)}"></a>\n')
            out.append(f"## {gname} — {_ops(n)}\n")
            for tn in tnames:
                if tn in op_files:
                    _emit_tag(tn)

    if orphan_tags:
        for tn in sorted(orphan_tags, key=lambda s: s.lower()):
            _emit_tag(tn)

    return "".join(out)


def _op_filename(method: str, path: str) -> str:
    return safe_name(method.upper() + " " + path.lstrip("/").replace("/", "-")) + ".md"


def build_openapi(spec: dict, slug: str, source_url: str = "") -> int:
    """Раскладывает один OpenAPI-документ в md/api/<slug>/."""
    global _CURRENT_SPEC
    if not isinstance(spec, dict) or "paths" not in spec:
        print(f"[la] нет paths в спеке {slug}")
        return 0
    _CURRENT_SPEC = spec

    info = spec.get("info") or {}
    servers = spec.get("servers") or []
    tags = spec.get("tags") or []
    tag_groups = spec.get("x-tagGroups") or []
    paths = spec.get("paths") or {}

    base_dir = OUT_API / slug
    (base_dir / "tags").mkdir(parents=True, exist_ok=True)
    (base_dir / "operations").mkdir(parents=True, exist_ok=True)

    (base_dir / "openapi.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    tag_def: dict[str, dict] = {t.get("name"): t for t in tags if t.get("name")}

    server_url = ""
    if servers and isinstance(servers, list):
        server_url = (servers[0] or {}).get("url") or ""
    if server_url.startswith("//"):
        server_url = "https:" + server_url

    op_files: dict[str, list[dict]] = {}
    written = 0
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch", "options", "head"):
                continue
            if not isinstance(op, dict):
                continue
            md = render_operation_md(method, path, op, server=server_url)
            tag_name = (op.get("tags") or ["_misc"])[0]
            tag_dir = safe_name(tag_name)
            (base_dir / "operations" / tag_dir).mkdir(parents=True, exist_ok=True)
            fname = _op_filename(method, path)
            (base_dir / "operations" / tag_dir / fname).write_text(md, encoding="utf-8")
            op_files.setdefault(tag_name, []).append({
                "method": method,
                "path": path,
                "rel": f"operations/{tag_dir}/{fname}",
                "summary": _clean_doc(op.get("summary") or ""),
                "op_id": op.get("operationId") or "",
                "deprecated": bool(op.get("deprecated")),
            })
            written += 1

    # карточка тега для каждого тега — описанного в spec.tags и встреченного
    # только в операциях (orphan): иначе ссылки из индекса ведут в никуда
    for tname in sorted(set(op_files) | set(tag_def)):
        t = tag_def.get(tname) or {"name": tname}
        (base_dir / "tags" / (safe_name(tname) + ".md")).write_text(
            render_tag_md(t), encoding="utf-8"
        )

    (base_dir / "_README.md").write_text(
        render_spec_index_md(slug, info, servers, tags, paths, tag_groups,
                             op_files, source_url),
        encoding="utf-8",
    )
    print(f"[la] спека {slug}: {written} эндпоинтов, {len(op_files)} тегов")
    return written


# ═══════════════════════════════════════════════════════════════════════
#                       ЧАСТЬ 2. Загрузка страниц
# ═══════════════════════════════════════════════════════════════════════

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
})


def fetch_text(url: str, tries: int = 3) -> str | None:
    """GET с ретраями. Возвращает HTML/текст или None."""
    for attempt in range(tries):
        try:
            r = _SESSION.get(url, timeout=45)
            if r.status_code == 200:
                r.encoding = "utf-8"
                time.sleep(REQUEST_DELAY)
                return r.text
            print(f"[la] {url} -> HTTP {r.status_code}")
        except Exception as e:
            print(f"[la] {url} ошибка ({attempt + 1}/{tries}): {e}")
        time.sleep(2)
    return None


# ═══════════════════════════════════════════════════════════════════════
#                  ЧАСТЬ 3. HTML-статья → markdown
# ═══════════════════════════════════════════════════════════════════════

_HEADING_LEVEL = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_INLINE_TAGS = {
    "a", "b", "strong", "i", "em", "code", "span", "br", "img",
    "sub", "sup", "u", "s", "mark", "abbr", "small", "kbd", "var",
}
_SKIP_TAGS = {"script", "style", "svg", "noscript", "button", "iframe"}


def _abs_url(src: str) -> str:
    """Делает ссылку абсолютной относительно academy.lamoda.ru."""
    src = (src or "").strip()
    if not src:
        return ""
    if src.startswith("//"):
        return "https:" + src
    if src.startswith(("http://", "https://")):
        return src
    if src.startswith("/"):
        return BASE + src
    return BASE + "/" + src


def _rewrite_link(href: str, ctx: dict) -> str:
    """Внутренние ссылки → относительные md-пути; остальное → абсолютный URL."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return href or "#"

    frag = ""
    if "#" in href:
        href, frag = href.split("#", 1)
        frag = "#" + frag
    if not href:
        return frag or "#"

    # резолвим ссылку (в т.ч. относительную ../) от URL текущей страницы
    abs_url = urljoin(ctx.get("page_url") or BASE, href)
    pr = urlparse(abs_url)
    if pr.netloc and pr.netloc.lower() != "academy.lamoda.ru":
        return abs_url + frag  # внешний ресурс — оставляем как есть

    path = pr.path or "/"
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if not path.endswith("/") and "." not in last:
        path += "/"

    target = ctx["url_map"].get(path)
    if target:
        rel = posixpath.relpath(target, ctx["md_dir"] or ".")
        return md_link(rel) + frag
    return BASE + path + frag


def _guess_lang(code: str) -> str:
    c = code.lstrip()
    head = c[:60]
    if c.startswith("curl") or "curl -" in head:
        return "bash"
    if c.startswith(("{", "[")):
        return "json"
    if c.startswith("<?xml") or (c.startswith("<") and "</" in c):
        return "xml"
    if re.match(r"(import |from \w|def |class |print\()", c):
        return "python"
    if re.match(r"(GET|POST|PUT|DELETE|PATCH|HEAD) /", c):
        return "http"
    if c.startswith(("$ ", "#!/", "export ")):
        return "bash"
    return ""


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "")


def inline_md(node, ctx: dict) -> str:
    """Рендер inline-содержимого узла (без блочной структуры)."""
    if isinstance(node, NavigableString):
        return _collapse_ws(str(node))
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in _SKIP_TAGS:
        return ""
    if name == "br":
        return "\n"
    if name == "img":
        src = node.get("src") or node.get("data-src") or ""
        alt = (node.get("alt") or "").strip()
        url = _abs_url(src)
        return f"![{alt}]({url})" if url else ""

    inner = "".join(inline_md(c, ctx) for c in node.children)

    if name in ("b", "strong"):
        t = inner.strip()
        return f"**{t}**" if t else ""
    if name in ("i", "em", "var"):
        t = inner.strip()
        return f"*{t}*" if t else ""
    if name in ("s",):
        t = inner.strip()
        return f"~~{t}~~" if t else ""
    if name in ("code", "kbd"):
        t = _collapse_ws(node.get_text()).strip()
        if not t:
            return ""
        return f"`` {t} ``" if "`" in t else f"`{t}`"
    if name == "a":
        href = node.get("href") or ""
        text = inner.strip() or href.strip()
        if not href:
            return text
        return f"[{text}]({_rewrite_link(href, ctx)})"
    if name == "sub":
        return f"<sub>{inner.strip()}</sub>"
    if name == "sup":
        return f"<sup>{inner.strip()}</sup>"
    return inner


def _table_md(table: Tag, ctx: dict) -> str:
    body = table.find("tbody") or table
    trs = body.find_all("tr", recursive=False) or table.find_all("tr")
    rows: list[list[str]] = []
    for tr in trs:
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        row = []
        for c in cells:
            txt = inline_md(c, ctx).replace("\n", " ").replace("|", "\\|")
            row.append(_collapse_ws(txt).strip() or " ")
        rows.append(row)
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    rows = [r + [" "] * (ncol - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join(["---"] * ncol) + " |"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines) + "\n\n"


def _list_md(node: Tag, ctx: dict, depth: int) -> str:
    ordered = node.name.lower() == "ol"
    indent = "  " * depth
    out: list[str] = []
    n = 0
    for li in node.find_all("li", recursive=False):
        n += 1
        marker = f"{n}. " if ordered else "- "
        inline_parts: list[str] = []
        nested: list[Tag] = []
        for c in li.children:
            if isinstance(c, Tag) and c.name and c.name.lower() in ("ul", "ol"):
                nested.append(c)
            else:
                inline_parts.append(inline_md(c, ctx))
        text = _collapse_ws(" ".join(inline_parts)).strip()
        out.append(f"{indent}{marker}{text}")
        for nl in nested:
            out.append(_list_md(nl, ctx, depth + 1).rstrip("\n"))
    return "\n".join(out) + "\n"


def block_md(node, ctx: dict, depth: int = 0) -> str:
    """Рендер блочного узла в markdown."""
    if isinstance(node, NavigableString):
        s = _collapse_ws(str(node))
        return s if s.strip() else ""
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in _SKIP_TAGS:
        return ""
    if name in _HEADING_LEVEL:
        txt = inline_md(node, ctx).strip()
        return f"\n{'#' * _HEADING_LEVEL[name]} {txt}\n\n" if txt else ""
    if name == "p":
        txt = inline_md(node, ctx).strip()
        return txt + "\n\n" if txt else ""
    if name == "pre":
        code = node.get_text().replace("\r\n", "\n").strip("\n")
        if not code.strip():
            return ""
        return f"```{_guess_lang(code)}\n{code}\n```\n\n"
    if name == "blockquote":
        inner = "".join(block_md(c, ctx, depth) for c in node.children).strip()
        if not inner:
            return ""
        quoted = "\n".join(("> " + ln) if ln else ">" for ln in inner.split("\n"))
        return quoted + "\n\n"
    if name in ("ul", "ol"):
        return _list_md(node, ctx, depth) + "\n"
    if name == "table":
        return _table_md(node, ctx)
    if name == "hr":
        return "\n---\n\n"
    if name == "br":
        return "\n"
    if name in _INLINE_TAGS:
        txt = inline_md(node, ctx).strip()
        return txt + "\n\n" if txt else ""
    # div / section / article / tbody / details ... — раскрываем детей
    return "".join(block_md(c, ctx, depth) for c in node.children)


def html_to_md(container: Tag, ctx: dict) -> str:
    """Конвертирует поддерево статьи (div.article-detail__text) в markdown."""
    parts = [block_md(c, ctx) for c in container.children]
    md = "".join(parts)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# ═══════════════════════════════════════════════════════════════════════
#                  ЧАСТЬ 4. Обход дерева /articles/api/
# ═══════════════════════════════════════════════════════════════════════

_YAML_URL_RE = re.compile(r"""url:\s*['"]([^'"]+\.ya?ml)['"]""")


def _norm_url(href: str, base: str) -> str:
    """Абсолютный URL без фрагмента."""
    u = urljoin(base, (href or "").strip())
    return u.split("#")[0]


def _path_key(url: str) -> str:
    """Ключ страницы: путь со слешом на конце, без query/fragment."""
    p = urlparse(url).path
    if not p.endswith("/"):
        p += "/"
    return p


def extract_children(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Достаёт (заголовок, href) дочерних страниц из листинга раздела."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    patterns = [
        ("div.subsections-article-item",
         ".subsections-article-item__title",
         "a.subsections-article-item__detail-link"),
        ("div.current-article-sections-subsection",
         ".current-article-sections-subsection__title",
         "a"),
    ]
    for box_sel, title_sel, link_sel in patterns:
        for box in soup.select(box_sel):
            a = box.select_one(link_sel) or box.find("a", href=True)
            if not a or not a.get("href"):
                continue
            href = a["href"].split("#")[0]
            if href in seen:
                continue
            seen.add(href)
            t = box.select_one(title_sel)
            title = (t.get_text(" ", strip=True) if t else "") or \
                    a.get_text(" ", strip=True)
            out.append((title, href))
    return out


def children_from_nav(soup: BeautifulSoup, url: str) -> list[tuple[str, str]]:
    """Запасной способ: дочерние страницы из бокового меню.

    Архивный раздел («Статьи для версии v1») листинг-блоков не содержит, его
    подразделы висят только в sidebar. Берём из меню ссылки ровно на один
    уровень глубже текущего пути — иначе в «дети» попадёт всё дерево.
    """
    base = _path_key(url)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").split("#")[0]
        if not href:
            continue
        path = _path_key(_norm_url(href, url))
        if not path.startswith(base) or path == base:
            continue
        if path[len(base):].strip("/").count("/"):
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append((a.get_text(" ", strip=True), href))
    return out


def _page_h1(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def crawl() -> dict:
    """BFS-обход /articles/api/. Возвращает словарь со статьями/разделами/спеками."""
    articles: dict[str, dict] = {}   # url -> {title, body_soup, h1}
    listings: dict[str, dict] = {}   # url -> {title, children:[(t,url)]}
    specs: list[dict] = []           # [{url, slug, yaml_url, title}]
    nav_title: dict[str, str] = {}   # url -> заголовок из родительского листинга

    queue: deque[str] = deque([API_ROOT])
    visited: set[str] = set()

    print(f"[la] обход дерева от {API_ROOT}")
    while queue:
        url = queue.popleft()
        key = url.split("#")[0]
        if key in visited:
            continue
        visited.add(key)

        html = fetch_text(url)
        if html is None:
            print(f"[la] пропуск (не загрузилось): {url}")
            continue

        # 1. Swagger-страница со спекой
        m = _YAML_URL_RE.search(html)
        if m and "swagger-ui" in html:
            yaml_url = _norm_url(m.group(1), url)
            slug = safe_name(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
            specs.append({"url": url, "slug": slug, "yaml_url": yaml_url,
                          "title": nav_title.get(url, slug)})
            print(f"[la]   спека: {slug} ({yaml_url})")
            continue

        soup = BeautifulSoup(html, "html.parser")
        art = soup.select_one("div.article-detail__text")
        children = extract_children(soup)
        if not children and (art is None or len(art.get_text(strip=True)) <= 40):
            children = children_from_nav(soup, url)

        # 2. Статья
        if art is not None and len(art.get_text(strip=True)) > 40 and not children:
            h1 = art.find("h1")
            title = (h1.get_text(" ", strip=True) if h1 else "") or \
                    _page_h1(soup) or nav_title.get(url, "")
            if h1:
                h1.extract()
            articles[url] = {"title": title or url, "body": art}
            print(f"[la]   статья: {title}")
            continue

        # 3. Листинг (раздел)
        if children:
            listings[url] = {
                "title": _page_h1(soup) or nav_title.get(url, ""),
                "children": children,
            }
            # необязательный вводный текст раздела
            listings[url]["intro"] = art
            for ctitle, chref in children:
                curl = _norm_url(chref, url)
                nav_title.setdefault(curl, ctitle)
                if curl.split("#")[0] not in visited:
                    queue.append(curl)
            # пагинация Bitrix (?PAGEN_1=N)
            for a in soup.select("a[href]"):
                h = a["href"]
                if "PAGEN_" in h:
                    purl = _norm_url(h, url)
                    if purl.split("#")[0] not in visited:
                        queue.append(purl)
            print(f"[la]   раздел: {listings[url]['title']} "
                  f"({len(children)} дочерних)")
            continue

        print(f"[la]   ? неопознанная страница: {url}")

    print(f"[la] обход завершён: {len(listings)} разделов, "
          f"{len(articles)} статей, {len(specs)} спек")
    return {"articles": articles, "listings": listings,
            "specs": specs, "nav_title": nav_title}


# ═══════════════════════════════════════════════════════════════════════
#                  ЧАСТЬ 5. Раскладка статей в md
# ═══════════════════════════════════════════════════════════════════════

def _api_rest(url: str) -> str | None:
    """Часть пути после /articles/api/ . None если URL вне дерева."""
    p = urlparse(url).path
    prefix = "/articles/api/"
    if not p.startswith(prefix):
        return None
    return p[len(prefix):].strip("/")


def article_paths(url: str) -> tuple[str, str, str, str]:
    """url статьи -> (section_folder, file_slug, md_rel, section_url)."""
    rest = _api_rest(url) or ""
    segs = [s for s in rest.split("/") if s]
    if len(segs) >= 2:
        folder = "/".join(safe_name(s) for s in segs[:-1])
        slug = safe_name(segs[-1])
        md_rel = f"articles/{folder}/{slug}.md"
        section_url = API_ROOT + "/".join(segs[:-1]) + "/"
    else:
        folder = ""
        slug = safe_name(segs[-1]) if segs else "index"
        md_rel = f"articles/{slug}.md"
        section_url = API_ROOT
    return folder, slug, md_rel, section_url


def section_md_rel(section_url: str) -> str:
    """url раздела -> относительный путь его _README.md."""
    rest = _api_rest(section_url) or ""
    if rest:
        folder = "/".join(safe_name(s) for s in rest.split("/") if s)
        return f"articles/{folder}/_README.md"
    return "articles/_README.md"


def build_url_map(data: dict, spec_built: dict[str, str]) -> dict[str, str]:
    """path-ключ -> относительный md-путь (для переписывания внутренних ссылок)."""
    url_map: dict[str, str] = {_path_key(API_ROOT): "articles/_README.md"}
    # статьи
    section_urls: set[str] = set()
    for url in data["articles"]:
        _, _, md_rel, section_url = article_paths(url)
        url_map[_path_key(url)] = md_rel
        section_urls.add(section_url)
    # разделы (только те, где реально есть статьи)
    for section_url in section_urls:
        url_map[_path_key(section_url)] = section_md_rel(section_url)
    # спеки
    for spec_url, slug in spec_built.items():
        url_map[_path_key(spec_url)] = f"api/{slug}/_README.md"
    return url_map


def render_article_md(url: str, art: dict, section_title: str,
                       url_map: dict[str, str]) -> str:
    _, _, md_rel, _ = article_paths(url)
    md_dir = posixpath.dirname(md_rel)
    ctx = {"url_map": url_map, "md_dir": md_dir, "page_url": url}

    title = art["title"]
    body = html_to_md(art["body"], ctx)

    out = [f"# {title}\n"]
    meta = []
    if section_title:
        sec_rel = posixpath.relpath(
            section_md_rel(article_paths(url)[3]), md_dir or ".")
        meta.append(f"**Раздел:** [{section_title}]({md_link(sec_rel)})")
    meta.append(f"**Источник:** [{url}]({url})")
    out.append("\n" + " · ".join(meta) + "\n")
    out.append("\n---\n\n")
    out.append(body + "\n")
    return "".join(out)


def render_section_index(section_url: str, title: str,
                         arts: list[dict]) -> str:
    out = [f"# {title}\n"]
    out.append(f"\nРаздел документации Lamoda Academy API. {_arts(len(arts))}.\n\n")
    for i, a in enumerate(sorted(arts, key=lambda x: _natkey(x["slug"])), 1):
        out.append(f"{i}. [{a['title']}]({md_link(a['slug'])}.md)\n")
    return "".join(out)


def render_articles_index(sections: list[dict]) -> str:
    total_arts = sum(len(s["articles"]) for s in sections)
    out = ["# Lamoda Academy — статьи по API\n"]
    out.append(
        f"\nЛокальная md-база статей документации `academy.lamoda.ru/articles/api/`. "
        f"{len(sections)} {_plural_ru(len(sections), 'раздел', 'раздела', 'разделов')} · "
        f"{_arts(total_arts)}.\n"
    )
    out.append("\n## Разделы\n\n")
    for s in sections:
        out.append(f"- [{s['title']}]({md_link(s['folder'])}/_README.md) — "
                   f"{_arts(len(s['articles']))}\n")
    for s in sections:
        out.append(f"\n### {s['title']}\n\n")
        for a in sorted(s["articles"], key=lambda x: _natkey(x["slug"])):
            rel = f"{s['folder']}/{a['slug']}.md"
            out.append(f"- [{a['title']}]({md_link(rel)})\n")
    return "".join(out)


def build_articles(data: dict, url_map: dict[str, str]) -> list[dict]:
    """Пишет статьи и индексы разделов. Возвращает список разделов."""
    OUT_ARTICLES.mkdir(parents=True, exist_ok=True)

    # группируем статьи по разделам (папка из URL)
    by_section: dict[str, dict] = {}
    for url, art in data["articles"].items():
        folder, slug, md_rel, section_url = article_paths(url)
        sec = by_section.setdefault(section_url, {
            "folder": folder, "url": section_url, "articles": [],
        })
        sec["articles"].append({"url": url, "slug": slug, "title": art["title"]})

    sections: list[dict] = []
    for section_url, sec in by_section.items():
        listing = data["listings"].get(section_url, {})
        title = (listing.get("title")
                 or data["nav_title"].get(section_url)
                 or sec["folder"])
        sec["title"] = title
        sections.append(sec)
    sections.sort(key=lambda s: _natkey(s["folder"]))

    written = 0
    for sec in sections:
        sec_dir = OUT_ARTICLES / sec["folder"] if sec["folder"] else OUT_ARTICLES
        sec_dir.mkdir(parents=True, exist_ok=True)
        for a in sec["articles"]:
            art = data["articles"][a["url"]]
            md = render_article_md(a["url"], art, sec["title"], url_map)
            (sec_dir / f"{a['slug']}.md").write_text(md, encoding="utf-8")
            written += 1
        (sec_dir / "_README.md").write_text(
            render_section_index(sec["url"], sec["title"], sec["articles"]),
            encoding="utf-8",
        )

    OUT_ARTICLES.mkdir(parents=True, exist_ok=True)
    (OUT_ARTICLES / "_README.md").write_text(
        render_articles_index(sections), encoding="utf-8"
    )
    print(f"[la] статьи: {written} файлов в {len(sections)} разделах")
    return sections


# ═══════════════════════════════════════════════════════════════════════
#                  ЧАСТЬ 6. OpenAPI-спеки + корневые индексы
# ═══════════════════════════════════════════════════════════════════════

def _yaml_to_jsonable(obj: Any) -> Any:
    """YAML-загрузка может вернуть datetime/date — приводим к JSON-совместимым типам.

    Ключи словарей тоже нормализуем в строки (в YAML коды ответов вроде `200:`
    парсятся как int, а ISO-даты — как datetime).
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str):
                key = k
            elif isinstance(k, (_dt.datetime, _dt.date, _dt.time)):
                key = k.isoformat()
            elif isinstance(k, bool):
                key = "true" if k else "false"
            else:
                key = str(k)
            out[key] = _yaml_to_jsonable(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_yaml_to_jsonable(v) for v in obj]
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    return obj


def build_specs(data: dict) -> dict[str, dict]:
    """Скачивает YAML каждой спеки и раскладывает в md/api/<slug>/."""
    if not data["specs"]:
        return {}
    OUT_API.mkdir(parents=True, exist_ok=True)
    built: dict[str, dict] = {}
    for sp in data["specs"]:
        print(f"[la] спека {sp['slug']}: качаю {sp['yaml_url']}")
        raw = fetch_text(sp["yaml_url"])
        if raw is None:
            print(f"[la] спека {sp['slug']}: YAML не загрузился — пропуск")
            continue
        try:
            spec = yaml.safe_load(raw)
        except Exception as e:
            print(f"[la] спека {sp['slug']}: ошибка YAML — {e}")
            continue
        if not isinstance(spec, dict):
            print(f"[la] спека {sp['slug']}: не похоже на OpenAPI — пропуск")
            continue
        spec = _yaml_to_jsonable(spec)
        n = build_openapi(spec, sp["slug"], source_url=sp["url"])
        built[sp["slug"]] = {
            "spec": spec, "url": sp["url"], "ops": n,
            "title": (spec.get("info") or {}).get("title") or sp["title"],
        }
    write_api_index(built)
    return built


def write_api_index(built: dict[str, dict]) -> None:
    out = ["# Lamoda API — OpenAPI-спецификации\n"]
    total = sum(b["ops"] for b in built.values())
    out.append(
        f"\nЛокальная md-база OpenAPI-спек Lamoda. "
        f"{len(built)} {_plural_ru(len(built), 'спека', 'спеки', 'спек')} · "
        f"{_ops(total)}.\n"
    )
    for slug in sorted(built):
        b = built[slug]
        info = (b["spec"].get("info") or {})
        version = info.get("version") or ""
        out.append(f"\n## [`{slug}`]({md_link(slug)}/_README.md)\n")
        out.append(f"\n**{b['title']}** — версия `{version}`\n")
        out.append(f"\n*{_ops(b['ops'])}.* Источник: [{b['url']}]({b['url']})\n")
    (OUT_API / "_README.md").write_text("".join(out), encoding="utf-8")


def write_global_index(sections: list[dict] | None,
                        built: dict[str, dict] | None) -> None:
    out = ["# Lamoda Academy — документация API\n"]
    out.append(
        "\nЛокальная md-база документации `academy.lamoda.ru/articles/api/`: "
        "текстовые статьи и OpenAPI-спецификации. Один файл = одна статья / "
        "один эндпоинт. Удобно искать `grep`-ом, держать в репозитории и "
        "скармливать LLM.\n"
    )

    if sections:
        total_arts = sum(len(s["articles"]) for s in sections)
        out.append("\n## Статьи\n")
        out.append(
            f"\n→ [articles/_README.md](articles/_README.md) — "
            f"{len(sections)} {_plural_ru(len(sections), 'раздел', 'раздела', 'разделов')}, "
            f"{_arts(total_arts)}.\n\n"
        )
        for s in sections:
            out.append(f"- [{s['title']}](articles/{md_link(s['folder'])}/_README.md) — "
                       f"{_arts(len(s['articles']))}\n")

    if built:
        total = sum(b["ops"] for b in built.values())
        out.append("\n## OpenAPI-спецификации\n")
        out.append(
            f"\n→ [api/_README.md](api/_README.md) — "
            f"{len(built)} {_plural_ru(len(built), 'спека', 'спеки', 'спек')}, "
            f"{_ops(total)}.\n\n"
        )
        for slug in sorted(built):
            b = built[slug]
            out.append(f"- [{b['title']}](api/{md_link(slug)}/_README.md) — "
                       f"{_ops(b['ops'])}\n")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUT_ROOT / "_README.md").write_text("".join(out), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
#                              ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════

def _read_existing_specs() -> dict[str, dict]:
    """Подхватывает уже спарсенные api/<slug>/openapi.json для корневого индекса."""
    built: dict[str, dict] = {}
    if not OUT_API.exists():
        return built
    for child in sorted(OUT_API.iterdir()):
        spec_file = child / "openapi.json"
        if not child.is_dir() or not spec_file.exists():
            continue
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        n = sum(
            1 for methods in (spec.get("paths") or {}).values()
            if isinstance(methods, dict)
            for m in methods
            if m.lower() in ("get", "post", "put", "delete", "patch")
        )
        built[child.name] = {
            "spec": spec, "url": "", "ops": n,
            "title": (spec.get("info") or {}).get("title") or child.name,
        }
    return built


def _read_existing_sections() -> list[dict]:
    """Подхватывает уже спарсенные разделы статей для корневого индекса."""
    sections: list[dict] = []
    if not OUT_ARTICLES.exists():
        return sections
    for child in sorted(OUT_ARTICLES.iterdir()):
        if not child.is_dir():
            continue
        arts = [{"slug": f.stem} for f in child.glob("*.md") if f.name != "_README.md"]
        if not arts:
            continue
        title = child.name
        readme = child / "_README.md"
        if readme.exists():
            m = re.match(r"#\s+(.+)", readme.read_text(encoding="utf-8"))
            if m:
                title = m.group(1).strip()
        sections.append({"folder": child.name, "title": title, "articles": arts})
    sections.sort(key=lambda s: _natkey(s["folder"]))
    return sections


def run(mode: str = "all") -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    data = crawl()

    spec_built_map: dict[str, str] = {
        sp["url"]: sp["slug"] for sp in data["specs"]
    }
    url_map = build_url_map(data, spec_built_map)

    sections: list[dict] | None = None
    built: dict[str, dict] | None = None

    if mode in ("all", "articles"):
        sections = build_articles(data, url_map)
    if mode in ("all", "api"):
        built = build_specs(data)

    # для корневого индекса подхватываем недостающую часть с диска
    if sections is None:
        sections = _read_existing_sections()
    if built is None:
        built = _read_existing_specs()

    write_global_index(sections, built)
    print(f"[la] готово. Результат в {OUT_ROOT}")


def main() -> None:
    arg = (sys.argv[1].lower() if len(sys.argv) > 1 else "all")
    if arg in ("-h", "--help"):
        print(__doc__)
        return
    if arg not in ("all", "articles", "api"):
        print(f"Неизвестный режим '{arg}'. Допустимо: all | articles | api")
        return
    run(arg)


if __name__ == "__main__":
    main()
