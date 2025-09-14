# main.py — Binance-only bot (robust fetch + startup forced-test)
import os
import asyncio
import time
import json
from datetime import datetime
import aiohttp
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ENV
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 300))
TEST_MODE = os.environ.get("TEST_MODE", "0") == "1"

# Init OpenAI
client = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print("[WARN] OpenAI init failed:", e)
        client = None

# Binance endpoints
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
BINANCE_CANDLE_URL = "https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=50"
BINANCE_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"

STARTUP_FLAG_PATH = "/tmp/bot_startup_sent"

# Helpers
async def send_telegram(session: aiohttp.ClientSession, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram credentials missing.")
        return False
    url = TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=15) as resp:
            txt = await resp.text()
            if resp.status != 200:
                print(f"[ERROR] Telegram send failed {resp.status}: {txt}")
                return False
            return True
    except Exception as e:
        print("[ERROR] Telegram request exception:", e)
        return False

async def fetch_ticker(session: aiohttp.ClientSession, symbol: str):
    url = BINANCE_TICKER_URL.format(symbol=symbol)
    try:
        async with session.get(url, timeout=15) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"Ticker HTTP {r.status}: {text[:200]}")
            d = await r.json()
            return {
                "price": float(d.get("lastPrice") or d.get("last_price") or 0),
                "volume": float(d.get("volume") or 0),
                "high": float(d.get("highPrice") or 0),
                "low": float(d.get("lowPrice") or 0),
                "pct": float(d.get("priceChangePercent") or 0),
            }
    except Exception as e:
        print(f"[ERROR] fetch_ticker {symbol}: {e}")
        return None

async def fetch_candles(session: aiohttp.ClientSession, symbol: str):
    url = BINANCE_CANDLE_URL.format(symbol=symbol)
    try:
        async with session.get(url, timeout=20) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"KlInes HTTP {r.status}: {text[:200]}")
            data = await r.json()
            candles = [
                {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
                for c in data
            ]
            return candles
    except Exception as e:
        print(f"[ERROR] fetch_candles {symbol}: {e}")
        return []

async def fetch_oi(session: aiohttp.ClientSession, symbol: str):
    url = BINANCE_OI_URL.format(symbol=symbol)
    try:
        async with session.get(url, timeout=10) as r:
            if r.status != 200:
                # futures OI endpoint might 404 for some non-futures symbols on some pairs
                return None
            d = await r.json()
            return float(d.get("openInterest") or 0)
    except Exception as e:
        # not fatal; OI optional
        print(f"[WARN] fetch_oi {symbol}: {e}")
        return None

async def openai_analyze(market_map: dict, candle_map: dict):
    if not client:
        print("[WARN] OpenAI client not initialized; skipping analysis.")
        return None
    try:
        lines = []
        for s, d in market_map.items():
            if d:
                lines.append(f"{s}: price={d['price']} vol24h={d['volume']} OI={d.get('oi','NA')}")
        prompt = (
            "You are a concise crypto technical analyst. Given the following spot summary and recent candlestick OHLC data, "
            "identify any common chart patterns (e.g., flag, triangle, head & shoulders, double top/bottom), state bias (bullish/bearish/neutral), "
            "and suggest possible buy/sell signals in one short paragraph.\n\n"
            "Spot summary:\n" + "\n".join(lines) + "\n\n"
        )
        for s, candles in candle_map.items():
            prompt += f"{s} last 10 candles (OHLC): " + ", ".join([f\"[{c['open']},{c['high']},{c['low']},{c['close']}]\" for c in candles[-10:]]) + "\n"
        # call OpenAI in thread
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=250,
                temperature=0.2,
            ),
        )
        text = resp.choices[0].message.content.strip()
        # condense: take first 6 lines or full
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        summary = "\n".join(lines[:6]) if len(lines) > 6 else text
        return summary
    except Exception as e:
        print("[ERROR] OpenAI analyze failed:", e)
        return None

# Startup alert once per container (local flag)
async def send_startup_once(session: aiohttp.ClientSession):
    if os.path.exists(STARTUP_FLAG_PATH):
        print("[INFO] startup flag exists, skipping startup alert for this container.")
        return
    ok = await send_telegram(session, "*Bot online — Binance only*")
    if ok:
        try:
            with open(STARTUP_FLAG_PATH, "w") as f:
                f.write(str(int(time.time())))
        except Exception as e:
            print("[WARN] could not write startup flag:", e)

# Forced startup test — fetch BTC/ETH and send a forced snapshot (useful for debug)
async def send_startup_test(session: aiohttp.ClientSession):
    symbols = ["BTCUSDT", "ETHUSDT"]
    market_map = {}
    candle_map = {}
    for s in symbols:
        t = await fetch_ticker(session, s)
        c = await fetch_candles(session, s)
        oi = await fetch_oi(session, s)
        if t:
            t["oi"] = oi
            market_map[s] = t
            candle_map[s] = c
    analysis = await openai_analyze(market_map, candle_map)
    lines = []
    for s, d in market_map.items():
        lines.append(f"*{s}*: {d['price']} (24hVol={d['volume']}, OI={d.get('oi','NA')})")
    msg = "*Startup test — forced snapshot*\\n" + "\\n".join(lines)
    if analysis:
        msg += f"\\n\\n🧠 Analysis:\\n{analysis}"
    await send_telegram(session, msg)

async def periodic_task():
    async with aiohttp.ClientSession() as session:
        # startup once
        await send_startup_once(session)
        # If TEST_MODE, also send forced startup test to verify fetch->openai->telegram
        if TEST_MODE:
            print("[INFO] TEST_MODE=1 -> sending startup forced test (BTC/ETH).")
            await send_startup_test(session)
        # main loop
        while True:
            start = time.time()
            market_map = {}
            candle_map = {}
            for s in SYMBOLS:
                t = await fetch_ticker(session, s)
                c = await fetch_candles(session, s)
                oi = await fetch_oi(session, s)
                if t:
                    t["oi"] = oi
                    market_map[s] = t
                else:
                    market_map[s] = None
                candle_map[s] = c
            # analyze
            analysis = await openai_analyze(market_map, candle_map)
            # build message
            lines = []
            for s, d in market_map.items():
                if d:
                    lines.append(f"*{s}*: {d['price']} (24hVol={d['volume']}, OI={d.get('oi','NA')})")
                else:
                    lines.append(f"*{s}*: fetch_failed")
            msg = f"*Snapshot (UTC {datetime.utcnow().strftime('%H:%M')})*\\n" + "\\n".join(lines)
            if analysis:
                # pretty short
                pretty = "\\n".join([ln.strip() for ln in analysis.splitlines() if ln.strip()][:6])
                msg += f"\\n\\n🧠 Analysis:\\n{pretty}"
            await send_telegram(session, msg)
            # sleep respecting poll interval
            elapsed = time.time() - start
            await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] Telegram env vars missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return
    print("[INFO] Starting Binance-only bot...")
    try:
        asyncio.run(periodic_task())
    except KeyboardInterrupt:
        print("[INFO] Interrupted by user.")

if __name__ == "__main__":
    main()
