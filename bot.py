# bot.py
# Автономный гибридный сканер "Крипта 1" для Dexscreener (Solana)
# Вариант C: new pairs watcher + periodic top pairs scanner
# Зависимости: python-telegram-bot==20.3, requests
# Файлы конфигурации: config.json, pairs.json (опционально)
# Переменные окружения: BOT_TOKEN (обязательно), ADMIN_CHAT_ID (опционально)

import os
import json
import time
import logging
import asyncio
from typing import Dict, Any, Optional, List, Tuple
import requests

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# -----------------------
# Настройка логов
# -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Крипта1")

# -----------------------
# Файлы / константы
# -----------------------
STATS_FILE = "stats.json"
PAIRS_FILE = "pairs.json"        # опционально: заранее подготовленный список
CONFIG_FILE = "config.json"
CODEWORD = "Крипта 1"

# -----------------------
# Утилиты: чтение/запись json
# -----------------------
def load_json_safe(path: str, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# -----------------------
# Статистика (постоянно)
# -----------------------
def load_stats():
    default = {"codeword": CODEWORD, "entries": [], "summary": {"total":0,"correct":0,"incorrect":0}}
    return load_json_safe(STATS_FILE, default)

def save_stats(data):
    save_json(STATS_FILE, data)

def record_entry(pair_label: str, url: str, chain: str, tf: str, answer: str, x_multiplier: Optional[float], meta: Dict[str,Any]):
    data = load_stats()
    entry = {
        "pair": pair_label,
        "url": url,
        "chain": chain,
        "tf": tf,
        "answer": answer,  # "YES" or "NO"
        "x_multiplier": x_multiplier,
        "meta": meta,
        "timestamp": int(time.time())
    }
    data["entries"].append(entry)
    data["summary"]["total"] += 1
    save_stats(data)
    logger.info("Recorded: %s %s %s => %s x=%s", pair_label, chain, tf, answer, x_multiplier)

# -----------------------
# Default config (если нет config.json)
# -----------------------
DEFAULT_CONFIG = {
    "scan_interval_seconds": 60,
    "new_pairs_interval_seconds": 10,
    "timeframes": ["1m","5m"],
    "alert_chat_id": None,
    "ADMIN_CHAT_ID": None,
    "min_volume_usd": 50,
    "volume_spike_multiplier": 3.0,
    "min_buyers_recent": 8,
    "required_buyers_for_strong": 15,
    "consecutive_bull_candles": 2,
    "max_recent_drawdown_pct": 30,
    "prefer_hours": {"start":3,"end":8},  # optional: UTC hours (can be null)
    "tf_for_signal": "1m",
    "xcap": 40.0,
    "top_pairs_limit": 200,  # how many top pairs to scan periodically
    "require_multitimeframe_confirm": False  # optional confirm on multiple TFs
}

# -----------------------
# HTTP helpers (non-blocking wrapper using asyncio.to_thread)
# -----------------------
def http_get(url: str, timeout: int = 10) -> Tuple[int, str]:
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)

async def async_http_get(url: str, timeout: int = 10) -> Tuple[int, str]:
    return await asyncio.to_thread(http_get, url, timeout)

# -----------------------
# Dexscreener fetch helpers
# - fetch pair JSON: { ... } from /{chain}/{pair}.json
# - fetch top pairs / new pairs via public API (api.dexscreener.com)
# -----------------------
async def fetch_pair_json(url: str) -> Dict[str,Any]:
    if url.endswith("/"):
        url = url[:-1]
    json_url = url + ".json"
    status, text = await async_http_get(json_url, timeout=8)
    if status == 200:
        try:
            return json.loads(text)
        except Exception as e:
            logger.debug("JSON parse failed %s: %s", json_url, e)
    return {}

async def fetch_top_pairs_from_api(chain: str, limit: int = 200) -> List[Dict[str,Any]]:
    """
    Uses Dexscreener public API: https://api.dexscreener.com/latest/dex/pairs?chain=solana
    Response: {"pairs": [ { "pairUrl": "...", "pair": {...} }, ... ]}
    """
    api = f"https://api.dexscreener.com/latest/dex/pairs?chain={chain}"
    status, text = await async_http_get(api, timeout=10)
    if status == 200:
        try:
            data = json.loads(text)
            pairs = data.get("pairs") or []
            # limit and map to simplified structure
            res = []
            for p in pairs[:limit]:
                res.append({
                    "label": p.get("pair","").replace(":", "_") or p.get("pairLabel") or p.get("pairUrl"),
                    "url": p.get("pairUrl") or p.get("url") or "",
                    "chain": chain
                })
            return res
        except Exception as e:
            logger.debug("fetch_top_pairs parse error: %s", e)
    return []

async def fetch_new_pairs_from_api(chain: str, since_seconds: int = 600) -> List[Dict[str,Any]]:
    """
    A lightweight attempt to get 'new' pairs - we reuse top pairs and filter by creation time if available.
    Dexscreener API may not provide exact creation timestamp; method uses heuristics.
    """
    pairs = await fetch_top_pairs_from_api(chain, limit=500)
    # heuristic: treat first N as 'new-ish' — we'll just return first 30 as candidates
    return pairs[:50]

# -----------------------
# Strategy evaluation function (Крипта1) - strict YES/NO + x estimate + meta
# -----------------------
def evaluate_strategy_from_dex(data: Dict[str,Any], tf: str, cfg: Dict[str,Any]) -> Tuple[str, Optional[float], Dict[str,Any]]:
    meta: Dict[str,Any] = {}
    try:
        # read common fields
        pair = data.get("pair") or data.get("pairInfo") or {}
        vol24 = float(pair.get("volumeUsd24h") or pair.get("volume") or 0.0)
        price = None
        try:
            price = float(pair.get("priceUsd") or pair.get("price"))
        except Exception:
            price = None
        meta["vol24"] = vol24
        meta["price"] = price

        # 1) min volume filter
        if vol24 < cfg.get("min_volume_usd", 50):
            return "NO", None, meta

        # 2) recent volume spike from chart data
        chart = data.get("chart") or {}
        points = chart.get("data") if isinstance(chart, dict) else data.get("chartData") or []
        recent_vol = 0.0
        if isinstance(points, list) and len(points) > 0:
            # points may be list of [ts, price, vol] or dicts
            last_points = points[-5:]
            for p in last_points:
                if isinstance(p, (list,tuple)) and len(p) >= 3:
                    recent_vol += float(p[2] or 0)
                elif isinstance(p, dict):
                    recent_vol += float(p.get("v") or p.get("volume") or 0)
        meta["recent_vol"] = recent_vol
        avg_hour = vol24 / 24.0 if vol24 > 0 else 0.0
        meta["avg_hour"] = avg_hour
        spike_mult = (recent_vol / (avg_hour + 1e-9)) if avg_hour > 0 else 0.0
        meta["spike_mult"] = spike_mult
        volume_spike = spike_mult >= cfg.get("volume_spike_multiplier", 3.0)

        # 3) buyers cluster: parse recent trades section
        buyers = set()
        txs = data.get("recentTrades") or data.get("recentTxs") or data.get("trades") or []
        if isinstance(txs, dict):
            # maybe structure { 'buys': [...], 'sells': [...] }
            txs = txs.get("buys") or txs.get("recent") or []
        if isinstance(txs, list):
            recent_slice = txs[-50:]
            for t in recent_slice:
                addr = None
                if isinstance(t, dict):
                    addr = t.get("from") or t.get("buyer") or t.get("addr")
                elif isinstance(t, (list,tuple)) and len(t) >= 3:
                    addr = t[2]
                if addr:
                    buyers.add(addr)
        buyers_count = len(buyers)
        meta["buyers_count"] = buyers_count

        strong_buyers = buyers_count >= cfg.get("required_buyers_for_strong", 15)
        enough_buyers = buyers_count >= cfg.get("min_buyers_recent", 8)

        # 4) structure: check bull candles from points (close increases)
        bull_candles = 0
        closes = []
        if isinstance(points, list):
            for p in points[-6:]:
                if isinstance(p, (list,tuple)) and len(p) >= 2:
                    try:
                        closes.append(float(p[1]))
                    except:
                        pass
                elif isinstance(p, dict) and "c" in p:
                    try:
                        closes.append(float(p["c"]))
                    except:
                        pass
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                bull_candles += 1
        meta["bull_candles"] = bull_candles
        structure_ok = bull_candles >= cfg.get("consecutive_bull_candles", 2)

        # 5) final decision logic (conservative)
        if strong_buyers and volume_spike:
            est = min(cfg.get("xcap",40.0), max(1.0, spike_mult * (vol24 / 1000.0)))
            return "YES", round(est,2), meta

        if volume_spike and structure_ok and enough_buyers:
            est = min(cfg.get("xcap",40.0), max(1.0, spike_mult * (vol24 / 2000.0)))
            return "YES", round(est,2), meta

        return "NO", None, meta

    except Exception as e:
        meta["error"] = str(e)
        return "NO", None, meta

# -----------------------
# Scanner tasks
# -----------------------
async def new_pairs_watcher(app, cfg):
    """
    Частая проверка новых пар (каждые new_pairs_interval_seconds),
    и немедленное применение стратегии.
    """
    chain = "solana"
    interval = int(cfg.get("new_pairs_interval_seconds", 10))
    seen_urls = set()
    # load initial from pairs.json if exists
    if os.path.exists(PAIRS_FILE):
        try:
            p = load_json_safe(PAIRS_FILE, [])
            for el in p:
                seen_urls.add(el.get("url"))
        except Exception:
            pass

    while True:
        try:
            candidates = await fetch_new_pairs_from_api(chain, since_seconds=600)
            for cand in candidates:
                url = cand.get("url") or ""
                label = cand.get("label") or url
                if not url or url in seen_urls:
                    continue
                # mark seen immediately to avoid duplicates
                seen_urls.add(url)
                # fetch pair JSON and evaluate
                data = await fetch_pair_json(url)
                tf = cfg.get("tf_for_signal", "1m")
                ans, x, meta = evaluate_strategy_from_dex(data, tf, cfg)
                if ans == "YES":
                    chat = cfg.get("alert_chat_id") or os.getenv("ADMIN_CHAT_ID")
                    text = (
                        f"#{CODEWORD} SIGNAL (NEW)\nPair: {label}\nChain: {chain}\nTF: {tf}\nDecision: {ans}\nX: {x}\nURL: {url}\nmeta: {meta}"
                    )
                    if chat:
                        try:
                            await app.bot.send_message(chat_id=chat, text=text)
                        except Exception as e:
                            logger.exception("Send message failed: %s", e)
                    else:
                        logger.info("Signal (no chat configured): %s", text)
                    record_entry(label, url, chain, tf, ans, x, meta)
                # small throttle
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.exception("new_pairs_watcher error: %s", e)
        await asyncio.sleep(interval)

async def top_pairs_scanner(app, cfg):
    """
    Периодический скан топ пар (каждые scan_interval_seconds).
    """
    chain = "solana"
    interval = int(cfg.get("scan_interval_seconds", 60))
    limit = int(cfg.get("top_pairs_limit", 200))
    while True:
        try:
            top_pairs = await fetch_top_pairs_from_api(chain, limit=limit)
            logger.info("Top scanner: got %d pairs", len(top_pairs))
            for p in top_pairs:
                url = p.get("url") or ""
                label = p.get("label") or url
                if not url:
                    continue
                data = await fetch_pair_json(url)
                for tf in cfg.get("timeframes", ["1m"]):
                    ans, x, meta = evaluate_strategy_from_dex(data, tf, cfg)
                    if ans == "YES":
                        chat = cfg.get("alert_chat_id") or os.getenv("ADMIN_CHAT_ID")
                        text = (
                            f"#{CODEWORD} SIGNAL (TOP)\nPair: {label}\nChain: {chain}\nTF: {tf}\nDecision: {ans}\nX: {x}\nURL: {url}\nmeta: {meta}"
                        )
                        if chat:
                            try:
                                await app.bot.send_message(chat_id=chat, text=text)
                            except Exception as e:
                                logger.exception("Send message failed: %s", e)
                        else:
                            logger.info("Signal (no chat): %s", text)
                        record_entry(label, url, chain, tf, ans, x, meta)
                # small pause per pair
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.exception("top_pairs_scanner error: %s", e)
        await asyncio.sleep(interval)

# -----------------------
# Telegram command handlers
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Крипта1 автономный сканер запущен. /last /report /pairs")

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_stats()["summary"]
    await update.message.reply_text(f"Total={s['total']} correct={s['correct']} incorrect={s['incorrect']}")

async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entries = load_stats()["entries"][-10:]
    lines = []
    # index relative to total entries
    base = max(0, len(load_stats()["entries"]) - 10)
    for i, e in enumerate(entries, start=base):
        lines.append(f"#{i} {e['pair']} {e['tf']} {e['answer']} x={e.get('x_multiplier')}")
    await update.message.reply_text("\n".join(lines) if lines else "No entries")

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(PAIRS_FILE):
        p = load_json_safe(PAIRS_FILE, [])
        await update.message.reply_text(f"Pairs file exists with {len(p)} entries.")
    else:
        await update.message.reply_text("No pairs.json found. Scanner uses Dexscreener API for new/top pairs.")

# -----------------------
# Main
# -----------------------
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN env var not set")

    cfg = load_json_safe(CONFIG_FILE, DEFAULT_CONFIG)

    app = ApplicationBuilder().token(token).build()

    # register commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("pairs", cmd_pairs))

    # background tasks on start
    async def on_start(app):
        # start both tasks
        app.create_task(new_pairs_watcher(app, cfg))
        app.create_task(top_pairs_scanner(app, cfg))
        logger.info("Background scanner tasks created.")

    app.post_init = on_start

    logger.info("Starting bot (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
