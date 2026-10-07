# WB API Parser → Markdown

Парсер публичной OpenAPI-документации Wildberries
(`dev.wildberries.ru/docs/openapi/*`), который превращает её в
**локальную базу markdown-файлов**: один эндпоинт = один файл. Удобно
скармливать LLM, держать в репозитории клиента, искать `grep`-ом и просто
читать без браузера.

[![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![playwright](https://img.shields.io/badge/Playwright-stealth-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](../LICENSE)

---

## Что внутри

```
md/
├── _README.md                                ← глобальный индекс
├── api-information/
│   ├── _README.md                            ← индекс API
│   ├── openapi.json                          ← сырая спека (OpenAPI 3)
│   ├── tags/<раздел>.md
│   └── operations/<METHOD путь>.md           ← один файл = один эндпоинт
├── analytics/...
├── financial-reports-and-accounting/...
├── in-store-pickup/...
├── orders-dbs/, orders-dbw/, orders-fbs/, orders-fbw/
├── promotion/...
├── reports/...
├── user-communication/...
└── work-with-products/...
```

В каждом md-файле эндпоинта:

- метод, путь, теги, `operationId`
- описание, лимиты, авторизация
- параметры (table)
- тело запроса со схемой и примерами
- все коды ответов со схемами и примерами JSON

> Текущий снимок: **12 API · 285 эндпоинтов · ~4.5 MB · 378 файлов**.

---

## Зачем

Документация WB живёт за антиботом и рендерится JS-ом. Скопировать удобный
референс «в один клик» нельзя, в LLM-контекст её не положишь.
А в md-формате с этим всё хорошо:

- **Контекст для AI-агентов.** Кладёшь нужный `operations/<endpoint>.md` в
  промпт — модель пишет рабочий клиент сразу с правильными полями и кодами
  ошибок.
- **Версионирование документации.** Чекин репо → `git diff` показывает, что
  WB сломал/добавил.
- **Быстрый поиск.** `grep -r "warehouseId"` за 0.2 секунды.
- **Офлайн.** Доки доступны без выхода в сеть.

---

## Что внутри хитрого

1. **Антибот wbaas.** Из коробки `playwright headless` ловит `498` —
   challenge не решается. Используется
   [`playwright-stealth`](https://pypi.org/project/playwright-stealth/) +
   `--disable-blink-features=AutomationControlled`.
2. **Извлечение спеки из HTML.** Документация рендерится Redoc-ом, спека
   заинлайнена в `<script>const __redoc_state = {...}</script>`. После
   `Redoc.hydrate(...)` в `window` объекта уже нет, поэтому он достаётся
   regex-ом + balanced-скобочный парсер (учитывает строки и эскейпы).
3. **Резолв `$ref`.** OpenAPI WB активно использует ссылки:
   - `responses: { 401: { $ref: '#/components/responses/401' } }`
   - `schema: { $ref: '#/components/schemas/responseCardCreate' }`
   - `examples: { creatingOneCard: { $ref: '#/components/examples/...' } }`
   Все три случая разворачиваются с защитой от циклов.
4. **Синтез JSON-примеров.** Если в response/requestBody нет `example`/
   `examples`, но у каждого свойства схемы стоит inline `example` —
   собираем их в один объект (как это делает Redoc на сайте).
5. **Автодискавери slug-ов.** Реальные имена API находятся через
   `/docs/openapi/<slug>` ссылки на главной — старые имена `content`,
   `prices`, `marketplace`, `statistics` уже не работают.

---

## Установка

```bash
git clone https://github.com/Slimpers/API-docs-parsers-MD.git
cd "API-docs-parsers-MD/WB Docs Parser"
pip install -r requirements.txt
playwright install chromium
```

## Использование

```bash
# Все известные API сразу (12 разделов, ~3 минуты):
python wb_docs_parser.py

# Один или несколько slug-ов:
python wb_docs_parser.py work-with-products promotion
```

Результат складывается в `md/`. Перезапуск — полностью обновляет файлы.

### Текущие slug-и WB OpenAPI

| slug                                | заголовок                |
|-------------------------------------|--------------------------|
| `api-information`                   | Общее                    |
| `analytics`                         | Аналитика и данные       |
| `financial-reports-and-accounting`  | Документы и бухгалтерия  |
| `in-store-pickup`                   | Заказы Самовывоз         |
| `orders-dbs`                        | Заказы DBS               |
| `orders-dbw`                        | Заказы DBW               |
| `orders-fbs`                        | Заказы FBS               |
| `orders-fbw`                        | Поставки FBW             |
| `promotion`                         | Маркетинг и продвижение  |
| `reports`                           | Отчёты                   |
| `user-communication`                | Общение с покупателями   |
| `work-with-products`                | Работа с товарами        |

---

## Пример вывода

`md/promotion/operations/GET adv-v1-balance.md`:

````markdown
# GET /adv/v1/balance

**Баланс**

теги: `Финансы`

## Описание
Метод возвращает информацию о счёте кабинета Продвижения WB...

## Авторизация
- `HeaderApiKey`

## Ответы

#### 200 — Успешно

**Content-Type:** `application/json`

- `balance` — integer. Счёт, ₽
- `net` — integer. Баланс, ₽
- `bonus` — integer. Бонусы, ₽
- `cashbacks` — array<object>. Промо-бонусы

```json
{
  "balance": 11083,
  "net": 0,
  "bonus": 15187,
  "cashbacks": [
    { "sum": 10672, "percent": 50,
      "expiration_date": "2026-04-17T10:46:02.176174Z" }
  ]
}
```

#### 401
... схема + пример из components/responses/401 ...

#### 429
... схема + пример из components/responses/429 ...
````

---

## Лицензия

MIT — делайте что хотите.

Документация принадлежит Wildberries; репозиторий служит лишь как
конвертер формата для собственного использования.
