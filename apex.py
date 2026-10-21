import os
import logging
import threading
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8798349889:AAFIhfwSs9qbD8BlU9zrq20Y3BHfbPlxJII"
AUTHORIZED_USER_ID = 8798349889
FRED_API_KEY = "6866b9232de4f451a032868ebf67f783"

STATE = {
    "sge_cny_per_gram": None
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ==========================================
# 1. FRED MACRO DATA ENGINE (Official API)
# ==========================================
def fetch_fred_series(series_id: str):
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit=10"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        # Filter out missing data points "."
        valid_obs = [o for o in data.get("observations", []) if o.get("value") != "."]
        
        if len(valid_obs) < 2:
            return {"latest": 0.0, "prev": 0.0, "change": 0.0, "trend": "No Data", "date": "N/A"}

        latest_val = float(valid_obs[0]["value"])
        prev_val = float(valid_obs[min(4, len(valid_obs)-1)]["value"])
        change = latest_val - prev_val

        if change > 0.02:
            trend = "Rising (Bearish Pressure)"
        elif change < -0.02:
            trend = "Falling (Bullish Tailwind)"
        else:
            trend = "Flat / Neutral"

        return {
            "latest": latest_val,
            "prev": prev_val,
            "change": change,
            "trend": trend,
            "date": valid_obs[0]["date"]
        }
    except Exception as e:
        logging.error(f"FRED API error for {series_id}: {e}")
        return {"latest": 0.0, "prev": 0.0, "change": 0.0, "trend": "API Error", "date": "N/A"}


# ==========================================
# 2. XAU CROSSES, DXY & 20 / 30 / 50 MAs
# ==========================================
def fetch_market_and_ma_data():
    tickers = {
        "gold": "GC=F",
        "eurusd": "EURUSD=X",
        "usdjpy": "JPY=X",
        "usdcny": "CNY=X",
        "dxy": "DX-Y.NYB"
    }

    data = {}
    for key, symbol in tickers.items():
        df = yf.Ticker(symbol).history(period="6mo", interval="1d")
        if df.empty:
            raise ValueError(f"Could not retrieve market data for {symbol}")
        data[key] = df

    gold_df = data["gold"].copy()
    eur_df = data["eurusd"].copy()
    jpy_df = data["usdjpy"].copy()
    cny_df = data["usdcny"].copy()
    dxy_df = data["dxy"].copy()

    gold_df["SMA_20"] = gold_df["Close"].rolling(window=20).mean()
    gold_df["SMA_30"] = gold_df["Close"].rolling(window=30).mean()
    gold_df["SMA_50"] = gold_df["Close"].rolling(window=50).mean()

    gold_df["EMA_20"] = gold_df["Close"].ewm(span=20, adjust=False).mean()
    gold_df["EMA_30"] = gold_df["Close"].ewm(span=30, adjust=False).mean()
    gold_df["EMA_50"] = gold_df["Close"].ewm(span=50, adjust=False).mean()

    xau_usd = float(gold_df["Close"].iloc[-1])
    xau_usd_prev = float(gold_df["Close"].iloc[-2])

    sma20 = float(gold_df["SMA_20"].iloc[-1])
    sma30 = float(gold_df["SMA_30"].iloc[-1])
    sma50 = float(gold_df["SMA_50"].iloc[-1])

    ema20 = float(gold_df["EMA_20"].iloc[-1])
    ema30 = float(gold_df["EMA_30"].iloc[-1])
    ema50 = float(gold_df["EMA_50"].iloc[-1])

    eur_usd = float(eur_df["Close"].iloc[-1])
    eur_usd_prev = float(eur_df["Close"].iloc[-2])
    usd_jpy = float(jpy_df["Close"].iloc[-1])
    usd_jpy_prev = float(jpy_df["Close"].iloc[-2])
    usd_cny = float(cny_df["Close"].iloc[-1])

    xau_eur = xau_usd / eur_usd
    xau_eur_prev = xau_usd_prev / eur_usd_prev

    xau_jpy = xau_usd * usd_jpy
    xau_jpy_prev = xau_usd_prev * usd_jpy_prev

    dxy_now = float(dxy_df["Close"].iloc[-1])
    dxy_ma20 = float(dxy_df["Close"].rolling(20).mean().iloc[-1])
    dxy_bullish = dxy_now > dxy_ma20
    dxy_trend = "Bullish (Gold Headwind)" if dxy_bullish else "Bearish (Gold Tailwind)"

    if xau_usd > sma20 and sma20 > sma30 and sma30 > sma50:
        ma_bias = "Strong Institutional Bullish (Price above 20, 30, 50)"
        ma_score = 20
    elif xau_usd > sma20 and xau_usd > sma50:
        ma_bias = "Constructive Bullish (Holding Above 20 and 50 MA)"
        ma_score = 15
    elif xau_usd < sma20 and sma20 < sma30 and sma30 < sma50:
        ma_bias = "Strong Institutional Bearish (Price below 20, 30, 50)"
        ma_score = 0
    elif xau_usd < sma20 and xau_usd > sma50:
        ma_bias = "Bullish Pullback into 30/50 MA Institutional Zone"
        ma_score = 12
    else:
        ma_bias = "Consolidation / Chop Inside 20-30-50 MA Ribbon"
        ma_score = 8

    bsl_high = float(gold_df["High"].tail(5).max())
    ssl_low = float(gold_df["Low"].tail(5).min())

    return {
        "xau_usd": xau_usd,
        "xau_usd_chg": ((xau_usd - xau_usd_prev) / xau_usd_prev) * 100,
        "xau_eur": xau_eur,
        "xau_eur_chg": ((xau_eur - xau_eur_prev) / xau_eur_prev) * 100,
        "xau_jpy": xau_jpy,
        "xau_jpy_chg": ((xau_jpy - xau_jpy_prev) / xau_jpy_prev) * 100,
        "usd_cny": usd_cny,
        "dxy": dxy_now,
        "dxy_trend": dxy_trend,
        "dxy_bullish": dxy_bullish,
        "sma20": sma20,
        "sma30": sma30,
        "sma50": sma50,
        "ema20": ema20,
        "ema30": ema30,
        "ema50": ema50,
        "ma_bias": ma_bias,
        "ma_score": ma_score,
        "bsl": bsl_high,
        "ssl": ssl_low
    }


# ==========================================
# 3. SHANGHAI GOLD PREMIUM (SGE SPREAD)
# ==========================================
def calculate_shanghai_spread(xau_usd: float, usd_cny: float):
    troy_oz_grams = 31.1034768
    if STATE["sge_cny_per_gram"] is not None:
        sge_cny_g = STATE["sge_cny_per_gram"]
        sge_usd_oz = (sge_cny_g * troy_oz_grams) / usd_cny
        spread = sge_usd_oz - xau_usd
        source_note = f"Live SGE Print ({sge_cny_g:.2f} CNY/g)"
    else:
        implied_cny_g = (xau_usd * usd_cny) / troy_oz_grams
        sge_cny_g = implied_cny_g + 3.5
        sge_usd_oz = (sge_cny_g * troy_oz_grams) / usd_cny
        spread = sge_usd_oz - xau_usd
        source_note = "Baseline Proxy (Update exact SGE via /set_sge)"

    if spread > 25:
        status = "Strong Physical Buying (Over +$25/oz Macro Bullish)"
        score = 20
    elif spread >= 0:
        status = "Neutral ($0 to +$25/oz Standard Import Spread)"
        score = 10
    else:
        status = "Discount (Below $0/oz Weak Physical Demand)"
        score = 0

    return {
        "spread": spread,
        "sge_usd_oz": sge_usd_oz,
        "status": status,
        "score": score,
        "source_note": source_note
    }


# ==========================================
# 4. THERMOMETER OF GOLD & CHEAT CODE ENGINE
# ==========================================
def build_thermometer_and_cheat_code(fred_data, mkt, sge):
    score = 0

    if fred_data["real_10y"]["change"] < -0.02:
        score += 25
    elif fred_data["real_10y"]["change"] <= 0.02:
        score += 12

    score += sge["score"]

    bullish_crosses = sum([
        mkt["xau_usd_chg"] > 0,
        mkt["xau_eur_chg"] > 0,
        mkt["xau_jpy_chg"] > 0
    ])
    if bullish_crosses == 3:
        score += 20
    elif bullish_crosses == 2:
        score += 14
    elif bullish_crosses == 1:
        score += 7

    score += 15 if not mkt["dxy_bullish"] else 5
    score += mkt["ma_score"]

    if score >= 80:
        label = "EXTREME GOLD STRENGTH (Super Bullish)"
    elif score >= 60:
        label = "MODERATE STRENGTH (Bullish Bias)"
    elif score >= 41:
        label = "NEUTRAL / CHOP (Consolidation)"
    elif score >= 20:
        label = "BEARISH PRESSURE (Favor Shorts / Hedges)"
    else:
        label = "EXTREME WEAKNESS (Capitulation)"

    filled = int(round(score / 10))
    bar = "█" * filled + "░" * (10 - filled)

    cheat_codes = []
    if mkt["xau_usd_chg"] < 0 and (mkt["xau_eur_chg"] > 0 and mkt["xau_jpy_chg"] > 0):
        cheat_codes.append(
            "<b>Divergence Break Code:</b> XAU/USD is dipping while XAU/EUR & XAU/JPY are rising. "
            "Drop is USD-isolated; do NOT short XAU/USD."
        )
    if mkt["xau_usd_chg"] < -0.5 and sge["spread"] > 35:
        cheat_codes.append(
            "<b>Shanghai Floor Code:</b> Western paper drop while Shanghai Premium exceeds +$35/oz. "
            "Watch for Asian session physical absorption & mean-reversion long."
        )
    if fred_data["nom_10y"]["change"] > 0 and fred_data["breakeven_10y"]["change"] > fred_data["real_10y"]["change"]:
        cheat_codes.append(
            "<b>Real Yield Trap Code:</b> Nominal yields are rising, but Breakeven Inflation (T10YIE) is rising faster "
            "than Real Yields (DFII10). Net bullish for Gold."
        )

    if not cheat_codes:
        cheat_codes.append(
            "<b>Masterclass Structure Protocol (Days 16–30):</b> Follow the 20/30/50 MA ribbon bias. "
            "Wait for a 15-minute liquidity sweep of London/NY highs or lows before executing."
        )

    return score, label, bar, "\n• ".join(cheat_codes)


# ==========================================
# 5. TELEGRAM COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "<b>🏛 Apex Gold Intel Bot (MarketSentinelBot)</b>\n\n"
        "<b>Commands:</b>\n"
        "• /brief — Full Institutional Brief (FRED, Shanghai Premium, XAU/USD, XAU/EUR, XAU/JPY, 20/30/50 MAs, Thermometer & Cheat Codes)\n"
        "• /set_sge <code>630.50</code> — Set live Shanghai Au99.99 price in RMB/g\n"
        "• /curriculum — View Day 1 to Day 30 Masterclass Reference Matrix"
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)


async def curriculum_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syllabus = (
        "<b>📚 30-DAY GOLD MASTERCLASS MATRIX</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "• <b>Days 1–5 (Structural Foundations):</b> Tick values, Asian physical vs. London/NY overlap, liquidity pools.\n"
        "• <b>Days 6–10 (Macro Drivers & Rates):</b> 10Y TIPS Real Yields (DFII10), DGS10, T10YIE breakevens, Fed liquidity.\n"
        "• <b>Days 11–15 (Physical Arbitrage):</b> Shanghai SGE Premium/Discount vs. COMEX/LBMA paper markets.\n"
        "• <b>Days 16–20 (Order Flow & MAs):</b> 20/30/50 Moving Average ribbon, Order Blocks, FVGs, Swing Failure Patterns.\n"
        "• <b>Days 21–25 (Intermarket Crosses):</b> XAU/USD, XAU/EUR, XAU/JPY divergence & DXY correlation breaks.\n"
        "• <b>Days 26–30 (Execution & Cheat Codes):</b> CPI/NFP/FOMC 15-minute liquidity sweep rule & asymmetric R:R sizing."
    )
    await update.message.reply_text(syllabus, parse_mode=ParseMode.HTML)


async def set_sge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/set_sge 630.50</code> (Enter Shanghai Au99.99 price in CNY/gram)",
            parse_mode=ParseMode.HTML
        )
        return
    try:
        val = float(context.args[0])
        STATE["sge_cny_per_gram"] = val
        await update.message.reply_text(
            f"✅ Shanghai Gold Au99.99 updated to <b>{val:.2f} CNY/g</b>.\nRun /brief to recalculate the spread.",
            parse_mode=ParseMode.HTML
        )
    except ValueError:
        await update.message.reply_text("Invalid number. Example: <code>/set_sge 630.50</code>", parse_mode=ParseMode.HTML)


async def brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text(
        "⏳ Scanning FRED API Yields, Shanghai Spread, XAU Crosses & MAs..."
    )

    try:
        fred_data = {
            "real_10y": fetch_fred_series("DFII10"),
            "nom_10y": fetch_fred_series("DGS10"),
            "nom_2y": fetch_fred_series("DGS2"),
            "breakeven_10y": fetch_fred_series("T10YIE"),
        }

        mkt = fetch_market_and_ma_data()
        sge = calculate_shanghai_spread(mkt["xau_usd"], mkt["usd_cny"])
        score, label, bar, active_codes = build_thermometer_and_cheat_code(fred_data, mkt, sge)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        report = (
            f"<b>🏛 APEX GOLD INTEL — INSTITUTIONAL BRIEF</b>\n"
            f"<i>{now_str}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>1. THERMOMETER OF GOLD</b>\n"
            f"<b>[{bar}] {score}%</b>\n"
            f"Bias: <b>{label}</b>\n\n"

            f"<b>2. FRED MACRO & REAL YIELDS</b>\n"
            f"• <b>10Y Real Yield (DFII10):</b> {fred_data['real_10y']['latest']:.2f}% ({fred_data['real_10y']['trend']})\n"
            f"• <b>10Y Nominal (DGS10):</b> {fred_data['nom_10y']['latest']:.2f}%\n"
            f"• <b>2Y Nominal (DGS2):</b> {fred_data['nom_2y']['latest']:.2f}%\n"
            f"• <b>10Y Breakeven (T10YIE):</b> {fred_data['breakeven_10y']['latest']:.2f}%\n"
            f"• <b>DXY Index:</b> {mkt['dxy']:.2f} — {mkt['dxy_trend']}\n\n"

            f"<b>3. SHANGHAI GOLD PREMIUM (SGE)</b>\n"
            f"• <b>Spread vs Spot:</b> {sge['spread']:+.2f} USD/oz\n"
            f"• <b>Status:</b> {sge['status']}\n"
            f"• <i>{sge['source_note']}</i>\n\n"

            f"<b>4. PRECIOUS METALS CROSS BREADTH</b>\n"
            f"• <b>XAU/USD:</b> ${mkt['xau_usd']:,.2f} ({mkt['xau_usd_chg']:+.2f}%)\n"
            f"• <b>XAU/EUR:</b> €{mkt['xau_eur']:,.2f} ({mkt['xau_eur_chg']:+.2f}%)\n"
            f"• <b>XAU/JPY:</b> ¥{mkt['xau_jpy']:,.0f} ({mkt['xau_jpy_chg']:+.2f}%)\n\n"

            f"<b>5. INSTITUTIONAL 20 / 30 / 50 MA RIBBON</b>\n"
            f"• <b>20 MA (SMA / EMA):</b> ${mkt['sma20']:,.2f} / ${mkt['ema20']:,.2f}\n"
            f"• <b>30 MA (SMA / EMA):</b> ${mkt['sma30']:,.2f} / ${mkt['ema30']:,.2f}\n"
            f"• <b>50 MA (SMA / EMA):</b> ${mkt['sma50']:,.2f} / ${mkt['ema50']:,.2f}\n"
            f"• <b>Ribbon Bias:</b> {mkt['ma_bias']}\n\n"

            f"<b>6. CHEAT CODE IN PLAY</b>\n"
            f"• {active_codes}\n\n"

            f"<b>7. KEY STRUCTURAL LIQUIDITY (5-DAY)</b>\n"
            f"• <b>Buy-Side Liquidity (BSL):</b> ${mkt['bsl']:,.2f}\n"
            f"• <b>Sell-Side Liquidity (SSL):</b> ${mkt['ssl']:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )

        await status_msg.edit_text(report, parse_mode=ParseMode.HTML)

    except Exception as e:
        logging.exception("Error generating brief")
        await status_msg.edit_text(f"⚠️ Error generating brief: {e}")


async def post_init(application: Application):
    try:
        await application.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text="✅ <b>MarketSentinelBot is Live!</b>\nAPI integrated. Tap /brief for your institutional Gold & Macro scan.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logging.warning(f"Startup ping skipped: {e}")


# ==========================================
# RENDER HEALTH CHECK SERVER (KEEPS BOT ALIVE)
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"MarketSentinelBot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"Render health server listening on port {port}")
    server.serve_forever()


# ==========================================
# MAIN ENTRY POINT
# ==========================================
def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("brief", brief_command))
    app.add_handler(CommandHandler("set_sge", set_sge_command))
    app.add_handler(CommandHandler("curriculum", curriculum_command))

    print("MarketSentinelBot is running... Open Telegram and send /brief!")
    app.run_polling(drop_pending_updates=True)


# if __name__ == "__main__":
#     main()
# ==========================================
# 6. NEWS, SENTIMENT, CORRELATIONS & LEVELS EXTENSION
# ==========================================
import time
import html
import xml.etree.ElementTree as ET

NEWS_FEEDS = {
    "🟡 GOLD (XAU)": "https://news.google.com/rss/search?q=gold+price+OR+XAUUSD+OR+bullion+when:1d&hl=en-US&gl=US&ceid=US:en",
    "⚪ SILVER (XAG)": "https://news.google.com/rss/search?q=silver+price+OR+XAGUSD+OR+silver+market+when:1d&hl=en-US&gl=US&ceid=US:en",
    "📈 BOND YIELDS": "https://news.google.com/rss/search?q=treasury+yields+OR+bond+market+yield+OR+10-year+yield+when:1d&hl=en-US&gl=US&ceid=US:en",
    "🛢 CRUDE OIL (WTI/BRENT)": "https://news.google.com/rss/search?q=crude+oil+price+OR+WTI+crude+OR+Brent+oil+when:1d&hl=en-US&gl=US&ceid=US:en"
}

SEEN_NEWS_LINKS = set()

BULLISH_WORDS = ["surge", "soar", "jump", "rally", "record high", "gain", "bull", "safe-haven", "rate cut", "buying", "climb", "rise"]
BEARISH_WORDS = ["drop", "fall", "plunge", "slump", "slide", "hawkish", "sell-off", "selloff", "loss", "bear", "retreat", "dip", "pressure"]


def analyze_headline_sentiment(title: str) -> str:
    lower_t = title.lower()
    bull_hits = sum(1 for w in BULLISH_WORDS if w in lower_t)
    bear_hits = sum(1 for w in BEARISH_WORDS if w in lower_t)
    if bull_hits > bear_hits:
        return "🟢"
    elif bear_hits > bull_hits:
        return "🔴"
    return "⚪"


def fetch_rss_items(url: str, limit: int = 3):
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    items = []
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for item in root.findall("./channel/item")[:limit]:
            raw_title = item.findtext("title") or "No Title"
            link = item.findtext("link") or ""
            pub_date = item.findtext("pubDate") or ""
            short_date = " ".join(pub_date.split()[:4]) if pub_date else ""
            sentiment_icon = analyze_headline_sentiment(raw_title)
            items.append({
                "title": html.escape(raw_title),
                "link": html.escape(link),
                "date": html.escape(short_date),
                "sentiment": sentiment_icon
            })
    except Exception as e:
        logging.error(f"RSS error for {url}: {e}")
    return items


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text(
        "⏳ Scanning live institutional headlines & sentiment for Gold, Silver, Yields & Crude Oil..."
    )
    try:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sections = [
            f"<b>📰 MACRO & COMMODITIES NEWS WIRE</b>\n<i>{now_str}</i>\n"
            f"<i>Legend: 🟢 Bullish | 🔴 Bearish | ⚪ Neutral</i>\n━━━━━━━━━━━━━━━━━━━━━━"
        ]

        for category, url in NEWS_FEEDS.items():
            headlines = fetch_rss_items(url, limit=3)
            sec_lines = [f"\n<b>{category}</b>"]
            if not headlines:
                sec_lines.append("• <i>No recent headlines found.</i>")
            else:
                for h in headlines:
                    date_str = f" <i>({h['date']})</i>" if h["date"] else ""
                    sec_lines.append(f"{h['sentiment']} <a href=\"{h['link']}\">{h['title']}</a>{date_str}")
            sections.append("\n".join(sec_lines))

        sections.append("\n━━━━━━━━━━━━━━━━━━━━━━")
        await status_msg.edit_text(
            "\n".join(sections),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.exception("Error in /news command")
        await status_msg.edit_text(f"⚠️ Error fetching news: {e}")


async def correlations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text(
        "⏳ Calculating Gold/Silver Ratio, Crude Oil Inflation Pulse & Treasury Yield Matrix..."
    )
    try:
        symbols = {
            "gold": "GC=F",
            "silver": "SI=F",
            "oil": "CL=F",
            "tnx": "^TNX"
        }
        mkt = {}
        for k, sym in symbols.items():
            df = yf.Ticker(sym).history(period="1mo", interval="1d")
            if df.empty or len(df) < 2:
                raise ValueError(f"Missing data for {sym}")
            last = float(df["Close"].iloc[-1])
            prev = float(df["Close"].iloc[-2])
            chg = ((last - prev) / prev) * 100
            mkt[k] = {"price": last, "chg": chg}

        gs_ratio = mkt["gold"]["price"] / mkt["silver"]["price"]
        go_ratio = mkt["gold"]["price"] / mkt["oil"]["price"]

        if gs_ratio > 85:
            gs_note = "Silver Historically Undervalued vs. Gold (High Ratio)"
        elif gs_ratio < 70:
            gs_note = "Silver Outperforming (Industrial / Precious Expansion)"
        else:
            gs_note = "Balanced Historical Range (70–85)"

        if mkt["oil"]["chg"] > 1.0 and mkt["tnx"]["chg"] > 0.5:
            macro_pulse = "⚠️ Energy-Driven Yield Spike (Inflationary Headwind)"
        elif mkt["oil"]["chg"] > 0.5 and mkt["tnx"]["chg"] <= 0:
            macro_pulse = "🔥 Stagflation / Commodity Bid (Strong Gold Tailwind)"
        elif mkt["tnx"]["chg"] < -0.5:
            macro_pulse = "🟢 Yields Cooling (Bullish Duration & Bullion)"
        else:
            macro_pulse = "⚖️ Mixed Intermarket Flows"

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        report = (
            f"<b>🌐 INTERMARKET & COMMODITIES RADAR</b>\n"
            f"<i>{now_str}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>1. LIVE ASSET SNAPSHOT</b>\n"
            f"• <b>Gold (GC=F):</b> ${mkt['gold']['price']:,.2f} ({mkt['gold']['chg']:+.2f}%)\n"
            f"• <b>Silver (SI=F):</b> ${mkt['silver']['price']:,.2f} ({mkt['silver']['chg']:+.2f}%)\n"
            f"• <b>WTI Crude Oil (CL=F):</b> ${mkt['oil']['price']:,.2f} ({mkt['oil']['chg']:+.2f}%)\n"
            f"• <b>US 10Y Yield (^TNX):</b> {mkt['tnx']['price']:.2f}% ({mkt['tnx']['chg']:+.2f}%)\n\n"
            f"<b>2. INSTITUTIONAL RATIOS</b>\n"
            f"• <b>Gold/Silver Ratio (XAU/XAG):</b> {gs_ratio:.2f}x\n"
            f"  <i>→ {gs_note}</i>\n"
            f"• <b>Gold/Oil Ratio:</b> {go_ratio:.1f} barrels/oz\n\n"
            f"<b>3. CROSS-ASSET REGIME</b>\n"
            f"• {macro_pulse}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await status_msg.edit_text(report, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.exception("Error in /correlations command")
        await status_msg.edit_text(f"⚠️ Error calculating correlations: {e}")


async def levels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("⏳ Computing Gold ATR Volatility & Intraday Pivot Levels...")
    try:
        df = yf.Ticker("GC=F").history(period="1mo", interval="1d")
        if df.empty or len(df) < 15:
            raise ValueError("Insufficient historical bars for GC=F")

        # Calculate 14-day ATR
        high_low = df["High"] - df["Low"]
        high_close = (df["High"] - df["Close"].shift()).abs()
        low_close = (df["Low"] - df["Close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr_14 = float(tr.rolling(14).mean().iloc[-1])

        # Previous session HLC for Standard Floor Pivots
        prev_bar = df.iloc[-2]
        h = float(prev_bar["High"])
        l = float(prev_bar["Low"])
        c = float(prev_bar["Close"])
        curr_price = float(df["Close"].iloc[-1])

        pivot = (h + l + c) / 3.0
        r1 = (2 * pivot) - l
        s1 = (2 * pivot) - h
        r2 = pivot + (h - l)
        s2 = pivot - (h - l)

        atr_upper = curr_price + atr_14
        atr_lower = curr_price - atr_14

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        report = (
            f"<b>🎯 XAU/USD INTRADAY PIVOTS & ATR MATRIX</b>\n"
            f"<i>{now_str} | Spot Ref: ${curr_price:,.2f}</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>1. STANDARD FLOOR PIVOTS</b>\n"
            f"• <b>Resistance 2 (R2):</b> ${r2:,.2f}\n"
            f"• <b>Resistance 1 (R1):</b> ${r1:,.2f}\n"
            f"• <b>Central Pivot (P):</b> <b>${pivot:,.2f}</b>\n"
            f"• <b>Support 1 (S1):</b> ${s1:,.2f}\n"
            f"• <b>Support 2 (S2):</b> ${s2:,.2f}\n\n"
            f"<b>2. 14-DAY ATR VOLATILITY BAND</b>\n"
            f"• <b>Daily ATR (14):</b> ±${atr_14:,.2f}\n"
            f"• <b>Upper Exhaustion (+1 ATR):</b> ${atr_upper:,.2f}\n"
            f"• <b>Lower Exhaustion (-1 ATR):</b> ${atr_lower:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await status_msg.edit_text(report, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.exception("Error in /levels command")
        await status_msg.edit_text(f"⚠️ Error computing levels: {e}")


def auto_news_alert_worker():
    """Background thread that checks for breaking news every 30 minutes and sends alerts."""
    for url in NEWS_FEEDS.values():
        for item in fetch_rss_items(url, limit=5):
            if item["link"]:
                SEEN_NEWS_LINKS.add(item["link"])

    while True:
        time.sleep(1800)
        for category, url in NEWS_FEEDS.items():
            latest_items = fetch_rss_items(url, limit=2)
            for item in latest_items:
                if item["link"] and item["link"] not in SEEN_NEWS_LINKS:
                    SEEN_NEWS_LINKS.add(item["link"])
                    msg = (
                        f"🚨 <b>BREAKING {category} ALERT</b>\n\n"
                        f"{item['sentiment']} <a href=\"{item['link']}\">{item['title']}</a>\n"
                        f"<i>{item['date']}</i>"
                    )
                    try:
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": AUTHORIZED_USER_ID,
                                "text": msg,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True
                            },
                            timeout=10
                        )
                    except Exception as e:
                        logging.error(f"Failed to send auto news alert: {e}")


# Hook all new commands into the bot without altering original code above
_original_builder_build = Application.builder

def _patched_builder():
    builder = _original_builder_build()
    orig_build = builder.build
    def _build_with_extensions():
        app = orig_build()
        app.add_handler(CommandHandler("news", news_command))
        app.add_handler(CommandHandler("correlations", correlations_command))
        app.add_handler(CommandHandler("levels", levels_command))
        return app
    builder.build = _build_with_extensions
    return builder

Application.builder = staticmethod(_patched_builder)

if __name__ == "__main__":
    threading.Thread(target=auto_news_alert_worker, daemon=True).start()
    main()
