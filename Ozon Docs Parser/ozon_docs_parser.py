"""Ozon API docs parser.

Скачивает OpenAPI-спеку с `https://docs.ozon.ru/api/<slug>/swagger.json` (за
антиботом wbaas) и раскладывает её в локальную базу markdown-файлов:

    md/<slug>/_README.md                     — индекс (метаданные + теги + список операций)
    md/<slug>/openapi.json                   — сырой OpenAPI 3
    md/<slug>/tags/<tag>.md                  — описание раздела (из tags)
    md/<slug>/operations/<METHOD путь>.md    — один файл = один эндпоинт

Каждый md по эндпоинту содержит summary, описание, параметры, requestBody,
responses и теги. Этих файлов достаточно, чтобы по ним писать клиенты.

Особенности Ozon (отличия от WB-парсера):
  • Антибот пробивается только Real Chrome (channel="chrome") + persistent
    profile. После прохождения challenge тот же browser context используется
    для прямого скачивания swagger.json через `page.request.get(...)`.
  • Спека одной страницей (~5–10 МБ JSON), но содержит ~400+ операций.
    Эндпоинты дополнительно группируются в подпапке `operations/<tag>/...`
    (по первому тегу), чтобы файловое дерево не превращалось в плоскую кашу.
  • Формально OpenAPI 3, но используется и x-tagGroups (Redoc) — мы их
    отображаем в индексе.

Использование:
    python ozon_docs_parser.py                # парсит slug по умолчанию (seller)
    python ozon_docs_parser.py seller         # явно
    python ozon_docs_parser.py seller foo     # несколько API
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


# ───────────────────────────── константы ──────────────────────────────

BASE = "https://docs.ozon.ru/api/"
HERE = Path(__file__).parent
# OUT_ROOT можно переопределить через env DOCS_OUT_ROOT (для общей базы на QNAP).
OUT_ROOT = Path(os.environ["DOCS_OUT_ROOT"]) if os.environ.get("DOCS_OUT_ROOT") else HERE / "md"
PROFILE_DIR = HERE / ".chrome-profile"

DEFAULT_SLUGS = ["seller", "performance"]


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
    return quote(path, safe="/")


# ───────────────────────── резолв $ref / схем ─────────────────────────

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
    "int64": "0",   # Ozon строкой передаёт int64
    "uint32": 0,
    "uint64": "0",  # uint64 у Ozon тоже строкой
    "float": 0.0,
    "double": 0.0,
}


def _build_example_from_schema(schema: Any, depth: int = 0, _seen: set | None = None) -> Any:
    """Собираем JSON-пример из schema. Поведение как у Redoc на сайте:
    inline example > enum[0] > дефолт по типу/format. None — только если ничего
    не определено и для составных типов нечего собрать.
    """
    if depth > 8 or not isinstance(schema, dict):
        return None
    seen = _seen or set()
    schema = _resolve_top_ref(schema, set(seen))
    if not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]

    # allOf — сливаем properties детей, потом продолжаем как с одной схемой
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

    # oneOf / anyOf — берём первый вариант
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
    fmt = schema.get("format") or ""

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


# ───────────────────────── рендеринг markdown ─────────────────────────

def _md_escape_pipe(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


# HTML-мусор, которым Ozon приправляет описания. Текст внутри почти всегда
# полезный, удаляем только теги-обёртки. Сохраняем `<details>`, `<summary>`,
# `<code>`, `<a href="https://...">` и собственный type-syntax вида
# `<integer (int32)>` из `schema_to_brief()`.
_HTML_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
# Внутренние якоря на operationId (<a href="#operation/Foo">text</a>) у нас
# не работают: целевой md лежит по пути operations/<tag>/<METHOD путь>.md.
_HTML_OPLINK_RE = re.compile(
    r'<a\s+href\s*=\s*\\?["\']?#operation/[^"\'\s>\\]+\\?["\']?[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_SPAN_VAL_RE = re.compile(
    r"<span\s+class=['\"]response__value[^'\"]*['\"]>(.*?)</span>",
    re.DOTALL,
)
_HTML_ASIDE_RE = re.compile(r"</?aside\b[^>]*>", re.IGNORECASE)
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")
_TRIPLE_NL_RE = re.compile(r"\n{3,}")


def _clean_doc(s: str | None) -> str:
    """Чистит HTML-обёртки, не трогая полезный контент.

    Применяется к op.summary/description, schema.description, sub_desc,
    requestBody.description и response.description.
    """
    if not s:
        return ""
    s = _HTML_STYLE_RE.sub("", s)
    s = _HTML_OPLINK_RE.sub(r"\1", s)
    s = _HTML_SPAN_VAL_RE.sub(r"\1", s)
    s = _HTML_ASIDE_RE.sub("", s)
    s = _HTML_BR_RE.sub("\n", s)
    s = _TRAILING_WS_RE.sub("\n", s)
    s = _TRIPLE_NL_RE.sub("\n\n", s)
    return s.strip()


# Старое имя функции — оставлено для совместимости, теперь делегирует _clean_doc.
def _clean_response_desc(s: str | None) -> str:
    return _clean_doc(s)


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

    # allOf — сливаем properties детей в одну схему
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
        # Резолвим $ref у items для разворачивания вложенного объекта
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
        desc = _clean_response_desc(desc)

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
    out.append(f"\n**Полный путь:** `{method.upper()} {server.rstrip('/') + path if server else path}`\n")
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


def _anchor(prefix: str, name: str) -> str:
    """HTML-якорь, безопасный для GitHub-markdown (без капризов авто-anchor'а)."""
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", (name or "")).strip("-").lower()
    return f"{prefix}-{s}" if s else prefix


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_HEADER_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)


def _first_sentence(text: str | None, maxlen: int = 200) -> str:
    """Берём первое содержательное предложение из markdown-описания.

    Чистим markdown-заголовки и html-теги (Ozon охотно вставляет <aside>,
    `<br>` и подзаголовки прямо в начало description тега).
    """
    if not text:
        return ""
    cleaned = _MD_HEADER_RE.sub("", text)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    # Берём первую непустую строку
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


def render_index_md(slug: str, info: dict, servers: list[dict],
                    tags: list[dict], paths: dict, tag_groups: list[dict],
                    op_files: dict[str, list[dict]]) -> str:
    """Подробный навигационный индекс по одному API.

    op_files: tag-name -> [{method, path, rel, summary, op_id, deprecated}, ...]
    """
    title = info.get("title") or slug
    version = info.get("version") or ""
    description = _clean_doc(info.get("description") or "")

    tag_desc = {t.get("name"): (t.get("description") or "") for t in tags if t.get("name")}

    # Какие теги уже разложены по группам — и какие остались «без группы»
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
    out.append(f"# {slug}\n")
    out.append(f"\n**{title}** — версия `{version}`\n")
    if server_url:
        out.append(f"\n**Базовый URL:** `{server_url}`\n")
    n_tags = len(op_files)
    n_groups = len(tag_groups)
    n_paths = len(paths)
    out.append(
        f"\n**Сводка:** {_ops(total_ops)} · "
        f"{n_tags} {_plural_ru(n_tags, 'тег', 'тега', 'тегов')} · "
        f"{n_groups} {_plural_ru(n_groups, 'группа', 'группы', 'групп')} · "
        f"{n_paths} {_plural_ru(n_paths, 'путь', 'пути', 'путей')}.\n"
    )
    out.append(
        "\n> **Как ориентироваться:** см. [Содержание](#содержание) ниже → "
        "выбираете группу → раздел (тег) → ссылку на конкретный эндпоинт. "
        "Файлы эндпоинтов лежат в `operations/<TagName>/<METHOD путь>.md`.\n"
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

    # ─── оглавление (TOC) ────────────────────────────────────────────────
    out.append("\n## Содержание\n")
    if tag_groups:
        for g in tag_groups:
            gname = g.get("name") or "—"
            anchor = _anchor("group", gname)
            # сколько эндпоинтов в группе
            n = sum(len(op_files.get(tn) or []) for tn in (g.get("tags") or []))
            out.append(f"- [{gname}](#{anchor}) — {_ops(n)}\n")
    if orphan_tags:
        out.append(f"- [Прочие разделы](#group-other) — "
                   f"{sum(len(op_files[t]) for t in orphan_tags)} операций\n")

    # ─── описание тега (один компактный helper) ──────────────────────────
    def _emit_tag(tname: str) -> None:
        ops = op_files.get(tname) or []
        anchor = _anchor("tag", tname)
        out.append(f'\n<a id="{anchor}"></a>\n')
        out.append(f"### {tname} — {_ops(len(ops))}\n")
        # ссылка на полную карточку тега и содержимое описания
        link_tag = md_link(safe_name(tname))
        out.append(f"\n*Полная карточка:* [tags/{tname}.md](tags/{link_tag}.md)\n")
        desc = _first_sentence(tag_desc.get(tname, ""), maxlen=400)
        if desc:
            out.append(f"\n{desc}\n")
        if not ops:
            out.append("\n*Нет операций.*\n")
            return
        out.append("\n")
        # Сортируем сначала по path, потом по method — стабильно и читаемо
        for op in sorted(ops, key=lambda x: (x["path"], x["method"])):
            method = op["method"].upper()
            path = op["path"]
            summary = op.get("summary") or ""
            dep = " ⚠️ deprecated" if op.get("deprecated") else ""
            line = f"- [`{method} {path}`]({md_link(op['rel'])})"
            if summary:
                line += f" — {summary}"
            if dep:
                line += dep
            out.append(line + "\n")

    # ─── секции по группам ───────────────────────────────────────────────
    if tag_groups:
        for g in tag_groups:
            gname = g.get("name") or "—"
            ganchor = _anchor("group", gname)
            tnames = g.get("tags") or []
            n = sum(len(op_files.get(tn) or []) for tn in tnames)
            out.append(f'\n<a id="{ganchor}"></a>\n')
            out.append(f"## {gname} — {_ops(n)}\n")

            # Группа целиком без операций → сжимаем в одну строку-список ссылок
            if n == 0:
                if tnames:
                    out.append("\n*Вводные разделы (без эндпоинтов):* ")
                    out.append(
                        " · ".join(
                            f"[{tn}](tags/{md_link(safe_name(tn))}.md)"
                            for tn in tnames
                        )
                    )
                    out.append("\n")
                continue

            for tn in tnames:
                if tn in op_files:
                    _emit_tag(tn)
                else:
                    # Тег внутри «рабочей» группы, но без операций — мета-страница.
                    desc = _first_sentence(tag_desc.get(tn, ""), maxlen=300)
                    link = md_link(safe_name(tn))
                    line = f"- *(без операций)* [{tn}](tags/{link}.md)"
                    if desc:
                        line += f" — {desc}"
                    out.append("\n" + line + "\n")

    # ─── теги без группы ─────────────────────────────────────────────────
    if orphan_tags:
        out.append('\n<a id="group-other"></a>\n')
        out.append(f"## Прочие разделы — "
                   f"{sum(len(op_files[t]) for t in orphan_tags)} операций\n")
        for tn in sorted(orphan_tags, key=lambda s: s.lower()):
            _emit_tag(tn)

    return "".join(out)


# ───────────────────── основная сборка одного API ──────────────────────

def fetch_spec(page, slug: str) -> dict[str, Any] | None:
    """Открывает доку Ozon, дожидается прохождения antibot и качает swagger.json."""
    page_url = BASE + slug + "/"
    print(f"[oz] open {page_url}")
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=120_000)
    except Exception as e:
        print(f"[oz] goto failed: {e}")
        return None

    # Ждём прохождения antibot challenge: title должен поменяться, контент вырасти.
    # Разные API отдают HTML разного размера (Seller ~9МБ, Performance ~800КБ),
    # поэтому ориентируемся на исчезновение challenge-маркеров и на title.
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(2)
        try:
            t = page.title() or ""
            cur = page.url or ""
            sz = len(page.content())
        except Exception:
            continue
        if "Доступ ограничен" in t or "Antibot" in t or "Challenge" in t:
            continue
        if "abt-challenge" in cur:
            continue
        # Антибот пройден: либо большой контент, либо «нормальный» title документации
        if sz > 200_000 and ("Документация" in t or "API" in t or "Ozon" in t):
            break
        if sz > 1_500_000:
            break

    # Качаем swagger.json через тот же контекст (унаследует cookies от антибота).
    spec_url = BASE + slug + "/swagger.json"
    print(f"[oz] fetch {spec_url}")
    for attempt in range(3):
        try:
            resp = page.request.get(
                spec_url,
                headers={"Referer": page_url, "Accept": "application/json"},
                timeout=120_000,
            )
            if resp.status != 200:
                print(f"[oz] swagger.json status {resp.status}")
                time.sleep(3)
                continue
            text = resp.text()
            return json.loads(text)
        except Exception as e:
            print(f"[oz] swagger fetch fail (attempt {attempt + 1}): {e}")
            time.sleep(3)
    return None


def _op_filename(method: str, path: str) -> str:
    return safe_name(method.upper() + " " + path.lstrip("/").replace("/", "-")) + ".md"


def build_for_slug(spec: dict, slug: str) -> int:
    global _CURRENT_SPEC
    if not isinstance(spec, dict) or "paths" not in spec:
        print(f"[oz] no paths in spec for {slug}")
        return 0
    _CURRENT_SPEC = spec

    info = spec.get("info") or {}
    servers = spec.get("servers") or []
    tags = spec.get("tags") or []
    tag_groups = spec.get("x-tagGroups") or []
    paths = spec.get("paths") or {}

    base_dir = OUT_ROOT / slug
    (base_dir / "tags").mkdir(parents=True, exist_ok=True)
    (base_dir / "operations").mkdir(parents=True, exist_ok=True)

    (base_dir / "openapi.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for t in tags:
        tname = t.get("name") or ""
        if not tname:
            continue
        (base_dir / "tags" / (safe_name(tname) + ".md")).write_text(
            render_tag_md(t), encoding="utf-8"
        )

    server_url = ""
    if servers and isinstance(servers, list):
        server_url = (servers[0] or {}).get("url") or ""
    # Ozon отдаёт protocol-relative URL вида "//api-seller.ozon.ru" — нормализуем
    if server_url.startswith("//"):
        server_url = "https:" + server_url

    # Группируем по первому тегу — складываем в operations/<tag>/<file>
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
            target = base_dir / "operations" / tag_dir / fname
            target.write_text(md, encoding="utf-8")
            op_files.setdefault(tag_name, []).append({
                "method": method,
                "path": path,
                "rel": f"operations/{tag_dir}/{fname}",
                "summary": _clean_doc(op.get("summary") or ""),
                "op_id": op.get("operationId") or "",
                "deprecated": bool(op.get("deprecated")),
            })
            written += 1

    (base_dir / "_README.md").write_text(
        render_index_md(slug, info, servers, tags, paths, tag_groups, op_files),
        encoding="utf-8",
    )
    print(f"[oz] {slug}: {written} endpoints, {len(tags)} tags, {len(tag_groups)} tag-groups")
    return written


def write_root_index(slug_stats: list[tuple[str, dict]]) -> None:
    """Собирает корневой индекс по ВСЕМ существующим md/<slug>/openapi.json.

    Так инкрементальный запуск (`python ozon_docs_parser.py performance`) не
    стирает упоминания slug-ов, спарсенных ранее.
    """
    by_slug: dict[str, dict] = {s: sp for s, sp in slug_stats}
    if OUT_ROOT.exists():
        for child in sorted(OUT_ROOT.iterdir()):
            if not child.is_dir():
                continue
            spec_file = child / "openapi.json"
            if child.name in by_slug or not spec_file.exists():
                continue
            try:
                by_slug[child.name] = json.loads(spec_file.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[oz] cannot read cached spec {spec_file}: {e}")

    out: list[str] = [
        "# Ozon API — индекс\n",
        "\nЛокальная база md-файлов по Ozon API (`docs.ozon.ru/api/<slug>`). "
        "Один файл = один эндпоинт.\n",
    ]
    for slug in sorted(by_slug.keys()):
        spec = by_slug[slug]
        info = spec.get("info") or {}
        title = info.get("title") or slug
        version = info.get("version") or ""
        paths = spec.get("paths") or {}
        tag_groups = spec.get("x-tagGroups") or []
        n_paths = len(paths)
        # Считаем операции и распределение по группам/тегам
        n_ops = 0
        ops_per_tag: dict[str, int] = {}
        for p, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for m, op in methods.items():
                if m.lower() not in ("get", "post", "put", "delete", "patch"):
                    continue
                if not isinstance(op, dict):
                    continue
                n_ops += 1
                tag = (op.get("tags") or ["_misc"])[0]
                ops_per_tag[tag] = ops_per_tag.get(tag, 0) + 1

        out.append(f"\n## [`{slug}`]({slug}/_README.md)\n")
        out.append(f"\n**{title}** — версия `{version}`\n")
        out.append(f"\n*{_ops(n_ops)} в {n_paths} {_plural_ru(n_paths, 'пути', 'путях', 'путях')}.*\n")

        if tag_groups:
            out.append("\nГруппы методов:\n")
            for g in tag_groups:
                gname = g.get("name") or "—"
                tnames = g.get("tags") or []
                gops = sum(ops_per_tag.get(tn, 0) for tn in tnames)
                # Якорь ведёт прямо в нужную секцию seller/_README.md
                anchor = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", gname).strip("-").lower()
                out.append(
                    f"- [{gname}]({slug}/_README.md#group-{anchor}) — {_ops(gops)}\n"
                )
        out.append(
            f"\n→ Полный навигатор по разделам и эндпоинтам: "
            f"[{slug}/_README.md]({slug}/_README.md)\n"
        )
    (OUT_ROOT / "_README.md").write_text("".join(out), encoding="utf-8")


def run(slugs: Iterable[str]) -> None:
    OUT_ROOT.mkdir(exist_ok=True)
    PROFILE_DIR.mkdir(exist_ok=True)
    slug_stats: list[tuple[str, dict]] = []
    with Stealth().use_sync(sync_playwright()) as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,            # антибот пробивается только видимым браузером
            channel="chrome",          # реальный Chrome, не chromium
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            viewport={"width": 1366, "height": 800},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        total = 0
        for slug in slugs:
            spec = fetch_spec(page, slug)
            if spec is None:
                continue
            cnt = build_for_slug(spec, slug)
            total += cnt
            slug_stats.append((slug, spec))
        write_root_index(slug_stats)
        print(f"[oz] done. total endpoints: {total}")
        ctx.close()


if __name__ == "__main__":
    args = sys.argv[1:] or DEFAULT_SLUGS
    run(args)
