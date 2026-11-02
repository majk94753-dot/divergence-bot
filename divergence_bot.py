#!/usr/bin/env python3
"""
Бот дивергенцій для BingX → Telegram.

Логіка така сама, як у індикаторі TradingView «Дивергенції (мульти-осцилятор)»:
шукає півоти на осциляторі й порівнює їх з ціною (low/high) у тих самих точках.

Запуск:
    python divergence_bot.py            — одна перевірка (так працює GitHub Actions)
    python divergence_bot.py --test     — надіслати тестове повідомлення в Telegram
    python divergence_bot.py --dry-run  — перевірка без надсилання і без збереження стану
    python divergence_bot.py --history  — показати всі дивергенції за останні 150 свічок
    python divergence_bot.py --loop 15  — працювати безперервно, перевірка кожні 15 хв (для свого ПК)

Потрібні змінні середовища TELEGRAM_TOKEN і TELEGRAM_CHAT_ID
(або впишіть їх нижче в TELEGRAM_TOKEN_FALLBACK / TELEGRAM_CHAT_ID_FALLBACK — тільки для свого ПК!).
Сторонні бібліотеки не потрібні — лише стандартний Python 3.9+.
"""

import html
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ═══════════════════════════ НАЛАШТУВАННЯ ═══════════════════════════

SYMBOLS = [
    "SUIUSDT", "KASUSDT", "AVAXUSDT", "GRASSUSDT", "HYPEUSDT", "ONDOUSDT",
    "ALLOUSDT", "AAVEUSDT", "TIAUSDT", "ZROUSDT", "NEARUSDT", "UNIUSDT",
    "DOTUSDT", "SEIUSDT", "DOGEUSDT", "ICPUSDT", "ADAUSDT", "SOLUSDT",
    "VIRTUALUSDT", "OPUSDT", "SANDUSDT", "RLCUSDT", "ATOMUSDT",
]

# Таймфрейми: 1m 3m 5m 15m 30m 1h 2h 4h 6h 12h 1d
TIMEFRAMES = ["15m","1h", "4h"]

# "swap" — безстрокові ф'ючерси BingX, "spot" — спот.
# Якщо пари немає на обраному ринку, бот автоматично спробує інший.
MARKET = "swap"

# Осцилятор: RSI, MACD, Stochastic, CCI, MFI, OBV, Momentum
OSCILLATOR = "RSI"
LENGTH = 14              # період осцилятора
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
STOCH_SMOOTH = 3

# Півоти (як у індикаторі)
PIVOT_LEFT = 5           # барів ліворуч
PIVOT_RIGHT = 5          # барів праворуч = затримка сигналу
RANGE_MIN = 5            # мін. відстань між півотами
RANGE_MAX = 60           # макс. відстань між півотами

# Які дивергенції надсилати
SHOW_REGULAR_BULL = True
SHOW_HIDDEN_BULL = True
SHOW_REGULAR_BEAR = True
SHOW_HIDDEN_BEAR = True

# Сповіщати про дивергенції, підтверджені за останні N закритих свічок.
# Запас на випадок, якщо запуск за розкладом запізнився. Дублів не буде — бот пам'ятає надіслане.
ALERT_LOOKBACK_BARS = 3

CANDLES_LIMIT = 500      # скільки свічок завантажувати

# Тільки для запуску на своєму ПК (на GitHub використовуйте Secrets!)
TELEGRAM_TOKEN_FALLBACK = ""
TELEGRAM_CHAT_ID_FALLBACK = ""

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ═══════════════════════════════════════════════════════════════════

BINGX_HOSTS = ["https://open-api.bingx.com", "https://open-api.bingx.pro"]
TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000,
}
DIV_INFO = {
    "regular_bull": ("🟢", "Звичайна бичача дивергенція", "ціна — нижчий мінімум, осцилятор — вищий"),
    "hidden_bull":  ("🟩", "Прихована бичача дивергенція", "ціна — вищий мінімум, осцилятор — нижчий"),
    "regular_bear": ("🔴", "Звичайна ведмежа дивергенція", "ціна — вищий максимум, осцилятор — нижчий"),
    "hidden_bear":  ("🟥", "Прихована ведмежа дивергенція", "ціна — нижчий максимум, осцилятор — вищий"),
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ─────────────────────────── HTTP ───────────────────────────

def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "divergence-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─────────────────────────── BingX ───────────────────────────

def to_bingx_symbol(sym):
    sym = sym.upper().replace("-", "")
    for quote in ("USDT", "USDC"):
        if sym.endswith(quote):
            return f"{sym[:-len(quote)]}-{quote}"
    return sym


def parse_klines(data):
    """Повертає список свічок [(time, open, high, low, close, volume)] у порядку зростання часу.
    BingX віддає свічки або масивами, або об'єктами — обробляємо обидва варіанти."""
    rows = []
    for k in data or []:
        if isinstance(k, dict):
            t = k.get("time", k.get("openTime", k.get("t")))
            row = (int(t), float(k["open"]), float(k["high"]), float(k["low"]),
                   float(k["close"]), float(k.get("volume", 0) or 0))
        else:
            row = (int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                   float(k[5]) if len(k) > 5 else 0.0)
        rows.append(row)
    rows.sort(key=lambda r: r[0])
    # прибрати можливі дублікати часу
    uniq = {}
    for r in rows:
        uniq[r[0]] = r
    return [uniq[t] for t in sorted(uniq)]


def fetch_klines_market(symbol, tf, market):
    path = "/openApi/swap/v3/quote/klines" if market == "swap" else "/openApi/spot/v2/market/kline"
    params = urllib.parse.urlencode({
        "symbol": to_bingx_symbol(symbol), "interval": tf, "limit": CANDLES_LIMIT,
        "timestamp": int(time.time() * 1000),
    })
    last_err = None
    for host in BINGX_HOSTS:
        try:
            resp = http_get_json(f"{host}{path}?{params}")
            if isinstance(resp, dict) and resp.get("code", 0) not in (0, "0"):
                raise RuntimeError(f"BingX code {resp.get('code')}: {resp.get('msg')}")
            data = resp.get("data") if isinstance(resp, dict) else resp
            candles = parse_klines(data)
            if not candles:
                raise RuntimeError("порожня відповідь")
            return candles
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e  # мережева помилка — пробуємо резервний домен
            continue
    raise RuntimeError(f"мережа: {last_err}")


def fetch_klines(symbol, tf):
    markets = [MARKET] + [m for m in ("swap", "spot") if m != MARKET]
    errors = []
    for m in markets:
        try:
            return fetch_klines_market(symbol, tf, m), m
        except Exception as e:
            errors.append(f"{m}: {e}")
    raise RuntimeError("; ".join(errors))


def drop_unclosed(candles, tf):
    now_ms = int(time.time() * 1000)
    step = TF_MS[tf]
    return [c for c in candles if c[0] + step <= now_ms]


# ─────────────────────────── Індикатори (як у TradingView) ───────────────────────────

def sma(src, n):
    out = [None] * len(src)
    for i in range(len(src)):
        if i + 1 >= n:
            window = src[i - n + 1:i + 1]
            if all(v is not None for v in window):
                out[i] = sum(window) / n
    return out


def rma(src, n):
    """ta.rma: старт із SMA, далі alpha = 1/n."""
    out = [None] * len(src)
    prev = None
    buf = []
    for i, v in enumerate(src):
        if v is None:
            continue
        if prev is None:
            buf.append(v)
            if len(buf) == n:
                prev = sum(buf) / n
                out[i] = prev
        else:
            prev = (v + (n - 1) * prev) / n
            out[i] = prev
    return out


def ema(src, n):
    """ta.ema: старт із SMA, далі alpha = 2/(n+1)."""
    out = [None] * len(src)
    alpha = 2 / (n + 1)
    prev = None
    buf = []
    for i, v in enumerate(src):
        if v is None:
            continue
        if prev is None:
            buf.append(v)
            if len(buf) == n:
                prev = sum(buf) / n
                out[i] = prev
        else:
            prev = alpha * v + (1 - alpha) * prev
            out[i] = prev
    return out


def rsi(src, n):
    up, down = [None], [None]
    for i in range(1, len(src)):
        ch = src[i] - src[i - 1]
        up.append(max(ch, 0.0))
        down.append(max(-ch, 0.0))
    ru, rd = rma(up, n), rma(down, n)
    out = [None] * len(src)
    for i in range(len(src)):
        if ru[i] is None or rd[i] is None:
            continue
        if rd[i] == 0:
            out[i] = 100.0
        elif ru[i] == 0:
            out[i] = 0.0
        else:
            out[i] = 100 - 100 / (1 + ru[i] / rd[i])
    return out


def macd_hist(src, fast, slow, signal):
    f, s = ema(src, fast), ema(src, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    sig = ema(line, signal)
    return [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]


def stoch(close, high, low, n, smooth):
    k = [None] * len(close)
    for i in range(n - 1, len(close)):
        hh, ll = max(high[i - n + 1:i + 1]), min(low[i - n + 1:i + 1])
        k[i] = 100 * (close[i] - ll) / (hh - ll) if hh != ll else 0.0
    return sma(k, smooth)


def cci(src, n):
    ma = sma(src, n)
    out = [None] * len(src)
    for i in range(len(src)):
        if ma[i] is None:
            continue
        window = src[i - n + 1:i + 1]
        dev = sum(abs(v - ma[i]) for v in window) / n
        out[i] = (src[i] - ma[i]) / (0.015 * dev) if dev != 0 else 0.0
    return out


def mfi(hlc3, volume, n):
    pos, neg = [0.0], [0.0]
    for i in range(1, len(hlc3)):
        ch = hlc3[i] - hlc3[i - 1]
        pos.append(volume[i] * hlc3[i] if ch > 0 else 0.0)
        neg.append(volume[i] * hlc3[i] if ch < 0 else 0.0)
    out = [None] * len(hlc3)
    for i in range(n, len(hlc3)):
        up, dn = sum(pos[i - n + 1:i + 1]), sum(neg[i - n + 1:i + 1])
        out[i] = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
    return out


def obv(close, volume):
    out, acc = [0.0], 0.0
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            acc += volume[i]
        elif close[i] < close[i - 1]:
            acc -= volume[i]
        out.append(acc)
    return out


def momentum(src, n):
    return [src[i] - src[i - n] if i >= n else None for i in range(len(src))]


def oscillator(candles):
    o = [c[1] for c in candles]
    h = [c[2] for c in candles]
    l = [c[3] for c in candles]
    c = [c[4] for c in candles]
    v = [c_[5] for c_ in candles]
    name = OSCILLATOR.upper()
    if name == "RSI":
        return rsi(c, LENGTH), f"RSI({LENGTH})"
    if name == "MACD":
        return macd_hist(c, MACD_FAST, MACD_SLOW, MACD_SIGNAL), f"MACD hist({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL})"
    if name == "STOCHASTIC":
        return stoch(c, h, l, LENGTH, STOCH_SMOOTH), f"Stoch({LENGTH},{STOCH_SMOOTH})"
    if name == "CCI":
        return cci(c, LENGTH), f"CCI({LENGTH})"
    if name == "MFI":
        return mfi([(a + b + d) / 3 for a, b, d in zip(h, l, c)], v, LENGTH), f"MFI({LENGTH})"
    if name == "OBV":
        return obv(c, v), "OBV"
    if name == "MOMENTUM":
        return momentum(c, LENGTH), f"Momentum({LENGTH})"
    raise ValueError(f"Невідомий осцилятор: {OSCILLATOR}")


# ─────────────────────────── Пошук дивергенцій ───────────────────────────

def is_pivot(osc, i, left, right, kind):
    v = osc[i]
    if v is None:
        return False
    for j in range(i - left, i + right + 1):
        if j == i:
            continue
        w = osc[j]
        if w is None:
            return False
        if kind == "low":
            if (j < i and w <= v) or (j > i and w < v):
                return False
        else:
            if (j < i and w >= v) or (j > i and w > v):
                return False
    return True


def find_divergences(candles, osc):
    """Повертає список дивергенцій. Кожна — dict з індексами півотів і значеннями."""
    n = len(candles)
    lows = [c[3] for c in candles]
    highs = [c[2] for c in candles]
    result = []
    prev_low = None   # (index, osc, price)
    prev_high = None
    for i in range(PIVOT_LEFT, n - PIVOT_RIGHT):
        # --- мінімуми → бичачі ---
        if is_pivot(osc, i, PIVOT_LEFT, PIVOT_RIGHT, "low"):
            cur = (i, osc[i], lows[i])
            if prev_low is not None and RANGE_MIN <= i - prev_low[0] <= RANGE_MAX:
                kind = None
                if SHOW_REGULAR_BULL and cur[2] < prev_low[2] and cur[1] > prev_low[1]:
                    kind = "regular_bull"
                elif SHOW_HIDDEN_BULL and cur[2] > prev_low[2] and cur[1] < prev_low[1]:
                    kind = "hidden_bull"
                if kind:
                    result.append(dict(kind=kind, prev=prev_low, cur=cur, confirm=i + PIVOT_RIGHT))
            prev_low = cur
        # --- максимуми → ведмежі ---
        if is_pivot(osc, i, PIVOT_LEFT, PIVOT_RIGHT, "high"):
            cur = (i, osc[i], highs[i])
            if prev_high is not None and RANGE_MIN <= i - prev_high[0] <= RANGE_MAX:
                kind = None
                if SHOW_REGULAR_BEAR and cur[2] > prev_high[2] and cur[1] < prev_high[1]:
                    kind = "regular_bear"
                elif SHOW_HIDDEN_BEAR and cur[2] < prev_high[2] and cur[1] > prev_high[1]:
                    kind = "hidden_bear"
                if kind:
                    result.append(dict(kind=kind, prev=prev_high, cur=cur, confirm=i + PIVOT_RIGHT))
            prev_high = cur
    return result


# ─────────────────────────── Форматування ───────────────────────────

def fmt_time(ms):
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(ms / 1000, ZoneInfo("Europe/Kyiv"))
        return dt.strftime("%d.%m %H:%M") + " (Київ)"
    except Exception:
        return datetime.fromtimestamp(ms / 1000).strftime("%d.%m %H:%M")


def fmt_num(x):
    if x is None:
        return "—"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.2f}".replace(",", " ")
    if ax >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    if ax == 0:
        return "0"
    digits = max(4, -int(math.floor(math.log10(ax))) + 3)
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def build_message(symbol, tf, osc_name, market, candles, d):
    emoji, title, expl = DIV_INFO[d["kind"]]
    p_i, p_osc, p_price = d["prev"]
    c_i, c_osc, c_price = d["cur"]
    last_close = candles[-1][4]
    tv_symbol = f"BINGX:{symbol.upper()}{'.P' if market == 'swap' else ''}"
    tv_url = "https://www.tradingview.com/chart/?symbol=" + urllib.parse.quote(tv_symbol)
    market_name = "ф'ючерси" if market == "swap" else "спот"
    return (
        f"{emoji} <b>{title}</b>\n"
        f"<b>{symbol}</b> · {tf} · {osc_name} · {market_name}\n"
        f"<i>{expl}</i>\n\n"
        f"Ціна: {fmt_num(p_price)} → {fmt_num(c_price)}\n"
        f"Осцилятор: {fmt_num(p_osc)} → {fmt_num(c_osc)}\n"
        f"Півоти: {fmt_time(candles[p_i][0])} → {fmt_time(candles[c_i][0])}\n"
        f"Поточна ціна: {fmt_num(last_close)}\n\n"
        f'<a href="{tv_url}">Відкрити графік у TradingView</a>'
    )


# ─────────────────────────── Telegram ───────────────────────────

def tg_credentials():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip() or TELEGRAM_TOKEN_FALLBACK.strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or TELEGRAM_CHAT_ID_FALLBACK.strip()
    return token, chat


def send_telegram(text):
    token, chat = tg_credentials()
    if not token or not chat:
        raise RuntimeError("Не задано TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            resp = http_post_json(url, payload)
            if resp.get("ok"):
                return
            raise RuntimeError(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code == 429:
                try:
                    wait = json.loads(body)["parameters"]["retry_after"]
                except Exception:
                    wait = 5
                time.sleep(wait + 1)
                continue
            raise RuntimeError(f"Telegram HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            if attempt == 2:
                raise RuntimeError(f"Telegram мережа: {e}")
            time.sleep(3)
    raise RuntimeError("Telegram: не вдалося надіслати після 3 спроб")


# ─────────────────────────── Стан (щоб не було дублів) ───────────────────────────

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("sent", {})
            return st
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": {}}


def save_state(state):
    cutoff = int(time.time()) - 30 * 86400
    state["sent"] = {k: v for k, v in state["sent"].items() if v >= cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ─────────────────────────── Основний цикл ───────────────────────────

def run_once(dry_run=False, history=False):
    state = load_state()
    sent_now = 0
    errors = []
    for tf in TIMEFRAMES:
        if tf not in TF_MS:
            log(f"Невідомий таймфрейм {tf}, пропускаю")
            continue
        for symbol in SYMBOLS:
            try:
                candles, market = fetch_klines(symbol, tf)
            except Exception as e:
                errors.append(f"{symbol} {tf}: {e}")
                log(f"ПОМИЛКА {symbol} {tf}: {e}")
                continue
            candles = drop_unclosed(candles, tf)
            if len(candles) < PIVOT_LEFT + PIVOT_RIGHT + LENGTH + 10:
                log(f"{symbol} {tf}: замало свічок ({len(candles)})")
                continue
            osc, osc_name = oscillator(candles)
            divs = find_divergences(candles, osc)
            last = len(candles) - 1
            window = 150 if history else ALERT_LOOKBACK_BARS
            fresh = [d for d in divs if d["confirm"] > last - window]
            for d in fresh:
                key = f"{symbol}|{tf}|{OSCILLATOR}|{d['kind']}|{candles[d['cur'][0]][0]}"
                if history:
                    _, title, _ = DIV_INFO[d["kind"]]
                    print(f"{symbol:12} {tf:4} {title:32} півоти {fmt_time(candles[d['prev'][0]][0])} → "
                          f"{fmt_time(candles[d['cur'][0]][0])}, підтверджено {fmt_time(candles[d['confirm']][0] + TF_MS[tf])}")
                    continue
                if key in state["sent"]:
                    continue
                msg = build_message(symbol, tf, osc_name, market, candles, d)
                if dry_run:
                    print("─" * 50 + "\n" + msg)
                else:
                    send_telegram(msg)
                    time.sleep(0.5)
                state["sent"][key] = int(time.time())
                sent_now += 1
            time.sleep(0.15)  # не перевантажувати API

    total = len(TIMEFRAMES) * len(SYMBOLS)
    if not history:
        log(f"Готово: нових сигналів {sent_now}, помилок {len(errors)} з {total}")

    # Якщо дані не вдалося отримати взагалі — повідомити (не частіше разу на 6 год)
    if errors and len(errors) == total and not dry_run and not history:
        now = int(time.time())
        if now - state.get("last_error_notice", 0) > 6 * 3600:
            try:
                send_telegram("⚠️ <b>Бот дивергенцій не може отримати дані з BingX.</b>\n"
                              f"<code>{html.escape(errors[0][:300])}</code>")
                state["last_error_notice"] = now
            except Exception as e:
                log(f"Не вдалося надіслати попередження: {e}")
        if not dry_run:
            save_state(state)
        return False

    if not dry_run and not history:
        save_state(state)
    return True


def main():
    args = sys.argv[1:]
    if "--test" in args:
        send_telegram("✅ Бот дивергенцій підключено. Сигнали приходитимуть сюди.\n"
                      f"Пари: {len(SYMBOLS)}, таймфрейми: {', '.join(TIMEFRAMES)}, осцилятор: {OSCILLATOR}")
        log("Тестове повідомлення надіслано")
        return
    if "--loop" in args:
        idx = args.index("--loop")
        minutes = int(args[idx + 1]) if idx + 1 < len(args) and args[idx + 1].isdigit() else 15
        log(f"Безперервний режим: перевірка кожні {minutes} хв. Зупинити — Ctrl+C")
        while True:
            try:
                run_once()
            except Exception as e:
                log(f"Помилка циклу: {e}")
            # прокидатися одразу після закриття свічки + 20 с
            step = minutes * 60
            sleep_for = step - (time.time() % step) + 20
            time.sleep(sleep_for)
    ok = run_once(dry_run="--dry-run" in args, history="--history" in args)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
