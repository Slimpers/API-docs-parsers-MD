# Yandex Market Partner API Parser → Markdown

Парсер официальной OpenAPI-спецификации партнёрского API Яндекс Маркета
(`api.partner.market.yandex.ru`) — превращает её в **локальную базу
markdown-файлов**: один эндпоинт = один файл. Удобно скармливать LLM,
держать в репозитории клиента, искать `grep`-ом и читать без браузера.

[![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](../LICENSE)

---

## Что внутри

```
md/
├── _README.md                                  ← глобальный индекс
└── partner-api/
    ├── _README.md                              ← индекс API (теги + операции)
    ├── openapi.json                            ← bundled OpenAPI 3 (один файл)
    ├── tags/<тег>.md                           ← карточка раздела
    └── operations/
        ├── orders/<METHOD путь>.md             ← один файл = один эндпоинт
        ├── reports/...
        ├── shipments/...
        └── ...                                 ← группировка по первому тегу
```

В каждом md-файле эндпоинта:

- метод, путь, теги, `operationId`, базовый URL
- описание, авторизация (`security` + `x-auth-scopes`)
- параметры (table)
- тело запроса со схемой и примерами
- все коды ответов со схемами и автоматически собранными JSON-примерами

> Текущий снимок: **1 API · 158 эндпоинтов · 36 тегов · 148 путей · ~1.4 MB
> openapi.json · 196 md-файлов · ~3.1 MB**.

---

## Зачем

Документация Яндекс Маркета на `yandex.ru/dev/market/partner-api` рендерится
JS-ом и закрыта SmartCaptcha — копировать «в один клик» нельзя, в LLM-контекст
не положишь. А в md-формате с этим всё хорошо:

- **Контекст для AI-агентов.** Кладёшь `operations/orders/POST v2-...md` в
  промпт — модель пишет рабочий клиент сразу с правильными полями,
  заголовком `Api-Key` и кодами ошибок.
- **Версионирование документации.** Чекин репо → `git diff` показывает, что
  Маркет сломал/добавил между запусками парсера.
- **Быстрый поиск.** `grep -r "campaignId"` за 0.2 секунды.
- **Офлайн.** Доки доступны без выхода в сеть.

---

## Что внутри хитрого

1. **Источник — публичный GitHub Яндекса.** Спека лежит под BSD-3 в
   [`yandex-market/yandex-market-partner-api`](https://github.com/yandex-market/yandex-market-partner-api).
   Никакого playwright/антибота не нужно — скачиваем GitHub-tarball и
   работаем локально.
2. **Multi-file → single bundle.** Корневой `openapi/openapi.yaml` — это
   только список путей с `$ref` на отдельные YAML-файлы (148 файлов в
   `paths/`, 785 + 59 + 7 в `components/{schemas,parameters,responses}/`).
   Парсер рекурсивно резолвит external-ref'ы и собирает один OpenAPI 3 dict:
   - файлы из `components/<type>/` → `components.<type>.<имя>` + ссылка
     переписывается в стандартный internal-ref `#/components/...`
   - файлы из `paths/` → инлайнятся в `paths[<url>]` целиком
3. **Глубокие цепочки `allOf`.** У Яндекса встречается
   `ApiClientDataErrorResponse → allOf → ApiErrorResponse → allOf → ApiResponse`
   на 3 уровня. Помимо мерджа `properties` верхнего уровня, парсер
   рекурсивно сворачивает вложенные `allOf` — иначе ответы 400/401/etc
   рендерились бы как пустые объекты.
4. **`x-auth-scopes`.** У Яндекса каждый метод поверх `security: ApiKey/OAuth`
   несёт расширение `x-auth-scopes` со списком разрешений токена. Выводится
   рядом с обычной авторизацией.
5. **Синтез JSON-примеров.** Если у operation нет `example`/`examples`, но у
   свойств схемы стоят inline `example` или `enum` — собираем их в один
   объект со скалярными дефолтами по `type/format` (как это делает Redoc).
6. **Группировка по тегам.** 158 операций в плоской папке неудобны —
   каждый эндпоинт раскладывается в `operations/<первый-тег>/...`. Тегов
   в корневом `openapi.yaml` нет (они только внутри методов), их имена
   собираются на лету и идут в индекс/папки.
7. **Чистка YFM-разметки.** Описания у Яндекса написаны в Yandex Flavored
   Markdown — с конструкциями `{% include notitle [text](path) %}`,
   `{% note warning %}...{% endnote %}`, `{% cut "..." %}`, `{% if audience %}`,
   `{% list tabs %}`, `{{ var-name }}`, `[{#T}](path)`. Они нерабочие в
   обычном md, поэтому `_clean_yfm` снимает обёртки (контент сохраняется)
   и режет битые относительные ссылки `[text](../../concepts/...md)`,
   оставляя только текст; внешние `https://`-ссылки не трогаем.

---

## Установка

```bash
git clone https://github.com/Slimpers/API-docs-parsers-MD.git
cd "API-docs-parsers-MD/YA Docs Parser"
pip install -r requirements.txt
```

Зависимости — только `PyYAML` и `requests`. Никаких браузеров.

## Использование

```bash
# Сборка main-ветки репозитория Яндекса (по умолчанию):
python ya_docs_parser.py

# Конкретный тэг/sha (если Яндекс выкатит релиз):
python ya_docs_parser.py --ref v1.2.3

# Сменить имя slug-папки (по умолчанию partner-api):
python ya_docs_parser.py --slug market

# Не удалять _repo/ после сборки (для отладки):
python ya_docs_parser.py --keep-repo
```

Время одного полного прогона: **~5 секунд** (скачивание tarball ~200 КБ +
bundling). Результат — в `md/`. Перезапуск полностью обновляет файлы.

---

## Пример вывода

`md/partner-api/operations/campaigns/GET v2-campaigns.md`:

````markdown
# GET /v2/campaigns

**Список магазинов пользователя**

`operationId`: getCampaigns
теги: `campaigns`, `dbs`, `express`, `fbs`, `fby`, `laas`

**Базовый URL:** `https://api.partner.market.yandex.ru`

**Полный путь:** `GET https://api.partner.market.yandex.ru/v2/campaigns`

## Описание
Возвращает список магазинов в кабинете, для которого выдан токен ...

## Авторизация
**`x-auth-scopes`:** `all-methods`, `all-methods:read-only`, `settings-management`, ...

## Параметры
| Имя | В | Тип | Обязательный | Описание |
|---|---|---|---|---|
| `pageToken` | query | string | нет | Идентификатор страницы c результатами. ... |
| `limit`     | query | integer (int32) | нет | ... |

## Ответы

#### 200 — Магазины пользователя.
- `campaigns` **(required)** — array<$ref: CampaignDTO>. Список магазинов.
  - *(элементы)*
    - `domain` — string. Название магазина.
    - `id` — $ref: CampaignId
    - `clientId` — integer (int64). Идентификатор плательщика.
    - ...
- `pager` — $ref: FlippingPagerDTO
- `paging` — $ref: PackagingForwardScrollingPagerDTO

```json
{
  "campaigns": [
    { "domain": "string", "id": 0, "clientId": 0,
      "business": { "id": 0, "name": "string" },
      "placementType": "FBS", "apiAvailability": "AVAILABLE" }
  ],
  ...
}
```
````

---

## Лицензия

MIT — делайте что хотите.

Документация принадлежит Яндексу (исходная спека — BSD-3); этот репозиторий
служит лишь как конвертер формата для собственного использования.
