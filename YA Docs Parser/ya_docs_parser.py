"""Yandex Market Partner API docs parser.

Превращает официальную OpenAPI-спецификацию Яндекс Маркета (она лежит публично
под BSD-3 в `github.com/yandex-market/yandex-market-partner-api`) в локальную
базу markdown-файлов: один эндпоинт = один файл.

    md/partner-api/_README.md                    — индекс (метаданные, теги, операции)
    md/partner-api/openapi.json                  — bundled OpenAPI 3 (один файл)
    md/partner-api/tags/<tag>.md                 — описание раздела (тега)
    md/partner-api/operations/<tag>/<METHOD путь>.md — карточка эндпоинта

Особенности (отличия от парсеров Ozon/WB):
  • Спека Яндекса — multi-file: `openapi.yaml` хранит только список путей и
    `$ref` на отдельные YAML-файлы в `paths/` и `components/`. Перед
    рендерингом мы скачиваем репозиторий через GitHub tarball и собираем все
    файлы в один dict (bundling), переписывая внешние `$ref` во внутренние
    `#/components/...`. Никакого playwright/антибота не нужно.
  • В корневом `openapi.yaml` нет `tags:`/`x-tagGroups:` — теги используются
    только внутри методов (`paths/*.yaml::tags`). Описания тегов мы
    синтезируем (имя + список операций), группировка по первому тегу.
  • Поверх ApiKey/OAuth2 у методов часто стоят `x-auth-scopes` — выводим в
    md рядом с обычной авторизацией.

Использование:
    python ya_docs_parser.py                      # main → partner-api
    python ya_docs_parser.py --ref v1.2.3         # фиксированный тэг репо
    python ya_docs_parser.py --slug partner-api   # сменить имя slug-папки
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
import yaml


# ───────────────────────────── константы ──────────────────────────────

REPO_OWNER = "yandex-market"
REPO_NAME = "yandex-market-partner-api"
TARBALL_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/tarball/{{ref}}"

HERE = Path(__file__).parent
# OUT_ROOT можно переопределить через env DOCS_OUT_ROOT (для общей базы на QNAP).
OUT_ROOT = Path(os.environ["DOCS_OUT_ROOT"]) if os.environ.get("DOCS_OUT_ROOT") else HERE / "md"
REPO_DIR = HERE / "_repo"

DEFAULT_SLUG = "partner-api"
DEFAULT_REF = "main"


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
    """URL-encode пробелов/амперсандов в md-ссылке, сохраняя `/`."""
    return quote(path, safe="/")


# ────────────────────────── скачивание репо ──────────────────────────

def fetch_repo(ref: str) -> Path:
    """Скачивает репозиторий через GitHub tarball API и распаковывает в REPO_DIR.

    Возвращает путь к каталогу `openapi/` внутри распакованного дерева.
    Tarball-API не требует ни авторизации, ни git/playwright.
    """
    url = TARBALL_URL.format(ref=ref)
    print(f"[ya] fetching tarball {url}")
    r = requests.get(url, timeout=120, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"tarball {url} → HTTP {r.status_code}")
    blob = r.content  # ~3-5 МБ, можно держать в памяти

    if REPO_DIR.exists():
        shutil.rmtree(REPO_DIR)
    REPO_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[ya] extracting {len(blob) / 1024:.0f} KB ...")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        tf.extractall(REPO_DIR)

    # tarball распаковывается как `<owner>-<repo>-<sha>/`
    children = [c for c in REPO_DIR.iterdir() if c.is_dir()]
    if not children:
        raise RuntimeError("empty tarball")
    root = children[0]
    openapi_dir = root / "openapi"
    if not openapi_dir.is_dir():
        raise RuntimeError(f"openapi/ not found in {root}")
    return openapi_dir


# ───────────────────── bundling multi-file YAML ────────────────────────

class Bundler:
    """Сворачивает multi-file OpenAPI Яндекса в один dict.

    `paths/*.yaml` целиком инлайнятся в `paths[<url>]`.
    Файлы из `components/{schemas,parameters,responses}/` собираются в
    `components.<type>.<имя>` (имя = stem yaml-файла), а ссылки на них
    переписываются в стандартные internal-ref'ы `#/components/...`.

    Транзитивно: вложенные `$ref` внутри загружаемых файлов тоже разрешаются.
    """

    COMPONENT_TYPES = ("schemas", "parameters", "responses",
                       "requestBodies", "examples", "headers")

    def __init__(self, openapi_dir: Path):
        self.openapi_dir = openapi_dir.resolve()
        self.paths_dir = (openapi_dir / "paths").resolve()
        self.components_dir = (openapi_dir / "components").resolve()
        # Кэш загруженных yaml-файлов, чтобы не парсить дважды
        self._cache: dict[Path, Any] = {}
        # Аккумулятор для components/<type>/<name>
        self.components: dict[str, dict[str, Any]] = {
            t: {} for t in self.COMPONENT_TYPES
        }
        # Защита от циклов при загрузке компонентов
        self._loading: set[Path] = set()

    # ───── загрузка и резолв путей ─────

    def _load_yaml(self, path: Path) -> Any:
        path = path.resolve()
        if path in self._cache:
            return self._cache[path]
        if not path.is_file():
            raise FileNotFoundError(f"YAML not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self._cache[path] = data
        return data

    def _resolve_path(self, ref: str, base_file: Path) -> tuple[Path, str | None]:
        """Разбирает `relative/path.yaml#/fragment` относительно base_file."""
        if "#" in ref:
            file_part, fragment = ref.split("#", 1)
        else:
            file_part, fragment = ref, None
        if not file_part:
            # Чисто фрагмент в текущем файле — у Яндекса не встречается, но
            # на всякий случай поддержим.
            return base_file, fragment
        target = (base_file.parent / file_part).resolve()
        return target, fragment

    def _classify_component(self, path: Path) -> tuple[str, str] | None:
        """Если файл лежит под `components/<type>/<name>.yaml`, возвращает
        (type, name) — имя без расширения."""
        try:
            rel = path.relative_to(self.components_dir)
        except ValueError:
            return None
        parts = rel.parts
        if len(parts) != 2:
            return None
        ctype, fname = parts
        if ctype not in self.COMPONENT_TYPES:
            return None
        name = Path(fname).stem
        return ctype, name

    # ───── рекурсивный обход ─────

    def _rewrite(self, node: Any, base_file: Path) -> Any:
        """Возвращает копию node, в которой все внешние `$ref` либо
        переписаны во внутренние `#/components/...`, либо инлайнены целиком.

        Внутренние `#/...` (если бы они появились) оставляем как есть.
        """
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                ref = node["$ref"]
                # Уже внутренний — не трогаем
                if ref.startswith("#/"):
                    return dict(node)
                # Внешний ref → резолвим
                target_file, fragment = self._resolve_path(ref, base_file)
                comp = self._classify_component(target_file)
                if comp is not None:
                    ctype, cname = comp
                    self._ensure_component(ctype, cname, target_file, fragment)
                    new_ref = f"#/components/{ctype}/{cname}"
                    out = {k: v for k, v in node.items() if k != "$ref"}
                    out["$ref"] = new_ref
                    return out
                # Не компонент — инлайним содержимое (paths/* и пр.)
                inlined = self._load_resolved(target_file, fragment)
                # Если у $ref в node были другие ключи (description и т.п.) —
                # сохраняем их поверх инлайна (как в OpenAPI 3.1).
                extras = {k: v for k, v in node.items() if k != "$ref"}
                if isinstance(inlined, dict):
                    return {**inlined, **extras}
                return inlined
            return {k: self._rewrite(v, base_file) for k, v in node.items()}
        if isinstance(node, list):
            return [self._rewrite(v, base_file) for v in node]
        return node

    def _load_resolved(self, target_file: Path, fragment: str | None) -> Any:
        """Загружает yaml-файл, переписывает его внутренности и возвращает
        либо весь файл, либо нужный fragment."""
        data = self._load_yaml(target_file)
        if fragment:
            # Простой JSON-pointer резолв
            node: Any = data
            for part in fragment.lstrip("/").split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return None
            data = node
        return self._rewrite(data, target_file)

    def _ensure_component(self, ctype: str, name: str,
                          source_file: Path, fragment: str | None) -> None:
        """Регистрирует компонент в self.components[ctype][name], рекурсивно
        переписывая его содержимое. Защита от циклов через self._loading.
        """
        bucket = self.components[ctype]
        if name in bucket:
            return
        # Стопор для рекурсии: помечаем сразу пустым словарём, потом
        # перезапишем готовым содержимым.
        if source_file in self._loading:
            return
        self._loading.add(source_file)
        try:
            bucket[name] = {}  # placeholder против бесконечной рекурсии
            data = self._load_yaml(source_file)
            if fragment:
                node: Any = data
                for part in fragment.lstrip("/").split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(node, dict) and part in node:
                        node = node[part]
                    else:
                        node = None
                        break
                data = node
            bucket[name] = self._rewrite(data, source_file)
        finally:
            self._loading.discard(source_file)

    # ───── сборка корневого spec ─────

    def bundle(self) -> dict[str, Any]:
        root_file = self.openapi_dir / "openapi.yaml"
        spec = self._load_yaml(root_file)
        if not isinstance(spec, dict):
            raise RuntimeError("openapi.yaml is not a mapping")

        # Инлайним paths
        new_paths: dict[str, Any] = {}
        for path, value in (spec.get("paths") or {}).items():
            if isinstance(value, dict) and "$ref" in value and isinstance(value["$ref"], str):
                ref = value["$ref"]
                if ref.startswith("#/"):
                    new_paths[path] = self._rewrite(value, root_file)
                    continue
                target_file, fragment = self._resolve_path(ref, root_file)
                inlined = self._load_resolved(target_file, fragment)
                if isinstance(inlined, dict):
                    new_paths[path] = inlined
                else:
                    print(f"[ya] WARN: path {path} ref {ref} resolved to non-dict")
            else:
                new_paths[path] = self._rewrite(value, root_file)
        spec = dict(spec)
        spec["paths"] = new_paths

        # Сливаем components: то, что было в корневом openapi.yaml + то, что
        # подобрали при resolve'е ref-ов.
        existing_components = self._rewrite(spec.get("components") or {}, root_file)
        if not isinstance(existing_components, dict):
            existing_components = {}
        merged_components = dict(existing_components)
        for ctype, bucket in self.components.items():
            if not bucket:
                continue
            target_bucket = dict(merged_components.get(ctype) or {})
            target_bucket.update(bucket)
            merged_components[ctype] = target_bucket
        spec["components"] = merged_components

        return spec


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
    "int64": 0,
    "uint32": 0,
    "uint64": 0,
    "float": 0.0,
    "double": 0.0,
}


def _flatten_allof(schema: Any, _seen: set | None = None, _depth: int = 0) -> dict[str, Any]:
    """Рекурсивно сворачивает цепочку `allOf` (включая вложенные allOf и $ref)
    в один словарь с объединёнными `properties` / `required`. Возвращает
    пустые `{}`, если у схемы нет ни одного properties — звено в цепочке
    может быть пустым.

    Используется в `render_schema_block` и `_build_example_from_schema`
    перед обработкой object-веток. У Яндекса встречается цепочка глубиной
    2–3 (например, `ApiClientDataErrorResponse → ApiErrorResponse → ApiResponse`).
    """
    if _depth > 8 or not isinstance(schema, dict):
        return {}
    seen = _seen or set()
    schema = _resolve_top_ref(schema, set(seen))
    if not isinstance(schema, dict):
        return {}
    out: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    if "properties" in schema:
        out["properties"].update(schema["properties"] or {})
    if "required" in schema:
        out["required"].extend(schema["required"] or [])
    if "allOf" in schema and isinstance(schema["allOf"], list):
        for sub in schema["allOf"]:
            child = _flatten_allof(sub, set(seen), _depth + 1)
            out["properties"].update(child.get("properties") or {})
            out["required"].extend(child.get("required") or [])
    return out


def _build_example_from_schema(schema: Any, depth: int = 0, _seen: set | None = None) -> Any:
    if depth > 8 or not isinstance(schema, dict):
        return None
    seen = _seen or set()
    schema = _resolve_top_ref(schema, set(seen))
    if not isinstance(schema, dict):
        return None
    if "example" in schema:
        return schema["example"]

    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged = _flatten_allof(schema, set(seen))
        if merged.get("properties"):
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

# YFM (Yandex Flavored Markdown) — разметка, которой Яндекс пишет описания
# у себя на `yandex.ru/dev`. В нашей md-базе она бесполезна:
#   • `{% include notitle [text](../../path.md) %}` — инклуд файла, которого нет
#   • `{% note warning "..." %} ... {% endnote %}` — обёртка-callout
#   • `{{ var-name }}` — yfm-переменные/плейсхолдеры
#   • `[{#T}](../../path.md)` — «подставь заголовок целевой страницы»
#   • `[Текст](../../concepts/foo.md#anchor)` — относительные ссылки в никуда
# Чистим перед рендерингом, иначе md-файлы захламлены строками вида
# `{% include notitle [access](../../_auto/method_scopes/getCampaigns.md) %}`.

_YFM_INCLUDE_RE = re.compile(r"\{%\s*include\b[^%]*?%\}")
_YFM_NOTE_OPEN_RE = re.compile(r"\{%\s*note\b[^%]*?%\}")
_YFM_NOTE_CLOSE_RE = re.compile(r"\{%\s*endnote\s*%\}")
# `{% cut "Заголовок" %} ... {% endcut %}` — yfm collapse-блок. Контент
# сохраняем, теги обёртки убираем.
_YFM_CUT_OPEN_RE = re.compile(r"\{%\s*cut\b[^%]*?%\}")
_YFM_CUT_CLOSE_RE = re.compile(r"\{%\s*endcut\s*%\}")
# `{% if audience == "partner" %} ... {% endif %}` — условные блоки yfm
# для разных аудиторий доки. Контент валиден для обеих, тегам тут не место.
_YFM_IF_OPEN_RE = re.compile(r"\{%\s*if\b[^%]*?%\}")
_YFM_IF_CLOSE_RE = re.compile(r"\{%\s*endif\s*%\}")
_YFM_ELSE_RE = re.compile(r"\{%\s*else\s*%\}")
# `{% list tabs %} ... {% endlist %}` — yfm tabs-блок.
_YFM_LIST_OPEN_RE = re.compile(r"\{%\s*list\b[^%]*?%\}")
_YFM_LIST_CLOSE_RE = re.compile(r"\{%\s*endlist\s*%\}")
_YFM_VAR_RE = re.compile(r"\{\{[^}]*?\}\}")
_YFM_T_LINK_RE = re.compile(r"\[\{#T\}\]\([^)]*\)")
# Любая markdown-ссылка `[text](url)`, где url не http(s) и не абсолютный
# (`/...`) — у Яндекса это всегда `../../concepts/...md`-ссылки в их доку.
_BAD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?!https?://|/|#)([^)]*)\)")
_TRIPLE_NL_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


def _clean_yfm(s: str | None) -> str:
    """Удаляет yfm-разметку и битые относительные md-ссылки.

    Содержимое `{% note %}` сохраняется (мы режем только теги-обёртки).
    Внешние http(s)-ссылки оставляем как есть.
    """
    if not s:
        return ""
    s = _YFM_INCLUDE_RE.sub("", s)
    s = _YFM_NOTE_OPEN_RE.sub("", s)
    s = _YFM_NOTE_CLOSE_RE.sub("", s)
    s = _YFM_CUT_OPEN_RE.sub("", s)
    s = _YFM_CUT_CLOSE_RE.sub("", s)
    s = _YFM_IF_OPEN_RE.sub("", s)
    s = _YFM_IF_CLOSE_RE.sub("", s)
    s = _YFM_ELSE_RE.sub("", s)
    s = _YFM_LIST_OPEN_RE.sub("", s)
    s = _YFM_LIST_CLOSE_RE.sub("", s)
    s = _YFM_VAR_RE.sub("", s)
    s = _YFM_T_LINK_RE.sub("", s)
    s = _BAD_LINK_RE.sub(r"\1", s)
    s = _TRAILING_WS_RE.sub("\n", s)
    s = _TRIPLE_NL_RE.sub("\n\n", s)
    return s.strip()


def _md_escape_pipe(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


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
    desc = _clean_yfm(schema.get("description"))
    if desc and depth == 0:
        out.append(desc + "\n")

    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged = _flatten_allof(schema, set(_seen))
        if merged.get("properties"):
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
            sub_desc = _clean_yfm(sub_desc)
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
                desc=_md_escape_pipe(_clean_yfm(p.get("description", ""))),
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
    desc = _clean_yfm(body.get("description"))
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
        desc = _clean_yfm(local_desc or merged.get("description") or "")

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
    summary = _clean_yfm(op.get("summary") or "")
    description = _clean_yfm(op.get("description") or "")
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
    scopes = op.get("x-auth-scopes")
    if sec is not None or scopes:
        out.append("\n## Авторизация\n\n")
        if sec is None:
            pass
        elif not sec:
            out.append("Без авторизации.\n")
        else:
            for s in sec:
                if isinstance(s, dict):
                    for name, sc in s.items():
                        sc_str = ", ".join(sc) if sc else "—"
                        out.append(f"- `{name}` (scopes: {sc_str})\n")
        if scopes:
            out.append("\n**`x-auth-scopes`:** ")
            out.append(", ".join(f"`{s}`" for s in scopes))
            out.append("\n")

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


def render_tag_md(name: str, ops: list[dict]) -> str:
    out = [f"# {name}\n",
           f"\n*{_ops(len(ops))}.*\n",
           "\n## Эндпоинты\n\n"]
    for op in sorted(ops, key=lambda x: (x["path"], x["method"])):
        method = op["method"].upper()
        path = op["path"]
        summary = op.get("summary") or ""
        line = f"- [`{method} {path}`](../operations/{md_link(safe_name(name))}/{md_link(_op_filename(method, path))})"
        if summary:
            line += f" — {summary}"
        if op.get("deprecated"):
            line += " ⚠️ deprecated"
        out.append(line + "\n")
    return "".join(out)


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
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
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", (name or "")).strip("-").lower()
    return f"{prefix}-{s}" if s else prefix


def render_index_md(slug: str, info: dict, servers: list[dict],
                    paths: dict, op_files: dict[str, list[dict]]) -> str:
    title = info.get("title") or slug
    version = info.get("version") or ""
    description = _clean_yfm(info.get("description") or "")

    total_ops = sum(len(v) for v in op_files.values())
    server_url = ""
    if servers:
        server_url = (servers[0] or {}).get("url") or ""

    out: list[str] = []
    out.append(f"# {slug}\n")
    out.append(f"\n**{title}** — версия `{version}`\n")
    if server_url:
        out.append(f"\n**Базовый URL:** `{server_url}`\n")
    n_tags = len(op_files)
    n_paths = len(paths)
    out.append(
        f"\n**Сводка:** {_ops(total_ops)} · "
        f"{n_tags} {_plural_ru(n_tags, 'тег', 'тега', 'тегов')} · "
        f"{n_paths} {_plural_ru(n_paths, 'путь', 'пути', 'путей')}.\n"
    )
    out.append(
        "\n> **Как ориентироваться:** см. [Содержание](#содержание) ниже → "
        "выбираете раздел (тег) → ссылку на конкретный эндпоинт. Файлы "
        "эндпоинтов лежат в `operations/<TagName>/<METHOD путь>.md`.\n"
    )
    if description:
        out.append("\n" + description + "\n")

    if servers and len(servers) > 1:
        out.append("\n## Серверы\n")
        for s in servers:
            url = s.get("url") or ""
            d = _clean_yfm(s.get("description") or "")
            out.append(f"- `{url}`" + (f" — {d}" if d else "") + "\n")

    out.append("\n## Содержание\n")
    for tname in sorted(op_files.keys(), key=lambda s: s.lower()):
        n = len(op_files[tname])
        out.append(f"- [{tname}](#{_anchor('tag', tname)}) — {_ops(n)}\n")

    for tname in sorted(op_files.keys(), key=lambda s: s.lower()):
        ops = op_files[tname]
        out.append(f'\n<a id="{_anchor("tag", tname)}"></a>\n')
        out.append(f"### {tname} — {_ops(len(ops))}\n")
        out.append(f"\n*Полная карточка:* [tags/{tname}.md](tags/{md_link(safe_name(tname))}.md)\n\n")
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

    return "".join(out)


# ───────────────────── основная сборка одного API ──────────────────────

def _op_filename(method: str, path: str) -> str:
    return safe_name(method.upper() + " " + path.lstrip("/").replace("/", "-")) + ".md"


def build(spec: dict, slug: str) -> int:
    global _CURRENT_SPEC
    if not isinstance(spec, dict) or "paths" not in spec:
        print(f"[ya] no paths in spec for {slug}")
        return 0
    _CURRENT_SPEC = spec

    info = spec.get("info") or {}
    servers = spec.get("servers") or []
    paths = spec.get("paths") or {}

    base_dir = OUT_ROOT / slug
    if base_dir.exists():
        shutil.rmtree(base_dir)
    (base_dir / "tags").mkdir(parents=True, exist_ok=True)
    (base_dir / "operations").mkdir(parents=True, exist_ok=True)

    (base_dir / "openapi.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    server_url = ""
    if servers and isinstance(servers, list):
        server_url = (servers[0] or {}).get("url") or ""

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
                "summary": _clean_yfm(op.get("summary") or ""),
                "op_id": op.get("operationId") or "",
                "deprecated": bool(op.get("deprecated")),
            })
            written += 1

    # tag-карточки (Яндекс не даёт описаний тегов, синтезируем)
    for tname, ops in op_files.items():
        (base_dir / "tags" / (safe_name(tname) + ".md")).write_text(
            render_tag_md(tname, ops), encoding="utf-8"
        )

    (base_dir / "_README.md").write_text(
        render_index_md(slug, info, servers, paths, op_files),
        encoding="utf-8",
    )
    print(f"[ya] {slug}: {written} endpoints, {len(op_files)} tags")
    return written


def write_root_index(slug_stats: list[tuple[str, dict]]) -> None:
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
                print(f"[ya] cannot read cached spec {spec_file}: {e}")

    out: list[str] = [
        "# Yandex Market Partner API — индекс\n",
        "\nЛокальная база md-файлов по партнёрскому API Яндекс Маркета "
        "(`api.partner.market.yandex.ru`). Источник — официальная "
        f"OpenAPI-спека из [`{REPO_OWNER}/{REPO_NAME}`]"
        f"(https://github.com/{REPO_OWNER}/{REPO_NAME}). "
        "Один файл = один эндпоинт.\n",
    ]
    for slug in sorted(by_slug.keys()):
        spec = by_slug[slug]
        info = spec.get("info") or {}
        title = info.get("title") or slug
        version = info.get("version") or ""
        paths = spec.get("paths") or {}
        n_paths = len(paths)
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
        if ops_per_tag:
            out.append("\nРазделы:\n")
            for tn in sorted(ops_per_tag.keys(), key=lambda s: s.lower()):
                out.append(f"- [{tn}]({slug}/_README.md#{_anchor('tag', tn)}) — "
                           f"{_ops(ops_per_tag[tn])}\n")
        out.append(
            f"\n→ Полный навигатор по разделам и эндпоинтам: "
            f"[{slug}/_README.md]({slug}/_README.md)\n"
        )
    (OUT_ROOT / "_README.md").write_text("".join(out), encoding="utf-8")


def run(slug: str, ref: str, keep_repo: bool) -> None:
    OUT_ROOT.mkdir(exist_ok=True)
    t0 = time.time()
    openapi_dir = fetch_repo(ref)
    print(f"[ya] bundling multi-file spec from {openapi_dir}")
    bundler = Bundler(openapi_dir)
    spec = bundler.bundle()
    n_paths = len(spec.get("paths") or {})
    n_schemas = len((spec.get("components") or {}).get("schemas") or {})
    print(f"[ya] bundled: {n_paths} paths, {n_schemas} schemas "
          f"(took {time.time() - t0:.1f}s)")
    build(spec, slug)
    write_root_index([(slug, spec)])
    if not keep_repo and REPO_DIR.exists():
        shutil.rmtree(REPO_DIR)
    print("[ya] done")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--slug", default=DEFAULT_SLUG,
                    help="имя slug-папки в md/ (по умолчанию: partner-api)")
    ap.add_argument("--ref", default=DEFAULT_REF,
                    help="git ref репозитория (ветка/тэг/sha, по умолчанию: main)")
    ap.add_argument("--keep-repo", action="store_true",
                    help="не удалять _repo/ после сборки (для отладки)")
    args = ap.parse_args()
    run(args.slug, args.ref, args.keep_repo)


if __name__ == "__main__":
    main()
