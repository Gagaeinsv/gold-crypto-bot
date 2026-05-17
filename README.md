# Ігровий Telegram-бот для новин (v1.6.0)

Автоматично веде ігровий Telegram-канал: **новини в пріоритеті**, роздачі — рідко і з різних магазинів.

## Можливості

- **12 RSS-джерел** — IGN, Kotaku, PC Gamer, Eurogamer, GamesRadar, GameSpot, Destructoid, Nintendo Life, Push Square та ін.
- **Безкоштовні ігри** — Epic, Steam, GOG, PlayStation, Xbox (через GamerPower + офіційні API), без спаму itch/indiegala
- **Релізи** — ігри на найближчі 7 днів (RAWG API, опційно)
- **Gemini AI** — кожен пост **українською + російською** за єдиним шаблоном
- **Шаблон поста** — заголовок, факти, опис, «Моя думка», посилання, хештеги, CTA
- **Прямі посилання** в магазин (Steam, Epic тощо), не биті URL

## Ліміти за замовчуванням

| Параметр | Значення |
|----------|----------|
| Інтервал між постами | 6 годин |
| Максимум постів на добу | 4 |
| Роздачі на добу | 1 |
| Роздачі на тиждень | 3 |
| Новин перед наступною роздачею | 3 |

Усі ліміти налаштовуються в `.env` — див. `.env.example`.

## Швидкий старт

```bash
pip install -r requirements.txt
cp .env.example .env
# заповни GAMING_BOT_TOKEN, GAMING_CHANNEL_ID, GEMINI_KEY
python gaming_bot.py
```

Після запуску в терміналі має бути:

```text
Bot started v1.6.0: @YourBot | Channel: @YourChannel
Limits: 6h between posts, max 4/day, giveaways max 1/day 3/week
```

## Покрокова інструкція

**[НАЛАШТУВАННЯ.md](НАЛАШТУВАННЯ.md)** — ключі, посилання, оновлення, типові помилки.

## Оновлення зі старої версії

1. Замінити `gaming_bot.py` (або завантажити ZIP з гілки `gaming-news-bot-only`)
2. Оновити `.env` за шаблоном `.env.example`
3. Видалити `gaming_bot.db` (щоб скинути чергу старих роздач)
4. Перезапустити бота

Завантажити останню версію:

https://github.com/Gagaeinsv/gold-crypto-bot/archive/refs/heads/gaming-news-bot-only.zip
