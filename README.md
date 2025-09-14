# Binance-only Crypto Bot

Features:
- Spot data (last price, 24h volume, high/low, % change)
- 5m Candlestick data (last 50)
- Futures Open Interest (OI)
- GPT-4o-mini: pattern recognition, buy/sell bias
- Telegram alerts (startup + every 5 min snapshot)

Deployment on Railway:
1. Upload repo.
2. Set environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY).
3. Deploy -> Worker runs `python main.py`.
