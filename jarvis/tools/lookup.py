"""Live information the model answers from: time, battery, stock prices and web results."""
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, quote_plus
from urllib.request import Request, urlopen

import psutil

YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search?quotesCount=1&newsCount=0&q={}"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1d&interval=1d"


def _get_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def get_time() -> str:
    """Get the current local date and time."""
    return datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")


def battery_status() -> str:
    """Get the laptop battery percentage and whether it is charging."""
    b = psutil.sensors_battery()
    if b is None:
        return "No battery found."
    state = "charging" if b.power_plugged else "on battery"
    return f"{b.percent:.0f}% and {state}."


def get_stock_price(company: str) -> str:
    """Get the live stock price of a company.

    Args:
      company: The company name or ticker symbol, for example 'apple' or 'AAPL'
    """
    try:
        found = _get_json(YAHOO_SEARCH.format(quote_plus(company)))["quotes"]
        if not found:
            return f"No stock found for {company}."
        symbol = found[0]["symbol"]
        meta = _get_json(YAHOO_CHART.format(quote(symbol)))["chart"]["result"][0]["meta"]
        price, prev = meta["regularMarketPrice"], meta.get("chartPreviousClose")
        when = datetime.datetime.fromtimestamp(meta["regularMarketTime"]).strftime("%A %H:%M")
        line = f"{meta.get('longName', symbol)} ({symbol}) is at {price:.2f} {meta.get('currency', '')}"
        if prev:
            line += f", {(price - prev) / prev * 100:+.2f}% today"
        return line + f", as of {when}."
    except Exception as e:
        return f"Couldn't get the stock price: {e}"


def web_lookup(query: str) -> str:
    """Look up current or factual information on the internet (news, sports, weather, recent events) and get text back to answer from.

    Args:
      query: What to look up
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return "Web lookup isn't installed (pip install ddgs)."
    # news results carry actual recent content, plain web results fill in facts; fetch both at once
    with ThreadPoolExecutor(2) as pool:
        news = pool.submit(DDGS().news, query, max_results=3)
        text = pool.submit(DDGS().text, query, max_results=3)
    lines = []
    try:
        lines += [f"- [{r.get('date', '')[:10]}] {r['title']}: {r['body']}" for r in news.result()]
    except Exception:
        pass
    try:
        lines += [f"- {r['title']}: {r['body']}" for r in text.result()]
    except Exception:
        pass
    if not lines:
        return "No results found."
    today = datetime.date.today().isoformat()
    return (f"Live web results for '{query}' (today is {today}). "
            "Answer the user directly from these:\n" + "\n".join(lines))
