"""
Live information: time, battery, weather and stock prices. The weather and stock prices are shown in a
card above the orb, and Jarvis says the gist in a sentence of its own instead of leaving it to the model.
"""
import datetime
import functools
import json
import logging
import re
import time
import webbrowser
from urllib.parse import quote, quote_plus
from urllib.request import Request, urlopen

import psutil

from ..phrases import words
from ..ui.status import Card, Status
from .system import powershell
from .web import GOOGLE

log = logging.getLogger("jarvis")

YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search?quotesCount=1&newsCount=0&q={}"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1d&interval=5m"   # the day, every 5 minutes
YAHOO_QUOTE = "https://finance.yahoo.com/quote/{}"
# how Jarvis says a price's currency; any other it says by its code
CURRENCIES = {"USD": "dollars", "EUR": "euros", "GBP": "pounds", "GBp": "pence", "JPY": "yen",
              "CHF": "Swiss francs", "RON": "lei"}

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
# a request naming its city plainly: 'show me the weather in Vienna', 'the forecast for New York on Friday'
CITY_ASKED = re.compile(r"\b(?:weather|forecast|temperature)\s+(?:like\s+)?(?:in|for|at)\s+([^?.!,]+)", re.I)
AFTER_CITY = DAY_WORDS | {"on", "this", "next", "for", "at", "and", "then", "again", "please", "now", "right"}
# a 'city' that means where the user is: 'here', 'my area', the model's 'current location'
HERE_WORDS = {"here", "my", "me", "our", "your", "the", "user", "user's", "current", "location", "local", "area",
              "city", "town", "near", "nearby", "around", "outside"}

# Where this PC is: Windows' location service, through PowerShell, as 'latitude longitude', or 'off'
# when Windows won't tell apps (Settings, Privacy, Location)
LOCATION_SCRIPT = (
    "Add-Type -AssemblyName System.Device; $watcher = New-Object System.Device.Location.GeoCoordinateWatcher; "
    "$watcher.Start(); $end = (Get-Date).AddSeconds(10); "
    "while ($watcher.Status -notin 'Ready', 'Disabled' -and $watcher.Permission -ne 'Denied' -and (Get-Date) -lt $end) "
    "{ Start-Sleep -Milliseconds 100 }; $at = $watcher.Position.Location; "
    "if ($watcher.Status -eq 'Disabled' -or $watcher.Permission -eq 'Denied') { 'off' } elseif (-not $at.IsUnknown) { "
    "$at.Latitude.ToString([cultureinfo]::InvariantCulture) + ' ' + $at.Longitude.ToString([cultureinfo]::InvariantCulture) }"
)
# then the town there, from OpenStreetMap, which asks apps to say who they are
REVERSE_GEOCODE = "https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=10&accept-language=en&lat={}&lon={}"
JARVIS_AGENT = "Jarvis desktop assistant"
HERE_SECONDS = 3600   # how long where the PC is stays known before Windows is asked again: a laptop moves
# a follow-up naming only another company: 'and Nvidia?', 'what about Apple?'
OTHER_COMPANY = re.compile(r"^\s*(?:and|what about|how about)\s+([^?.!,]+?)[\s?.!]*$", re.I)
NOT_COMPANY = DAY_WORDS | {"it", "that", "this", "them", "those", "you", "me"}


def _get_json(url: str, agent: str = "Mozilla/5.0") -> dict:
    request = Request(url, headers={"User-Agent": agent})
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
        self.city = None    # the last city asked about; '' for where the PC is
        self.here = None    # (when, place): where the PC is, as Windows last said
        self.screen = Status()   # where the forecast is shown; this one shows nothing
        # what Jarvis says itself about the last call, as (sentence, its values), or None for the model
        # to word the result, like a failure; set again by every call
        self.reply: tuple[str, dict] | None = None

    def get_weather(self, city: str | None = None) -> str:
        """Get the weather in a city, or where the user is: right now, or the forecast for the days the user asks about.

        Args:
          city: The city name, for example 'Milan'. Leave it out for the weather where the user is
        """
        city = self._city_meant(city or "")
        self.city = city
        try:
            place = _find_place(city) if city else self._place_here()
            if place is None:
                return f"No place called {city} found."
            forecast = _get_json(FORECAST.format(place["latitude"], place["longitude"]))
            days = _days_asked(self.request, forecast["daily"]["time"])
            shown = self.screen.show_card(_weather_card(place, forecast, days))
            self.reply = _weather_said(place, forecast, days, shown)
            return _weather_report(place, forecast, days)
        except Exception as e:
            return f"Couldn't get the weather: {e}"

    def _city_meant(self, city: str) -> str:
        """The city the user asked about, or '' for where they are: for 'here' or 'my location', and for
        a city they never said, which small models make up (New York) when none was asked about. The
        last city asked about stays, as the model passes it again for 'and tomorrow?'."""
        named = set(words(city).split())
        if not named or named <= HERE_WORDS:
            return ""
        if city != self.city and not named & set(words(self.request).split()):
            log.info("[weather] '%s' wasn't asked about, so it's the weather where the PC is", city)
            return ""
        return city

    def _place_here(self) -> dict:
        """Where this PC is, as a place like _find_place's; Windows is asked at most every HERE_SECONDS."""
        if self.here is None or time.monotonic() - self.here[0] > HERE_SECONDS:
            self.here = time.monotonic(), _ask_windows_where()
        return self.here[1]

    def follow_up_args(self, request: str, also: set[str]) -> dict | None:
        """The arguments to get the weather again for a follow-up that only changes the day, like
        'and tomorrow?' or 'what about Friday?': the last city, or where the PC is again. None for
        anything else. also: more words such a follow-up may hold, like 'rain' in 'will it rain on Friday?'."""
        asked = set(words(request).split())
        if self.city is not None and asked & DAY_WORDS and not asked - DAY_WORDS - FOLLOW_UP_WORDS - also:
            return {"city": self.city}
        return None

    @staticmethod
    def asked_args(request: str) -> dict | None:
        """The arguments for a request that names its city plainly, like 'show me the weather in Vienna'
        or 'the forecast for New York on Friday'. None if it doesn't."""
        found = CITY_ASKED.search(request)
        if not found:
            return None
        city = []
        for word in found.group(1).split():   # up to the day or whatever else follows it
            if word.lower() in AFTER_CITY:
                break
            city.append(word)
        return {"city": " ".join(city)} if city else None


@functools.lru_cache(maxsize=64)   # places stay put, so 'and tomorrow?' skips a round trip
def _find_place(city: str) -> dict | None:
    """The place Open-Meteo knows by this name, with its latitude and longitude."""
    found = _get_json(GEOCODE.format(quote_plus(city))).get("results")
    if not found:   # the geocoder finds 'Milan' but not 'Milan, Italy', so try the part before the comma
        found = _get_json(GEOCODE.format(quote_plus(city.split(",")[0]))).get("results")
    return found[0] if found else None


def _ask_windows_where() -> dict:
    """Where this PC is, from Windows' location service (the Wi-Fi networks around it, or GPS), as a
    place like _find_place's. Rounded to about a kilometre: enough for the weather, and all that
    leaves the PC."""
    result = powershell(LOCATION_SCRIPT)
    found = result.stdout.split()
    if found == ["off"]:
        raise LookupError("location is turned off for apps on this PC (Windows Settings, Privacy, Location). "
                          "The user can turn it on, or name a city")
    if len(found) != 2:
        log.info("[location] %s", result.stderr.strip()[:300])
        raise LookupError("Windows couldn't tell where this PC is. The user can name a city instead")
    latitude, longitude = (round(float(n), 2) for n in found)
    try:
        address = _get_json(REVERSE_GEOCODE.format(latitude, longitude), JARVIS_AGENT).get("address", {})
    except Exception as e:   # the weather works without the town's name
        log.info("[location] no town name: %s", e)
        address = {}
    name = next((address[k] for k in ("city", "town", "village", "municipality") if k in address), "your area")
    return {"name": name, "country": address.get("country"), "latitude": latitude, "longitude": longitude}


def _where(place: dict) -> str:
    """'Milan, Italy'."""
    return ", ".join(filter(None, [place["name"], place.get("country")]))


def _sky(code: int) -> str:
    return WEATHER_CODES.get(code, "unknown weather")


def _weather_report(place: dict, forecast: dict, days: list[int] | None) -> str:
    """The weather for the days the request is about, for the model and the conversation so far. Only
    those: small models read out every line they're given."""
    where = _where(place)
    now, daily = forecast["current"], forecast["daily"]
    if days is None:   # right now, and today
        return "\n".join([f"Weather in {where} now: {now['temperature_2m']:.0f} degrees (feels like "
                          f"{now['apparent_temperature']:.0f} degrees), {_sky(now['weather_code'])}, "
                          f"wind {now['wind_speed_10m']:.0f} km/h.",
                          _describe_day(daily, 0), "Answer in one short sentence."])
    whole_week = len(days) == len(daily["time"])
    return "\n".join([f"Forecast for {where}:", *(_describe_day(daily, i) for i in days),
                      "Sum up the week in two short sentences." if whole_week else "Answer in one short sentence."])


def _weather_said(place: dict, forecast: dict, days: list[int] | None, shown: bool) -> tuple[str, dict] | None:
    """What Jarvis says about the weather itself: right now, or the one day asked about. Several days
    are on the card, so it only points to them; None without a card, for the model to sum them up."""
    daily = forecast["daily"]
    if days is None:
        now = forecast["current"]
        return "It's {temperature} degrees in {place}, {sky}.", {
            "temperature": round(now["temperature_2m"]), "place": place["name"], "sky": _sky(now["weather_code"])}
    if len(days) == 1:
        i = days[0]
        rain = daily["precipitation_probability_max"][i]
        values = {"day": _day_label(daily, i), "place": place["name"], "sky": _sky(daily["weather_code"][i]),
                  "low": round(daily["temperature_2m_min"][i]), "high": round(daily["temperature_2m_max"][i])}
        if rain:   # None or 0: nothing worth saying
            return "{day} in {place}: {sky}, {low} to {high} degrees, {rain}% chance of rain.", values | {"rain": rain}
        return "{day} in {place}: {sky}, {low} to {high} degrees.", values
    return ("Here's the forecast for {place}.", {"place": place["name"]}) if shown else None


def _weather_card(place: dict, forecast: dict, days: list[int] | None) -> Card:
    """Right now, and the week ahead a day a row, with the days asked about marked."""
    now, daily = forecast["current"], forecast["daily"]
    rain = daily["precipitation_probability_max"]
    return Card(
        title=f"Weather in {_where(place)}",
        big=f"{round(now['temperature_2m'])}°",
        notes=(_sky(now["weather_code"]).capitalize(),
               f"Feels like {round(now['apparent_temperature'])}°, wind {round(now['wind_speed_10m'])} km/h"),
        rows=tuple((_day_label(daily, i), _sky(code).capitalize(), "" if rain[i] is None else f"{rain[i]}% rain",
                    f"{round(daily['temperature_2m_min'][i])}° – {round(daily['temperature_2m_max'][i])}°")
                   for i, code in enumerate(daily["weather_code"])),
        marked=tuple(days) if days and len(days) < len(daily["time"]) else (),   # the whole week: none stands out
        link="See it on Google",
        on_link=lambda: webbrowser.open(GOOGLE.format(quote_plus(f"weather {place['name']}"))))


def _day_label(daily: dict, i: int) -> str:
    """'Today', 'Tomorrow', then the day's name, like 'Friday'."""
    return ("Today", "Tomorrow")[i] if i < 2 else datetime.date.fromisoformat(daily["time"][i]).strftime("%A")


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


# ---------------------------------------------------------------- stocks

class Stocks:
    def __init__(self):
        self.screen = Status()   # where the price is shown; this one shows nothing
        self.reply: tuple[str, dict] | None = None   # what Jarvis says itself about the last call, as for Weather

    def get_stock_price(self, company: str) -> str:
        """Get the live stock price of a company.

        Args:
          company: The company name or ticker symbol, for example 'apple' or 'AAPL'
        """
        try:
            symbol = _find_symbol(company)
            if symbol is None:
                return f"No stock found for {company}."
            chart =_get_json(YAHOO_CHART.format(quote(symbol)))["chart"]["result"][0]
            meta = chart["meta"]
            price, previous_close = meta["regularMarketPrice"], meta.get("chartPreviousClose")
            change = (price - previous_close) / previous_close * 100 if previous_close else None
            name, currency = meta.get("longName") or meta.get("shortName") or symbol, meta.get("currency", "")
            when = datetime.datetime.fromtimestamp(meta["regularMarketTime"]).strftime("%A %H:%M")
            self.screen.show_card(_stock_card(symbol, name, chart, change, when))
            self.reply = _stock_said(name, price, currency, change)
            line = f"{name} ({symbol}) is at {price:.2f} {currency}"
            if change is not None:
                line += f", {change:+.2f}% today"
            return line + f", as of {when}."
        except Exception as e:
            return f"Couldn't get the stock price: {e}"

    @staticmethod
    def follow_up_args(request: str) -> dict | None:
        """The arguments for a follow-up that only names another company, like 'and Nvidia?' or 'what
        about Apple?' right after a price. None for anything else."""
        found = OTHER_COMPANY.match(request)
        company = found.group(1).strip() if found else ""
        if not company or len(company.split()) > 3 or set(words(company).split()) & NOT_COMPANY:
            return None
        return {"company": company}


@functools.lru_cache(maxsize=64)   # a company keeps its ticker, so asking again skips a round trip
def _find_symbol(company: str) -> str | None:
    """The ticker Yahoo knows this company by, like 'NVDA' for 'nvidia'."""
    found = _get_json(YAHOO_SEARCH.format(quote_plus(company)))["quotes"]
    return found[0]["symbol"] if found else None


def _stock_said(name: str, price: float, currency: str, change: float | None) -> tuple[str, dict]:
    """'Apple Inc. is at 345.48 dollars, down 0.5% today.' Big prices without the cents, which only take
    long to say."""
    values = {"name": name, "price": f"{price:.0f}" if price >= 1000 else f"{price:.2f}",
              "currency": CURRENCIES.get(currency, currency)}
    if change is None:
        return "{name} is at {price} {currency}.", values
    if round(change, 1) == 0:
        return "{name} is at {price} {currency}, unchanged today.", values
    sentence = ("{name} is at {price} {currency}, up {change}% today." if change > 0
                else "{name} is at {price} {currency}, down {change}% today.")
    return sentence, values | {"change": f"{abs(change):.1f}"}


def _stock_card(symbol: str, name: str, chart: dict, change: float | None, when: str) -> Card:
    """The price and how it moved today, the day's prices as a line against yesterday's close, and the
    day's and the year's range."""
    meta = chart["meta"]
    price, previous_close = meta["regularMarketPrice"], meta.get("chartPreviousClose")
    moved = "" if change is None else f"{price - previous_close:+,.2f} ({change:+.2f}%) today"
    exchange = meta.get("fullExchangeName")
    rows = [("Previous close", _money(previous_close)),
            ("Day range", _range(meta.get("regularMarketDayLow"), meta.get("regularMarketDayHigh"))),
            ("52-week range", _range(meta.get("fiftyTwoWeekLow"), meta.get("fiftyTwoWeekHigh")))]
    return Card(
        title=f"{name} ({symbol})",
        big=f"{price:,.2f} {meta.get('currency', '')}".strip(),
        notes=tuple(filter(None, [moved, f"As of {when}" + (f", {exchange}" if exchange else "")])),
        rows=tuple(row for row in rows if row[1]),
        chart=_day_line(chart),
        baseline=previous_close,
        trend=0 if not change else 1 if change > 0 else -1,
        link="See it on Yahoo Finance",
        on_link=lambda: webbrowser.open(YAHOO_QUOTE.format(quote(symbol))))


def _money(value: float | None) -> str:
    return "" if value is None else f"{value:,.2f}"


def _range(low: float | None, high: float | None) -> str:
    return "" if low is None or high is None else f"{low:,.2f} – {high:,.2f}"


def _day_line(chart: dict) -> tuple[tuple[float, float], ...]:
    """The day's prices as points across the trading day: (how far into it, 0 to 1, price), so a day
    still going fills only part of the chart. Outside trading hours, across the last day's prices."""
    times = chart.get("timestamp") or []
    closes = (chart.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    points = [(t, c) for t, c in zip(times, closes) if c is not None]
    if len(points) < 2:
        return ()
    period = chart["meta"].get("currentTradingPeriod", {}).get("regular", {})
    start, end = period.get("start", 0), period.get("end", 0)
    if not start <= points[0][0] < points[-1][0] <= end:   # the prices are from an earlier day
        start, end = points[0][0], points[-1][0]
    return tuple(((t - start) / ((end - start) or 1), c) for t, c in points)
