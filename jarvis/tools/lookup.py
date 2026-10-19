"""Live information the model answers from: time, battery, weather, stock prices and web results."""
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, quote_plus
from urllib.request import Request, urlopen

import psutil

YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search?quotesCount=1&newsCount=0&q={}"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1d&interval=1d"
# Open-Meteo: free, no API key
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&name={}"
FORECAST = ("https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}&timezone=auto&forecast_days=7"
            "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max")
WEATHER_CODES = {   # WMO codes, as Open-Meteo reports them
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


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


def get_weather(city: str) -> str:
    """Get the current weather and the forecast for the next 7 days in a city.

    Args:
      city: The city name, for example 'Milan'
    """
    try:
        # the geocoder finds 'Milan' but not 'Milan, Italy', so fall back to the part before the comma
        places = _get_json(GEOCODE.format(quote_plus(city))).get("results") \
            or _get_json(GEOCODE.format(quote_plus(city.split(",")[0]))).get("results")
        if not places:
            return f"No place called {city} found."
        p = places[0]
        w = _get_json(FORECAST.format(p["latitude"], p["longitude"]))
        sky = lambda code: WEATHER_CODES.get(code, "unknown weather")
        now, daily = w["current"], w["daily"]
        lines = [f"Weather in {p['name']}, {p.get('country', '')} now: {now['temperature_2m']:.0f} degrees "
                 f"(feels like {now['apparent_temperature']:.0f} degrees), {sky(now['weather_code'])}, "
                 f"wind {now['wind_speed_10m']:.0f} km/h."]
        for i, day in enumerate(daily["time"]):
            name = ("Today", "Tomorrow")[i] if i < 2 else datetime.date.fromisoformat(day).strftime("%A")
            rain = daily["precipitation_probability_max"][i]
            lines.append(f"{name}: {sky(daily['weather_code'][i])}, {daily['temperature_2m_min'][i]:.0f} to "
                         f"{daily['temperature_2m_max'][i]:.0f} degrees" + (f", {rain}% chance of rain" if rain is not None else ""))
        return "\n".join(lines)
    except Exception as e:
        return f"Couldn't get the weather: {e}"


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
    """Look up current or factual information on the internet (news, sports, recent events) and get text back to answer from.

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
