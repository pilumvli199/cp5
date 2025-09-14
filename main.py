# main.py - Binance-only Crypto Bot (Candles + Volume + OI + GPT Analysis + Telegram)

import os, asyncio, time
from datetime import datetime
import aiohttp
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Env vars
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 300))

# Init OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Binance endpoints
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
BINANCE_CANDLE_URL = "https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=50"
BINANCE_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"

# Symbols
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"

async def send_telegram(session, text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
    await session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"})

async def fetch_ticker(session, symbol):
    async with session.get(BINANCE_TICKER_URL.format(symbol=symbol)) as r:
        d = await r.json()
        return {
            "price": float(d["lastPrice"]),
            "volume": float(d["volume"]),
            "high": float(d["highPrice"]),
            "low": float(d["lowPrice"]),
            "pct": float(d["priceChangePercent"])
        }

async def fetch_candles(session, symbol):
    async with session.get(BINANCE_CANDLE_URL.format(symbol=symbol)) as r:
        data = await r.json()
        return [
            {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
            for c in data
        ]

async def fetch_oi(session, symbol):
    try:
        async with session.get(BINANCE_OI_URL.format(symbol=symbol)) as r:
            d = await r.json()
            return float(d["openInterest"])
    except:
        return None

async def openai_analyze(market_map, candle_map):
    if not client: return None
    lines = []
    for s, d in market_map.items():
        if d:
            lines.append(f"{s}: price={d['price']} vol24h={d['volume']} OI={d.get('oi','NA')}")
    prompt = (
        "You are a crypto technical analyst. "
        "Given the spot data and OHLC candles, detect patterns (flags, triangles, double tops, head & shoulders) "
        "and indicate bias (bullish, bearish, neutral). Suggest possible buy/sell signals.\n\n"
        "Market summary:\n" + "\n".join(lines) + "\n\n"
    )
    for s, candles in candle_map.items():
        prompt += f"{s} last 10 candles (OHLC): " + ", ".join([f"[{c['open']},{c['high']},{c['low']},{c['close']}]" for c in candles[-10:]]) + "\n"
    resp = await asyncio.get_event_loop().run_in_executor(None, lambda: client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        temperature=0.3,
    ))
    return resp.choices[0].message.content.strip()

async def periodic_task():
    async with aiohttp.ClientSession() as session:
        await send_telegram(session, "*Bot online — Binance only*")
        while True:
            market_map, candle_map = {}, {}
            for s in SYMBOLS:
                try:
                    ticker = await fetch_ticker(session, s)
                    candles = await fetch_candles(session, s)
                    oi = await fetch_oi(session, s)
                    ticker["oi"] = oi
                    market_map[s] = ticker
                    candle_map[s] = candles
                except Exception as e:
                    print("Fetch error", s, e)

            analysis = await openai_analyze(market_map, candle_map)
            # Telegram message
            lines = []
            for s, d in market_map.items():
                if d:
                    lines.append(f"*{s}*: {d['price']} (24hVol={d['volume']}, OI={d['oi']})")
            msg = f"*Snapshot (UTC {datetime.utcnow().strftime('%H:%M')})*\n" + "\n".join(lines)
            if analysis: msg += f"\n\n🧠 Analysis:\n{analysis}"
            await send_telegram(session, msg)

            await asyncio.sleep(POLL_INTERVAL)

def main():
    asyncio.run(periodic_task())

if __name__ == "__main__":
    main()
