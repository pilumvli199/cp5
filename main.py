# main.py — Robust Binance-only bot (concurrent fetch, OpenAI analyze, Telegram with dedupe)
import os
import asyncio
import time
import json
from datetime import datetime
import aiohttp
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ---------------------
# Configuration (env)
# ---------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 300))
TEST_MODE = os.environ.get("TEST_MODE", "0") == "1"

# Message cooldown (seconds) to avoid duplicates
MSG_COOLDOWN = int(os.environ.get("MSG_COOLDOWN", 70))

# Symbols
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# Endpoints
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
BINANCE_CANDLE_URL = "https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=50"
BINANCE_OI_URL = "https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"

# Local paths
STARTUP_FLAG_PATH = "/tmp/bot_startup_sent"
LAST_MSG_PATH = "/tmp/last_msg_sent"

# ---------------------
# Init OpenAI client
# ---------------------
client = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print("[WARN] OpenAI init failed:", e)
        client = None
else:
    print("[WARN] OPENAI_API_KEY not set; OpenAI analysis disabled.")

# ---------------------
# Low-level Telegram send
# ---------------------
async def _really_send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
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

def _recent_message_sent() -> bool:
    try:
        if os.path.exists(LAST_MSG_PATH):
            ts = int(open(LAST_MSG_PATH).read().strip())
            if time.time() - ts < MSG_COOLDOWN:
                return True
    except Exception:
        pass
    return False

def _mark_message_sent():
    try:
        with open(LAST_MSG_PATH, "w") as f:
            f.write(str(int(time.time())))
    except Exception as e:
        print("[WARN] cannot write last_msg file:", e)

# ---------------------
# Binance fetch helpers
# ---------------------
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
        return e  # return exception for gather handling

async def fetch_candles(session: aiohttp.ClientSession, symbol: str):
    url = BINANCE_CANDLE_URL.format(symbol=symbol)
    try:
        async with session.get(url, timeout=20) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"Klines HTTP {r.status}: {text[:200]}")
            data = await r.json()
            candles = [
                {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])}
                for c in data
            ]
            return candles
    except Exception as e:
        return e

async def fetch_oi(session: aiohttp.ClientSession, symbol: str):
    url = BINANCE_OI_URL.format(symbol=symbol)
    try:
        async with session.get(url, timeout=10) as r:
            # some pairs may return non-200; treat as None
            if r.status != 200:
                return None
            d = await r.json()
            return float(d.get("openInterest") or 0)
    except Exception:
        return None

# ---------------------
# OpenAI analysis (robust, includes DATA_MISSING markers)
# ---------------------
async def openai_analyze(market_map: dict, candle_map: dict):
    if not client:
        return None
    try:
        # Ensure stable ordering and explicit DATA_MISSING for clarity
        lines = []
        for s in SYMBOLS:
            d = market_map.get(s) or {}
            if d.get("price") is not None:
                lines.append(f"{s}: price={d['price']} vol24h={d.get('volume','NA')} OI={d.get('oi','NA')}")
            else:
                lines.append(f"{s}: DATA_MISSING")
        prompt_parts = [
            "You are a concise crypto technical analyst.",
            "Given the following spot summary and recent candlestick OHLC data for BTCUSDT, ETHUSDT, and SOLUSDT,",
            "identify common chart patterns (e.g., flag, triangle, head & shoulders, double top/bottom), state bias (bullish/bearish/neutral),",
            "and suggest possible buy/sell signals in one short paragraph.",
            "",
            "Spot summary (DATA_MISSING means that data fetch failed):",
            "\n".join(lines),
            ""
        ]
        # Append candle lines for all symbols (or DATA_MISSING)
        for s in SYMBOLS:
            candles = candle_map.get(s) or []
            if candles:
                last10 = candles[-10:]
                candle_texts = [f"[{c.get('open')},{c.get('high')},{c.get('low')},{c.get('close')}]" for c in last10]
                prompt_parts.append(f"{s} last 10 candles (OHLC): " + ", ".join(candle_texts))
            else:
                prompt_parts.append(f"{s} last 10 candles (OHLC): DATA_MISSING")
        prompt = "\n".join(prompt_parts)

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.2,
            ),
        )
        text = resp.choices[0].message.content.strip()
        lines_out = [ln.strip() for ln in text.splitlines() if ln.strip()]
        summary = "\n".join(lines_out[:6]) if len(lines_out) > 6 else text
        return summary
    except Exception as e:
        print("[ERROR] OpenAI analyze failed:", e)
        return None

# ---------------------
# Utility to clean analysis (remove long candle dumps)
# ---------------------
def _clean_analysis_text(text: str, max_lines: int = 5) -> str:
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    filtered = []
    for ln in lines:
        # remove raw "Last 10 Candles" numbered lists if present
        if ln.lower().startswith("last 10 candles") or ln[:2].lstrip().isdigit():
            continue
        filtered.append(ln)
    if not filtered:
        filtered = lines
    short = []
    for ln in filtered:
        if len(ln) > 220:
            ln = ln[:200].rstrip() + "…"
        short.append(ln)
        if len(short) >= max_lines:
            break
    return "\n".join(short)

# ---------------------
# Snapshot message builder + dedupe
# ---------------------
async def send_snapshot_message(session: aiohttp.ClientSession, market_map: dict, candle_map: dict, analysis_raw: str):
    if _recent_message_sent():
        print("[INFO] Recent message sent — skipping duplicate snapshot (cooldown).")
        return False

    header_lines = []
    for s in SYMBOLS:
        d = market_map.get(s)
        if d and d.get("price") is not None:
            header_lines.append(f"*{s}*: {d['price']} (24hVol={d.get('volume','NA')}, OI={d.get('oi','NA')})")
        else:
            header_lines.append(f"*{s}*: DATA_MISSING")

    analysis_clean = _clean_analysis_text(analysis_raw, max_lines=5)
    msg = f"*Snapshot (UTC {datetime.utcnow().strftime('%H:%M')})*\n" + "\n".join(header_lines)
    if analysis_clean:
        msg += f"\n\n🧠 Analysis:\n{analysis_clean}"

    sent = await _really_send_telegram(session, msg)
    if sent:
        _mark_message_sent()
    return sent

# ---------------------
# Startup helpers
# ---------------------
async def send_startup_once(session: aiohttp.ClientSession):
    if os.path.exists(STARTUP_FLAG_PATH):
        print("[INFO] startup flag exists, skipping startup alert for this container.")
        return
    ok = await _really_send_telegram(session, "*Bot online — Binance only*")
    if ok:
        try:
            with open(STARTUP_FLAG_PATH, "w") as f:
                f.write(str(int(time.time())))
        except Exception as e:
            print("[WARN] could not write startup flag:", e)

async def send_startup_test(session: aiohttp.ClientSession):
    symbols = ["BTCUSDT", "ETHUSDT"]
    market_map = {}
    candle_map = {}
    # fetch per-symbol synchronously here (debug)
    for s in symbols:
        t = await fetch_ticker(session, s)
        c = await fetch_candles(session, s)
        oi = await fetch_oi(session, s)
        if isinstance(t, Exception):
            print(f"[WARN] startup test ticker exception {s}: {t}")
            market_map[s] = {"price": None, "volume": None, "oi": None}
        else:
            if t:
                t["oi"] = oi
                market_map[s] = t
            else:
                market_map[s] = {"price": None, "volume": None, "oi": None}
        candle_map[s] = c if not isinstance(c, Exception) else []
    analysis = await openai_analyze(market_map, candle_map)
    header = []
    for s in symbols:
        d = market_map.get(s)
        if d and d.get("price") is not None:
            header.append(f"*{s}*: {d['price']} (24hVol={d['volume']}, OI={d.get('oi','NA')})")
        else:
            header.append(f"*{s}*: DATA_MISSING")
    msg = "*Startup test — forced snapshot*\n" + "\n".join(header)
    if analysis:
        msg += f"\n\n🧠 Analysis:\n{_clean_analysis_text(analysis, max_lines=6)}"
    await _really_send_telegram(session, msg)

# ---------------------
# Main loop
# ---------------------
async def periodic_task():
    async with aiohttp.ClientSession() as session:
        # startup alert once per container
        await send_startup_once(session)

        if TEST_MODE:
            print("[INFO] TEST_MODE=1 -> sending startup forced test (BTC/ETH).")
            # small delay to avoid overlap with main loop immediate snapshot
            await asyncio.sleep(1)
            await send_startup_test(session)

        while True:
            start = time.time()
            # Prepare placeholders
            market_map = {s: {"price": None, "volume": None, "oi": None} for s in SYMBOLS}
            candle_map = {s: [] for s in SYMBOLS}

            # Build concurrent fetch tasks (ticker, candles, oi) per symbol
            tasks = []
            for s in SYMBOLS:
                tasks.append(fetch_ticker(session, s))
                tasks.append(fetch_candles(session, s))
                tasks.append(fetch_oi(session, s))

            # gather
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # results order: tick0, cand0, oi0, tick1, cand1, oi1, ...
            for i, s in enumerate(SYMBOLS):
                tick_res = results[i * 3]
                cand_res = results[i * 3 + 1]
                oi_res = results[i * 3 + 2]

                # ticker
                if isinstance(tick_res, Exception):
                    print(f"[WARN] ticker fetch exception for {s}: {tick_res}")
                elif tick_res is None:
                    print(f"[WARN] ticker returned None for {s}")
                else:
                    market_map[s]["price"] = tick_res.get("price")
                    market_map[s]["volume"] = tick_res.get("volume")

                # candles
                if isinstance(cand_res, Exception):
                    print(f"[WARN] candles fetch exception for {s}: {cand_res}")
                    candle_map[s] = []
                elif cand_res is None:
                    print(f"[WARN] candles None for {s}")
                    candle_map[s] = []
                else:
                    candle_map[s] = cand_res

                # oi
                if isinstance(oi_res, Exception):
                    print(f"[WARN] oi fetch exception for {s}: {oi_res}")
                else:
                    market_map[s]["oi"] = oi_res

            # debug summary
            print("[DEBUG] fetch summary:")
            for s in SYMBOLS:
                print(f"  {s}: price={market_map[s]['price']}  candles={len(candle_map[s])}  oi={market_map[s]['oi']}")

            # analysis
            analysis = await openai_analyze(market_map, candle_map)

            # send snapshot (deduped)
            await send_snapshot_message(session, market_map, candle_map, analysis)

            # Sleep until next interval (account for elapsed)
            elapsed = time.time() - start
            await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))

# ---------------------
# Entrypoint
# ---------------------
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] Telegram env vars missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return
    print("[INFO] Starting Binance-only bot (robust fetch)...")
    try:
        asyncio.run(periodic_task())
    except KeyboardInterrupt:
        print("[INFO] Interrupted by user.")

if __name__ == "__main__":
    main()
