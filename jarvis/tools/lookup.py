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

# Weather from Open-Meteo: free, no API key
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
# Words for the days a weather request is about
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEK_WORDS = {"week", "weekly", "forecast", "days"}
DAY_WORDS = {*WEEKDAYS, *WEEK_WORDS, "today", "tonight", "tomorrow", "weekend"}
# words a follow-up that only changes the day may hold besides the day: 'and what about on friday?'
FOLLOW_UP_WORDS = {"and", "what", "what's", "whats", "about", "how", "how's", "hows", "on", "the", "for", "this",
                   "next", "then", "so", "ok", "okay", "is", "it", "will", "be", "like", "there", "day", "after"}


def _get_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def get_time() -> str:
    """Get the current local date and time."""
    return datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")


def battery_status() -> str:
    """Get the laptop battery percentage and whether it is charging."""
    battery = psutil.sensors_battery()
    if battery is None:
        return "No battery found."
    state = "charging" if battery.power_plugged else "on battery"
    return f"{battery.percent:.0f}% and {state}."


# ---------------------------------------------------------------- weather

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

    def follow_up_args(self, request: str, also: set[str]) -> dict | None:
        """The arguments to get the weather again for a follow-up that only changes the day, like
        'and tomorrow?' or 'what about Friday?': the last city. None for anything else.
        also: more words such a follow-up may hold, like 'rain' in 'will it rain on Friday?'."""
        asked = set(words(request).split())
        if self.city and asked & DAY_WORDS and not asked - DAY_WORDS - FOLLOW_UP_WORDS - also:
            return {"city": self.city}
        return None


def _weather(city: str, request: str) -> str:
    try:
        return _weather_report(city, request)
    except Exception as e:
        return f"Couldn't get the weather: {e}"


def _weather_report(city: str, request: str) -> str:
    """The weather for the days the request is about. Only those: small models read out every line
    they're given."""
    place = _find_place(city)
    if place is None:
        return f"No place called {city} found."
    forecast = _get_json(FORECAST.format(place["latitude"], place["longitude"]))
    where = f"{place['name']}, {place.get('country', '')}"
    now, daily = forecast["current"], forecast["daily"]
    days = _days_asked(request, daily["time"])
    if days is None:   # right now, and today
        return "\n".join([f"Weather in {where} now: {now['temperature_2m']:.0f} degrees (feels like "
                          f"{now['apparent_temperature']:.0f} degrees), {_sky(now['weather_code'])}, "
                          f"wind {now['wind_speed_10m']:.0f} km/h.",
                          _describe_day(daily, 0), "Answer in one or two sentences."])
    whole_week = len(days) == len(daily["time"])
    return "\n".join([f"Forecast for {where}:", *(_describe_day(daily, i) for i in days),
                      "Sum up the week in two or three sentences." if whole_week else "Answer in one or two sentences."])


def _find_place(city: str) -> dict | None:
    """The place Open-Meteo knows by this name, with its latitude and longitude."""
    found = _get_json(GEOCODE.format(quote_plus(city))).get("results")
    if not found:   # the geocoder finds 'Milan' but not 'Milan, Italy', so try the part before the comma
        found = _get_json(GEOCODE.format(quote_plus(city.split(",")[0]))).get("results")
    return found[0] if found else None


def _sky(code: int) -> str:
    return WEATHER_CODES.get(code, "unknown weather")


def _describe_day(daily: dict, i: int) -> str:
    """One day of the forecast, like 'Tuesday (today): light rain, 8 to 15 degrees, 83% chance of rain'.
    With the real name next to 'today' and 'tomorrow', small models don't mislabel the days."""
    name = datetime.date.fromisoformat(daily["time"][i]).strftime("%A")
    if i < 2:
        name += (" (today)", " (tomorrow)")[i]
    line = (f"{name}: {_sky(daily['weather_code'][i])}, "
            f"{daily['temperature_2m_min'][i]:.0f} to {daily['temperature_2m_max'][i]:.0f} degrees")
    rain = daily["precipitation_probability_max"][i]
    return line + (f", {rain}% chance of rain" if rain is not None else "")


def _days_asked(request: str, dates: list[str]) -> list[int] | None:
    """Which forecast days the request is about, as indexes into dates (today first); None for
    right now and today."""
    asked = set(words(request).split())
    names = [datetime.date.fromisoformat(d).strftime("%A").lower() for d in dates]
    weekdays = [i for i, name in enumerate(names) if name in asked]
    if weekdays:
        return weekdays
    if "weekend" in asked:
        return [i for i, name in enumerate(names) if name in ("saturday", "sunday")][:2]
    if "tomorrow" in asked:
        return [1]
    if asked & WEEK_WORDS:
        return list(range(len(dates)))
    return None


# ---------------------------------------------------------------- stocks and the web

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
        price, previous_close = meta["regularMarketPrice"], meta.get("chartPreviousClose")
        when = datetime.datetime.fromtimestamp(meta["regularMarketTime"]).strftime("%A %H:%M")
        line = f"{meta.get('longName', symbol)} ({symbol}) is at {price:.2f} {meta.get('currency', '')}"
        if previous_close:
            line += f", {(price - previous_close) / previous_close * 100:+.2f}% today"
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
        pages = pool.submit(DDGS().text, query, max_results=3)
    lines = []
    try:
        lines += [f"- [{r.get('date', '')[:10]}] {r['title']}: {r['body']}" for r in news.result()]
    except Exception:
        pass
    try:
        lines += [f"- {r['title']}: {r['body']}" for r in pages.result()]
    except Exception:
        pass
    if not lines:
        return "No results found."
    today = datetime.date.today().isoformat()
    return (f"Live web results for '{query}' (today is {today}). "
            "Answer the user directly from these:\n" + "\n".join(lines))
