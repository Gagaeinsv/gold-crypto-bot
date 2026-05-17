# Ігровий Telegram-бот (v2.0.0)

Канал про **PlayStation, PS Plus, Xbox / Game Pass**: ігри місяця, підписки, релізи.  
Пости **українською + російською**. Кілька разів на день, **без спаму**. Роздачі — **лише коли немає свіжих новин**.

## Завантажити один файл

https://raw.githubusercontent.com/Gagaeinsv/gold-crypto-bot/gaming-news-bot-only/gaming_bot.py

(Також див. `ЗАВАНТАЖИТИ.txt`)

## Можливості

- **RSS:** Push Square, PlayStation Blog, Pure Xbox + відфільтровані Eurogamer / VG247
- **Фільтр:** лише PS / PS+ / Xbox / Game Pass / ігри місяця
- **Релізи:** RAWG (опційно), лише PlayStation та Xbox
- **Роздачі:** лише PlayStation / Xbox, і лише якщо 24+ год без новин
- **Gemini:** двомовний шаблон (UA + RU, CTA, хештеги)

## Ліміти за замовчуванням

| Параметр | Значення |
|----------|----------|
| Перевірка джерел | кожні 60 хв |
| Між постами (новини) | 4 год |
| Максимум постів на добу | 5 |
| Роздача | якщо 24 год без новин PS/Xbox |

## Швидкий старт

```bash
pip install -r requirements.txt
cp .env.example .env
python gaming_bot.py
```

У логах: `Bot started v2.0.0`

## Інструкція

**[НАЛАШТУВАННЯ.md](НАЛАШТУВАННЯ.md)**

## Оновлення

1. Замінити `gaming_bot.py` за посиланням вище  
2. Оновити `.env` з `.env.example`  
3. Видалити `gaming_bot.db`  
4. Перезапустити бота  
