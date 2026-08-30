# ruff: noqa: RUF001
from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 10.0
MAX_CITY_LENGTH = 80
MAX_URL_LENGTH = 2048

INVALID_CITY_MESSAGE = "请输入 1 至 80 个字符的城市名称，例如：/weather 北京。"
CITY_NOT_FOUND_MESSAGE = "没有找到这个城市，请检查城市名称后重试。"
UPSTREAM_UNAVAILABLE_MESSAGE = "天气服务暂时不可用，请稍后再试。"

WEATHER_DESCRIPTIONS = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "强毛毛雨",
    56: "轻微冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "轻微冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "中等阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


class WeatherInputError(ValueError):
    pass


class CityNotFoundError(LookupError):
    pass


class WeatherUpstreamError(RuntimeError):
    pass


class WeatherPlugin:
    def __init__(self) -> None:
        self._geocoding_url = DEFAULT_GEOCODING_URL
        self._forecast_url = DEFAULT_FORECAST_URL
        self._client: httpx.AsyncClient | None = None

    async def startup(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ValueError("weather config must be an object")
        unexpected = set(config) - {
            "geocoding_base_url",
            "forecast_base_url",
            "timeout_seconds",
        }
        if unexpected:
            raise ValueError("weather config contains unsupported fields")

        self._geocoding_url = _validate_endpoint_url(
            config.get("geocoding_base_url", DEFAULT_GEOCODING_URL),
            field_name="geocoding_base_url",
        )
        self._forecast_url = _validate_endpoint_url(
            config.get("forecast_base_url", DEFAULT_FORECAST_URL),
            field_name="forecast_base_url",
        )
        timeout_seconds = _validate_timeout(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        if self._client is not None:
            await self._client.aclose()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "wechat-bot-weather/0.1"},
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def health(self) -> dict[str, str]:
        return {"status": "ok" if self._client is not None else "not_initialized"}

    async def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        command = event.get("command")
        if command is None:
            return {"actions": []}
        if not isinstance(command, dict):
            raise ValueError("event command must be an object")
        if command.get("name") != "weather":
            return {"actions": []}

        result = await self._public_query(command.get("arguments"))
        return {
            "actions": [
                {
                    "type": "reply.text",
                    "action_key": "weather-result",
                    "content": result["text"],
                }
            ]
        }

    async def invoke_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name != "plugin.weather.query":
            raise ValueError("unsupported tool")
        if not isinstance(context, dict):
            raise ValueError("tool context must be an object")
        if not isinstance(arguments, dict):
            return _error_result("INVALID_ARGUMENT", INVALID_CITY_MESSAGE)
        if set(arguments) - {"city"}:
            return _error_result("INVALID_ARGUMENT", "只支持 city 参数。")
        return await self._public_query(arguments.get("city"))

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _public_query(self, city_value: object) -> dict[str, Any]:
        try:
            city = _validate_city(city_value)
        except WeatherInputError:
            return _error_result("INVALID_ARGUMENT", INVALID_CITY_MESSAGE)

        try:
            location = await self._geocode(city)
            current = await self._forecast(location)
        except CityNotFoundError:
            return _error_result("CITY_NOT_FOUND", CITY_NOT_FOUND_MESSAGE)
        except WeatherUpstreamError:
            return _error_result("WEATHER_SERVICE_UNAVAILABLE", UPSTREAM_UNAVAILABLE_MESSAGE)

        text = _format_weather_text(location, current)
        return {
            "ok": True,
            "provider": "Open-Meteo",
            "query": city,
            "location": location,
            "current": current,
            "text": text,
        }

    async def _geocode(self, city: str) -> dict[str, Any]:
        payload = await self._request_json(
            self._geocoding_url,
            params={"name": city, "count": 1, "language": "zh", "format": "json"},
        )
        results = payload.get("results")
        if results is None or results == []:
            raise CityNotFoundError
        if not isinstance(results, list) or not isinstance(results[0], dict):
            raise WeatherUpstreamError

        first = results[0]
        latitude = _bounded_number(first.get("latitude"), minimum=-90, maximum=90)
        longitude = _bounded_number(first.get("longitude"), minimum=-180, maximum=180)
        return {
            "name": _upstream_text(first.get("name"), required=True),
            "admin1": _upstream_text(first.get("admin1"), required=False),
            "country": _upstream_text(first.get("country"), required=False),
            "latitude": latitude,
            "longitude": longitude,
            "timezone": _upstream_text(first.get("timezone"), required=False),
        }

    async def _forecast(self, location: dict[str, Any]) -> dict[str, Any]:
        payload = await self._request_json(
            self._forecast_url,
            params={
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": (
                    "temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "precipitation,weather_code,wind_speed_10m"
                ),
                "temperature_unit": "celsius",
                "wind_speed_unit": "kmh",
                "precipitation_unit": "mm",
                "timezone": "auto",
            },
        )
        current = payload.get("current")
        if not isinstance(current, dict):
            raise WeatherUpstreamError

        weather_code_number = _bounded_number(current.get("weather_code"), minimum=0, maximum=99)
        if not weather_code_number.is_integer():
            raise WeatherUpstreamError
        observed_at = _upstream_text(current.get("time"), required=True, max_length=40)
        return {
            "observed_at": observed_at,
            "weather_code": int(weather_code_number),
            "weather": WEATHER_DESCRIPTIONS.get(int(weather_code_number), "未知天气"),
            "temperature_c": _bounded_number(
                current.get("temperature_2m"), minimum=-100, maximum=100
            ),
            "apparent_temperature_c": _bounded_number(
                current.get("apparent_temperature"), minimum=-120, maximum=120
            ),
            "relative_humidity_percent": _bounded_number(
                current.get("relative_humidity_2m"), minimum=0, maximum=100
            ),
            "precipitation_mm": _bounded_number(
                current.get("precipitation"), minimum=0, maximum=10_000
            ),
            "wind_speed_kmh": _bounded_number(
                current.get("wind_speed_10m"), minimum=0, maximum=500
            ),
        }

    async def _request_json(
        self,
        url: str,
        *,
        params: dict[str, str | int | float | bool | None],
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("weather plugin is not initialized")
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise WeatherUpstreamError from None
        if not isinstance(payload, dict):
            raise WeatherUpstreamError
        return payload


def _validate_city(value: object) -> str:
    if not isinstance(value, str):
        raise WeatherInputError
    city = value.strip()
    if not city or len(city) > MAX_CITY_LENGTH or not city.isprintable():
        raise WeatherInputError
    return city


def _validate_endpoint_url(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_URL_LENGTH
        or not normalized.isprintable()
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError(f"{field_name} is invalid")
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError:
        raise ValueError(f"{field_name} is invalid") from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL without credentials")
    return normalized


def _validate_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("timeout_seconds must be a number")
    timeout = float(value)
    if not math.isfinite(timeout) or not 0.2 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds must be between 0.2 and 10")
    return timeout


def _upstream_text(
    value: object,
    *,
    required: bool,
    max_length: int = 120,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise WeatherUpstreamError
    normalized = value.strip()
    if not normalized or len(normalized) > max_length or not normalized.isprintable():
        raise WeatherUpstreamError
    return normalized


def _bounded_number(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WeatherUpstreamError
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise WeatherUpstreamError
    return number


def _error_result(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message},
        "text": message,
    }


def _format_weather_text(location: dict[str, Any], current: dict[str, Any]) -> str:
    location_name = str(location["name"])
    qualifiers = [
        str(value)
        for value in (location.get("admin1"), location.get("country"))
        if value and str(value).casefold() != location_name.casefold()
    ]
    unique_qualifiers = list(dict.fromkeys(qualifiers))
    display_name = (
        f"{location_name}（{'，'.join(unique_qualifiers)}）" if unique_qualifiers else location_name
    )
    return (
        f"{display_name}当前天气：{current['weather']}，"
        f"{_format_number(current['temperature_c'])}°C，"
        f"体感 {_format_number(current['apparent_temperature_c'])}°C，"
        f"湿度 {_format_number(current['relative_humidity_percent'])}%，"
        f"降水 {_format_number(current['precipitation_mm'])} mm，"
        f"风速 {_format_number(current['wind_speed_kmh'])} km/h。"
        f"观测时间：{current['observed_at']}"
    )


def _format_number(value: int | float) -> str:
    number = float(value)
    return f"{number:.1f}".rstrip("0").rstrip(".")


def create_plugin() -> WeatherPlugin:
    return WeatherPlugin()
