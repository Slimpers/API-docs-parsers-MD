"""WB OpenAPI docs parser.

Открывает страницы https://dev.wildberries.ru/docs/openapi/<slug>, проходит
антибот через playwright-stealth, извлекает заинлайненную OpenAPI-спецификацию
из `const __redoc_state = {...}` и сохраняет:

  md/<slug>/_README.md            — индекс (метаданные + список тегов и операций)
  md/<slug>/tags/<tag-slug>.md    — описание секции из tags
  md/<slug>/operations/<op>.md    — по одному файлу на эндпоинт

Каждый md по эндпоинту содержит summary, описание, параметры, requestBody,
responses и tags. Этих файлов достаточно, чтобы в будущем по ним писать клиенты.

Использование:
    python wb_docs_parser.py                      # дефолтный список API
    python wb_docs_parser.py api-information      # одна страница
    python wb_docs_parser.py content prices ...   # несколько
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


# ───────────────────────────── константы ──────────────────────────────

BASE = "https://dev.wildberries.ru/docs/openapi/"
HERE = Path(__file__).parent
# OUT_ROOT можно переопределить через env DOCS_OUT_ROOT (для общей базы на QNAP).
OUT_ROOT = Path(os.environ["DOCS_OUT_ROOT"]) if os.environ.get("DOCS_OUT_ROOT") else HERE / "md"

# Известные страницы WB OpenAPI (берем все, что захардкодим — если страница
# отсутствует, скрипт спокойно её пропустит). Можно переопределить аргументами.
DEFAULT_SLUGS = [
    "analytics",
    "api-information",
    "customer-communication",
    "dbs",
    "documents-and-accounting",
    "in-store-pickup",
    "item-management",
    "orders-dbw",
    "orders-fbs",
    "orders-fbw",
    "promotion",
    "reports",
]


# ─────────────────────────── имена файлов ─────────────────────────────

# Запрещённые в Windows имени файла символы. Кириллица/другие unicode-буквы — OK.
_FORBIDDEN_RE = re.compile(r'[<>:"|?*\x00-\x1f]+')


def safe_name(s: str, maxlen: int = 120) -> str:
    """Превращаем произвольную строку в имя файла, безопасное для Windows.
    Сохраняем юникод-буквы (кириллицу), режем только запрещённые символы и
    сворачиваем разделители путей.
    """
    s = (s or "").strip()
    s = s.replace("/", "-").replace("\\", "-")
    s = _FORBIDDEN_RE.sub("_", s)
    # схлопнуть подряд идущие "-" и пробелы
    s = re.sub(r"[-_]{2,}", "-", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .-_")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip(" .-_")
    return s or "_"


# ─────────────────── извлечение спеки из __redoc_state ────────────────

_STATE_PREFIX = "const __redoc_state ="


def _balanced_object(text: str, start: int) -> str:
    """Возвращает подстроку JSON-объекта, начинающуюся с '{' на позиции start.

    Учитывает строки и экранирование, чтобы не поймать `}` внутри строки.
    """
    assert text[start] == "{"
    depth = 0
    i = start
    n = len(text)
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    raise ValueError("Unterminated object")


def extract_redoc_state(html: str) -> dict[str, Any]:
    # Берём вхождение БЕЗ экранированных кавычек (второе, в чистом script-теге).
    candidates = []
    pos = -1
    while True:
        pos = html.find(_STATE_PREFIX, pos + 1)
        if pos == -1:
            break
        candidates.append(pos)
    if not candidates:
        raise RuntimeError("__redoc_state not found in HTML")

    # Предпочитаем последнее вхождение — оно гарантированно «чистое» (afterInteractive).
    for pos in reversed(candidates):
        # Найти первую '{' после знака '='
        eq = html.find("=", pos)
        brace = html.find("{", eq)
        if brace == -1:
            continue
        try:
            obj_text = _balanced_object(html, brace)
        except ValueError:
            continue
        try:
            return json.loads(obj_text)
        except json.JSONDecodeError:
            continue
    raise RuntimeError("Failed to parse __redoc_state JSON")


# ───────────────────────── рендеринг markdown ─────────────────────────

def _md_escape_pipe(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


# Глобальный кэш для резолва $ref внутри одной спеки. Устанавливается в
# build_for_slug перед рендерингом md.
_CURRENT_SPEC: dict | None = None


def _resolve_ref(ref: str) -> Any:
    """Резолвит '#/components/examples/foo' в объект. Возвращает None, если нельзя."""
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
    """Если в OpenAPI у example есть $ref — резолвим его рекурсивно."""
    if isinstance(ex, dict):
        if "$ref" in ex:
            target = _resolve_ref(ex["$ref"])
            if target is None:
                return None
            return _resolve_example(target)
        # Стандартное OpenAPI Example Object: { summary, description, value, externalValue }
        if "value" in ex:
            return ex["value"]
    return ex


def _resolve_top_ref(node: Any, _seen: set | None = None) -> Any:
    """Если на верхнем уровне есть $ref — резолвим. Защита от циклов."""
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


def _build_example_from_schema(schema: Any, depth: int = 0, _seen: set | None = None) -> Any:
    """Собираем пример из schema. Если есть inline `example` — берём его.
    Для object — собираем из значений inline-примеров property; пустых полей не вставляем.
    """
    if depth > 6 or not isinstance(schema, dict):
        return None
    seen = _seen or set()
    schema = _resolve_top_ref(schema, set(seen))
    if not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        out = {}
        for name, sub in (schema.get("properties") or {}).items():
            v = _build_example_from_schema(sub, depth + 1, seen)
            if v is not None:
                out[name] = v
        return out or None
    if t == "array":
        item = _build_example_from_schema(schema.get("items"), depth + 1, seen)
        return [item] if item is not None else None
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    return None


# HTML-обёртки, которыми WB приправляет описания. Контент почти всегда
# полезный, удаляем только теги. Сохраняем `<details>`, `<summary>`,
# `<code>`, `<a href="https://...">` и собственный type-syntax вида
# `<integer (int32)>` из `schema_to_brief()`.
_HTML_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_HTML_SPAN_VAL_RE = re.compile(
    r"<span\s+class=['\"]response__value[^'\"]*['\"]>(.*?)</span>",
    re.DOTALL,
)
# WB активно использует <div class="description_auth/important/limit/ref/token">
# и <pre style="..."> — это служебные обёртки Redoc-плагинов, в обычном md
# они не рендерятся. Снимаем теги, контент остаётся.
_HTML_DIV_RE = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)
_HTML_PRE_RE = re.compile(r"</?pre\b[^>]*>", re.IGNORECASE)
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
    s = _HTML_SPAN_VAL_RE.sub(r"\1", s)
    s = _HTML_ASIDE_RE.sub("", s)
    s = _HTML_DIV_RE.sub("", s)
    s = _HTML_PRE_RE.sub("", s)
    s = _HTML_BR_RE.sub("\n", s)
    s = _TRAILING_WS_RE.sub("\n", s)
    s = _TRIPLE_NL_RE.sub("\n\n", s)
    return s.strip()


# Старое имя — оставлено для совместимости, теперь делегирует _clean_doc.
def _clean_response_desc(s: str | None) -> str:
    return _clean_doc(s)


def schema_to_brief(schema: dict | None) -> str:
    """Краткое представление схемы в одну строку (тип, формат, enum, ref)."""
    if not isinstance(schema, dict):
        return ""
    if "$ref" in schema:
        ref = schema["$ref"].rsplit("/", 1)[-1]
        return f"$ref: {ref}"
    parts = []
    t = schema.get("type")
    fmt = schema.get("format")
    if t == "array":
        items = schema.get("items") or {}
        parts.append(f"array<{schema_to_brief(items) or 'any'}>")
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
    """Раскрываем object-схему как маркдаун-список свойств.

    Верхнеуровневый $ref разрезолвливаем (с защитой от циклов), вложенные —
    оставляем по имени, чтобы не раздувать вывод.
    """
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
            # вложенные объекты разворачиваем не глубже 3 уровней
            if depth < 3 and isinstance(sub, dict):
                if sub.get("type") == "object" or "properties" in sub:
                    out.append(render_schema_block(sub, depth + 1, _seen))
                elif sub.get("type") == "array":
                    items = sub.get("items") or {}
                    if items.get("type") == "object" or "properties" in items:
                        out.append(f"{'  ' * (depth + 1)}- *(элементы)*\n")
                        out.append(render_schema_block(items, depth + 2, _seen))
    elif t == "array":
        items = schema.get("items") or {}
        out.append(f"{'  ' * depth}- array of: {schema_to_brief(items) or 'any'}\n")
        if items.get("type") == "object":
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
        # Достаём reference, если response описан через $ref на components/responses.
        # При этом description из локального resp имеет приоритет (так делает Redoc).
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
                # Нет явного example/examples — собираем из inline `example` свойств схемы.
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


def render_index_md(slug: str, info: dict, servers: list[dict],
                    tags: list[dict], paths: dict) -> str:
    out = [f"# {slug}\n"]
    title = info.get("title") or ""
    version = info.get("version") or ""
    if title:
        out.append(f"\n**{title}** — версия `{version}`\n")
    description = _clean_doc(info.get("description") or "")
    if description:
        out.append("\n" + description + "\n")
    if servers:
        out.append("\n## Серверы\n")
        for s in servers:
            url = s.get("url") or ""
            d = _clean_doc(s.get("description") or "")
            out.append(f"- `{url}`" + (f" — {d}" if d else "") + "\n")

    if tags:
        out.append("\n## Разделы (tags)\n")
        for t in tags:
            tname = t.get("name") or "—"
            out.append(f"- [{tname}](tags/{safe_name(tname)}.md)\n")

    out.append("\n## Эндпоинты\n")
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch", "options", "head"):
                continue
            summary = _clean_doc(op.get("summary") if isinstance(op, dict) else "")
            file_name = safe_name(method.upper() + " " + path.lstrip("/").replace("/", "-")) + ".md"
            label = f"`{method.upper()} {path}`"
            if summary:
                label += f" — {summary}"
            out.append(f"- [{label}](operations/{file_name})\n")
    return "".join(out)


# ───────────────────── основная сборка одного API ──────────────────────

def fetch_state(page, slug: str, attempts: int = 3) -> dict[str, Any] | None:
    """Страница WB иногда отдаёт HTML до гидрации или ещё навигируется — пробуем несколько раз."""
    for i in range(1, attempts + 1):
        state = _fetch_state_once(page, slug)
        if state is not None:
            return state
        if _REDIRECTED:
            return None
        if i < attempts:
            print(f"[wb] повтор {i + 1}/{attempts} для {slug}")
            page.wait_for_timeout(3000)
    return None


_REDIRECTED = False


def _fetch_state_once(page, slug: str) -> dict[str, Any] | None:
    global _REDIRECTED
    _REDIRECTED = False
    url = BASE + slug
    print(f"[wb] -> {url}")
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except Exception as e:
        print(f"[wb] goto failed: {e}")
        return None
    page.wait_for_timeout(1500)
    try:
        # Текущий URL после возможного редиректа
        cur = page.url
        if not cur.rstrip("/").endswith(slug):
            print(f"[wb] redirected to {cur}, skip")
            _REDIRECTED = True
            return None
        html = page.content()
        return extract_redoc_state(html)
    except Exception as e:
        print(f"[wb] parse failed: {e}")
        return None


def build_for_slug(state: dict, slug: str) -> int:
    global _CURRENT_SPEC
    spec = (state or {}).get("spec") or {}
    data = spec.get("data") or {}
    if not isinstance(data, dict) or "paths" not in data:
        print(f"[wb] no paths in spec for {slug}")
        return 0
    _CURRENT_SPEC = data

    info = data.get("info") or {}
    servers = data.get("servers") or []
    tags = data.get("tags") or []
    paths = data.get("paths") or {}

    base_dir = OUT_ROOT / slug
    (base_dir / "tags").mkdir(parents=True, exist_ok=True)
    (base_dir / "operations").mkdir(parents=True, exist_ok=True)

    # сохраняем сырой openapi на всякий случай
    (base_dir / "openapi.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # tags
    for t in tags:
        tname = t.get("name") or ""
        if not tname:
            continue
        (base_dir / "tags" / (safe_name(tname) + ".md")).write_text(
            render_tag_md(t), encoding="utf-8"
        )

    # operations
    server_url = ""
    if servers and isinstance(servers, list):
        server_url = (servers[0] or {}).get("url") or ""

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
            fname = safe_name(method.upper() + " " + path.lstrip("/").replace("/", "-")) + ".md"
            (base_dir / "operations" / fname).write_text(md, encoding="utf-8")
            written += 1

    # index
    (base_dir / "_README.md").write_text(
        render_index_md(slug, info, servers, tags, paths), encoding="utf-8"
    )
    print(f"[wb] {slug}: {written} endpoints, {len(tags)} tags")
    return written


def write_root_index(slug_stats: list[tuple[str, dict]]) -> None:
    """Сводный индекс по всем спарсенным API (md/_README.md)."""
    out = ["# Wildberries OpenAPI — индекс\n",
           "\nЛокальная база md-файлов по API Wildberries (dev.wildberries.ru/docs/openapi).",
           "Один файл = один эндпоинт. См. подпапку каждого API:\n"]
    for slug, info in slug_stats:
        title = (info.get("info") or {}).get("title") or slug
        n_paths = len(info.get("paths") or {})
        n_ops = sum(
            1
            for p in (info.get("paths") or {}).values()
            if isinstance(p, dict)
            for m in p
            if m.lower() in ("get", "post", "put", "delete", "patch")
        )
        out.append(f"- [`{slug}`]({slug}/_README.md) — **{title}** ({n_paths} путей / {n_ops} операций)\n")
    (OUT_ROOT / "_README.md").write_text("".join(out), encoding="utf-8")


def run(slugs: Iterable[str]) -> None:
    OUT_ROOT.mkdir(exist_ok=True)
    slug_stats: list[tuple[str, dict]] = []
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU",
        )
        page = ctx.new_page()

        total = 0
        for slug in slugs:
            state = fetch_state(page, slug)
            if state is None:
                continue
            cnt = build_for_slug(state, slug)
            total += cnt
            data = ((state or {}).get("spec") or {}).get("data") or {}
            if isinstance(data, dict):
                slug_stats.append((slug, data))
        write_root_index(slug_stats)
        print(f"[wb] done. total endpoints: {total}")
        browser.close()


if __name__ == "__main__":
    args = sys.argv[1:] or DEFAULT_SLUGS
    run(args)
