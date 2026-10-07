# API docs parsers → Markdown

Парсеры публичной документации API маркетплейсов, Честного знака и транспортных
компаний. Каждый превращает документацию в **локальную базу markdown-файлов**
(один метод — один файл), которую удобно читать, искать `grep`-ом и отдавать LLM
как контекст при написании интеграций.

| Папка | Источник | Как достаётся | Slug |
|---|---|---|---|
| `WB Docs Parser` | dev.wildberries.ru (Redoc) | Playwright + stealth, спека из `__redoc_state` | `wb` |
| `Ozon Docs Parser` | docs.ozon.ru/api (Seller, Performance) | Real Chrome, `swagger.json` | `ozon` |
| `YA Docs Parser` | github.com/yandex-market/yandex-market-partner-api | tarball + bundling YAML | `ya` |
| `Lamoda Docs Parser` | academy.lamoda.ru | requests + BeautifulSoup, статьи и OpenAPI | `lamoda` |
| `CRPT Docs Parser` | docs.crpt.ru/gismt (Честный знак) | requests + BeautifulSoup, статичный HTML | `crpt` |
| `Dellin Docs Parser` | dev.dellin.ru/api/swagger (Деловые Линии) | Real Chrome без флагов автоматизации (Qrator), `schema.yaml` | `dellin` |
| `Baikal Docs Parser` | api.baikalsr.ru/restapi (Байкал Сервис) | PDF → PyMuPDF, разделы по шрифту заголовков | `baikal` |

Подробности по каждому — в README внутри папки (если есть) и в docstring скрипта.

## Запуск

Все парсеры разом, параллельно, в общую базу:

```bat
update_docs_bases.bat
```

или

```bash
python update_docs_bases.py                     # все парсеры
python update_docs_bases.py wb dellin           # только выбранные
python update_docs_bases.py --sequential        # по очереди, вывод в консоль
python update_docs_bases.py --root D:\docs\MD   # другой корень базы
python update_docs_bases.py --dry-run           # показать команды
```

Оркестратор выставляет каждому парсеру `DOCS_OUT_ROOT=<корень>\<Имя>\md`
и пишет вывод в `_run_<slug>_<дата>.log`. Без `DOCS_OUT_ROOT` парсер, запущенный
напрямую, пишет в `./md` рядом со своим скриптом.

## Зависимости

```bash
pip install -r "<Папка парсера>/requirements.txt"
python -m playwright install chromium   # для WB
```

Ozon и Деловым Линиям нужен установленный Google Chrome: антиботы пропускают
только видимый настоящий браузер, во время прогона откроется окно.

## Лицензия

MIT — см. [LICENSE](LICENSE).
