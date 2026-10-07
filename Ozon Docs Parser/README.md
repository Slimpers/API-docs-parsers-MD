# Ozon API Parser → Markdown

Парсер публичной OpenAPI-документации Ozon Seller и Performance
(`docs.ozon.ru/api/<slug>`), который превращает её в **локальную базу
markdown-файлов**: один эндпоинт = один файл. Удобно скармливать LLM,
держать в репозитории клиента, искать `grep`-ом и просто читать без браузера.

[![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![playwright](https://img.shields.io/badge/Playwright-stealth-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](../LICENSE)

---

## Что внутри

```
md/
├── _README.md                                ← глобальный индекс
├── seller/
│   ├── _README.md                            ← индекс API (теги + x-tagGroups)
│   ├── openapi.json                          ← сырая спека (OpenAPI 3)
│   ├── tags/<раздел>.md                      ← описание раздела из tags
│   └── operations/
│       ├── AnalyticsAPI/<METHOD путь>.md     ← один файл = один эндпоинт
│       ├── ProductAPI/...
│       ├── FBS/...
│       └── ...                               ← группировка по первому тегу
└── performance/
    ├── _README.md
    ├── openapi.json
    ├── tags/...
    └── operations/
        ├── Campaign/...
        ├── Statistics/...
        └── ...
```

В каждом md-файле эндпоинта:

- метод, путь, теги, `operationId`
- описание, авторизация, базовый URL
- параметры (table)
- тело запроса со схемой и примерами
- все коды ответов со схемами и примерами JSON

> Текущий снимок: **2 API · 492 эндпоинта · 80 тегов · 10 групп · ~7.2 MB · 577 файлов**
> (seller: 446 op / 69 tags / 8 groups, performance: 46 op / 11 tags / 2 groups).

---

## Зачем

Документация Ozon живёт за антиботом и рендерится JS-ом. Скопировать удобный
референс «в один клик» нельзя, в LLM-контекст её не положишь.
А в md-формате с этим всё хорошо:

- **Контекст для AI-агентов.** Кладёшь нужный
  `operations/<TagName>/<endpoint>.md` в промпт — модель пишет рабочий
  клиент сразу с правильными полями, заголовками `Client-Id` / `Api-Key` и
  кодами ошибок.
- **Версионирование документации.** Чекин репо → `git diff` показывает, что
  Ozon сломал/добавил.
- **Быстрый поиск.** `grep -r "warehouse_id"` за 0.2 секунды.
- **Офлайн.** Доки доступны без выхода в сеть.

---

## Что внутри хитрого

1. **Антибот wbaas Ozon.** `playwright` headless ловит «Доступ ограничен» —
   challenge не решается. Спасает связка
   [`playwright-stealth`](https://pypi.org/project/playwright-stealth/) +
   `launch_persistent_context(channel="chrome", headless=False)` — то есть
   **реальный установленный Chrome** с локальным профилем (`.chrome-profile/`).
   Куки challenge кешируются между запусками.
2. **Источник спеки.** Redoc у Ozon не инлайнит OpenAPI в HTML, а тянет
   `https://docs.ozon.ru/api/seller/swagger.json` отдельным запросом. После
   прохождения antibot тот же playwright-контекст идёт в `page.request.get(...)`
   и забирает чистый JSON со всеми cookies.
3. **Группировка по тегам.** В Seller API ~450 операций — плоская папка
   неудобна. Каждый эндпоинт раскладывается в `operations/<первый-тег>/...`.
4. **Резолв `$ref`.** Ozon активно ссылается на компоненты:
   - `responses: { 400: { $ref: '#/components/responses/...' } }`
   - `schema: { $ref: '#/components/schemas/...' }`
   - `examples: { ...: { $ref: '#/components/examples/...' } }`
   Все три случая разворачиваются с защитой от циклов.
5. **`allOf` со слиянием.** Многие схемы у Ozon собраны из `allOf` — мы
   мерджим их `properties` в одну плоскую схему перед рендерингом.
6. **Синтез JSON-примеров.** Если у operation нет `example`/`examples`, но
   у каждого свойства схемы стоит inline `example` — собираем их в один
   объект (как это делает Redoc на сайте).
7. **URL-encode ссылок.** В тегах Ozon встречаются пробелы и `&`
   (`Getting started`, `Prices&StocksAPI`) — индекс генерируется с
   `urllib.parse.quote`, чтобы ссылки не ломались на GitHub.

---

## Установка

```bash
git clone https://github.com/Slimpers/API-docs-parsers-MD.git
cd "API-docs-parsers-MD/Ozon Docs Parser"
pip install -r requirements.txt
playwright install chromium
```

Дополнительно нужен **установленный Google Chrome** (channel="chrome"
в playwright). Если Chrome не установлен — поставьте
[Google Chrome](https://www.google.com/chrome/) или замените channel на
`"msedge"` в `ozon_docs_parser.py`.

## Использование

```bash
# Все slug-и по умолчанию (seller + performance):
python ozon_docs_parser.py

# Один или несколько вручную:
python ozon_docs_parser.py seller
python ozon_docs_parser.py performance
python ozon_docs_parser.py seller performance
```

Корневой `md/_README.md` обновляется инкрементально — повторный запуск
одного slug-а не стирает упоминания других уже спарсенных.

При первом запуске откроется окно Chrome — это нормально, оно нужно для
прохождения antibot challenge. Последующие запуски используют сохранённый
профиль из `.chrome-profile/` и проходят быстрее.

Результат складывается в `md/`. Перезапуск — полностью обновляет файлы.

### Текущие slug-и Ozon

| slug          | заголовок                       | базовый URL                            |
|---------------|---------------------------------|----------------------------------------|
| `seller`      | Документация Ozon Seller API    | `https://api-seller.ozon.ru`           |
| `performance` | Документация Ozon Performance API | `https://api-performance.ozon.ru:443` |

---

## Пример вывода

`md/seller/operations/AnalyticsAPI/POST v1-analytics-stocks.md`:

````markdown
# POST /v1/analytics/stocks

**Получить аналитику по остаткам**

`operationId`: AnalyticsAPI_AnalyticsStocks
теги: `AnalyticsAPI`

**Базовый URL:** `https://api-seller.ozon.ru`

**Полный путь:** `POST https://api-seller.ozon.ru/v1/analytics/stocks`

## Описание
Используйте метод, чтобы получить аналитику по остаткам товаров на складах...

## Параметры
| Имя         | В      | Тип    | Обязательный | Описание         |
|-------------|--------|--------|--------------|------------------|
| `Client-Id` | header | string | да           | Идентификатор клиента. |
| `Api-Key`   | header | string | да           | API-ключ.        |

## Запрос
### Тело запроса
- `cluster_ids` — array<string (int64)>. Фильтр по идентификаторам кластеров.
- `skus` **(required)** — array<string (int64)>. Фильтр по SKU Ozon.
- `warehouse_ids` — array<string (int64)>. Фильтр по идентификаторам складов.
... (полная схема + автоматически собранный пример) ...

## Ответы

#### 200 — Аналитика по остаткам на складах
- `items` — array<v1AnalyticsStocksResponseItem>. Информация о товарах.
... схема + JSON-пример ...

#### 400 — Неверный параметр
... схема + пример из components/responses ...
````

---

## Лицензия

MIT — делайте что хотите.

Документация принадлежит Ozon; репозиторий служит лишь как конвертер
формата для собственного использования.
