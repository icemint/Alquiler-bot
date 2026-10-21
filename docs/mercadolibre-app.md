# MercadoLibre application

| Field | Value |
| --- | --- |
| Name | NidoWatch |
| Short name | nidowatch |
| Description | Telegram bot that alerts subscribers to new rental listings matching their chosen city, neighborhood, price and size. Read-only. |

## API access check (2026-10-21)

| Request | Result |
| --- | --- |
| App token (`client_credentials`) | 200 |
| User token (`authorization_code`, read scope) | 200, refresh token issued |
| `/users/me`, `/categories/MLA1459` | 200 |
| `/sites/MLA/search` (category, newest first, free-text query), with either token | 403 forbidden |
| `/highlights/MLA/category/MLA1459` | 403 (policy) |

Search is closed to third-party applications, so there is no API way to list listings. MercadoLibre stays on the HTML scraper (`MERCADOLIBRE_MODE=html`).
