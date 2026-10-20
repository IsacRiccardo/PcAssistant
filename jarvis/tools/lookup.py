"""Live information the model answers from: time, battery, weather, stock prices and web results."""
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, quote_plus
from urllib.request import Request, urlopen

import psutil

from ..phrases import words

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


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEK_WORDS = {"week", "weekly", "forecast", "days"}
DAY_WORDS = {*WEEKDAYS, *WEEK_WORDS, "today", "tonight", "tomorrow", "weekend"}
# words a follow-up that only changes the day may hold besides the day: 'and what about on friday?'
FOLLOW_UP_WORDS = {"and", "what", "what's", "whats", "about", "how", "how's", "hows", "on", "the", "for", "this",
                   "next", "then", "so", "ok", "okay", "is", "it", "will", "be", "like", "there", "day", "after"}


def _days_asked(request: str, dates: list[str]) -> list[int] | None:
    """Which forecast days the request is about, as indexes into dates (today first); None for
    right now and today. Small models read out every line they're given, so they get only these."""
    asked = set(words(request).split())
    names = [datetime.date.fromisoformat(d).strftime("%A").lower() for d in dates]
    days = [i for i, name in enumerate(names) if name in asked]
    if days:
        return days
    if "weekend" in asked:
        return [i for i, name in enumerate(names) if name in ("saturday", "sunday")][:2]
    if "tomorrow" in asked:
        return [1]
    if asked & WEEK_WORDS:
        return list(range(len(dates)))
    return None


class Weather:
    def __init__(self):
        self.request = ""   # what the user just said, set by Tools.begin
        self.city = None    # the last city asked about

    def get_weather(self, city: str) -> str:
        """Get the weather in a city: right now, or the forecast for the days the user asks about.

        Args:
          city: The city name, for example 'Milan'
        """
        self.city = city
        return _weather(city, self.request)

    def again(self, request: str, also: set[str]) -> dict | None:
        """The arguments to get the weather again for a follow-up that only changes the day, like
        'and tomorrow?' or 'what about Friday?': the last city. None for anything else.
        also: more words such a follow-up may hold, like 'rain' in 'will it rain on Friday?'."""
        asked = set(words(request).split())
        if self.city and asked & DAY_WORDS and not asked - DAY_WORDS - FOLLOW_UP_WORDS - also:
            return {"city": self.city}
        return None


def _weather(city: str, request: str) -> str:
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

        def day(i: int) -> str:
            name = datetime.date.fromisoformat(daily["time"][i]).strftime("%A")
            if i < 2:   # 'Tuesday (today)': with the real name too, small models don't mislabel the days
                name += (" (today)", " (tomorrow)")[i]
            rain = daily["precipitation_probability_max"][i]
            return (f"{name}: {sky(daily['weather_code'][i])}, {daily['temperature_2m_min'][i]:.0f} to "
                    f"{daily['temperature_2m_max'][i]:.0f} degrees" + (f", {rain}% chance of rain" if rain is not None else ""))

        # only the days asked about: small models read out every line they're given
        place = f"{p['name']}, {p.get('country', '')}"
        days = _days_asked(request, daily["time"])
        if days and len(days) == len(daily["time"]):
            return "\n".join([f"Forecast for {place}:", *map(day, days), "Sum up the week in two or three sentences."])
        if days:
            return "\n".join([f"Forecast for {place}:", *map(day, days), "Answer in one or two sentences."])
        return "\n".join([f"Weather in {place} now: {now['temperature_2m']:.0f} degrees (feels like "
                          f"{now['apparent_temperature']:.0f} degrees), {sky(now['weather_code'])}, "
                          f"wind {now['wind_speed_10m']:.0f} km/h.", day(0), "Answer in one or two sentences."])
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
