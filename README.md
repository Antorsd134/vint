# Vinted Autoresponder

Многопоточный автоответчик для Vinted.de с управлением через Telegram-бота, интеграцией с Google Gemini (gemini-1.5-flash) и антидетект-браузером Vision.

## Возможности

- **Мульти-аккаунт мониторинг** — каждый аккаунт работает в изолированном браузерном профиле с отдельным прокси
- **Google Gemini AI** — автоматические ответы покупателям и определение готовности к сделке (intent detection)
- **Память диалогов** — Gemini запоминает стиль общения каждого покупателя и адаптируется
- **Telegram-бот** — загрузка куки, уведомления о сделках, отправка реквизитов, ручные ответы, статистика
- **Динамические реквизиты** — PayPal/IBAN запрашиваются у админа в Telegram на каждую сделку
- **Антидетект** — Vision API профили + человекоподобная печать с рандомными задержками
- **Строгий JSON-режим** — Gemini возвращает только валидный JSON через `response_mime_type`

## Установка

### 1. Зависимости

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Конфигурация

```bash
cp config.example.json config.json
cp .env.example .env
```

Заполните `.env`:
```
GEMINI_API_KEY=ваш-ключ-gemini
BOT_TOKEN=токен-telegram-бота
ADMIN_CHAT_ID=ваш-chat-id
```

Отредактируйте `config.json`:
```json
{
  "vision_api_url": "http://localhost:3000",
  "vinted_inbox_url": "https://www.vinted.de/inbox",
  "check_interval_min": 45,
  "check_interval_max": 90,
  "typing_delay_min": 60,
  "typing_delay_max": 140,
  "accounts": {}
}
```

### 3. Прокси

Добавьте прокси в `data/proxies.txt` (один на строку):
```
ip:port:login:password
```

### 4. Запуск Vision

Убедитесь, что антидетект-браузер Vision запущен и доступен по адресу из конфига.

### 5. Запуск

```bash
python main.py
```

## Использование Telegram-бота

### Добавление аккаунта
Отправьте боту `.json` файл с куки Vinted. Бот автоматически:
- Сохранит куки в `data/cookies/`
- Привяжет свободный прокси
- Запустит мониторинг

### Уведомления о сделках
При обнаружении готовности покупателя к сделке бот отправляет уведомление с кнопками:
- **[PayPal]** — запросит email, отправит покупателю
- **[Карта/IBAN]** — запросит реквизиты, отправит покупателю
- **[Вручную]** — введите произвольный ответ

### Команды
- `/start` — приветствие
- `/help` — справка
- `/stats` — статистика по аккаунтам

## Структура проекта

```
├── main.py              # Точка входа, оркестрация
├── telegram_bot.py      # Telegram-бот (aiogram 3.x)
├── browser_core.py      # Vision API + Playwright CDP
├── llm_handler.py       # Google Gemini интеграция
├── config.json          # Конфигурация (создать из example)
├── .env                 # API ключи (создать из example)
├── requirements.txt     # Зависимости
└── data/
    ├── proxies.txt      # Список прокси
    └── cookies/         # Куки аккаунтов
```
