# ruff: noqa: RUF001
from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx

from wechat_bot.plugins.manifest import load_plugin_manifest

WEATHER_PACKAGE = Path(__file__).parents[2] / "builtin_plugins" / "weather"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WEATHER_MODULE_SPEC = importlib.util.spec_from_file_location(
    "builtin_weather_plugin",
    WEATHER_PACKAGE / "plugin.py",
)
assert WEATHER_MODULE_SPEC is not None and WEATHER_MODULE_SPEC.loader is not None
WEATHER_MODULE = importlib.util.module_from_spec(WEATHER_MODULE_SPEC)
WEATHER_MODULE_SPEC.loader.exec_module(WEATHER_MODULE)

CITY_NOT_FOUND_MESSAGE = WEATHER_MODULE.CITY_NOT_FOUND_MESSAGE
INVALID_CITY_MESSAGE = WEATHER_MODULE.INVALID_CITY_MESSAGE
UPSTREAM_UNAVAILABLE_MESSAGE = WEATHER_MODULE.UPSTREAM_UNAVAILABLE_MESSAGE
WeatherPlugin = WEATHER_MODULE.WeatherPlugin

GEOCODING_RESPONSE = {
    "results": [
        {
            "name": "北京",
            "admin1": "北京市",
            "country": "中国",
            "latitude": 39.9042,
            "longitude": 116.4074,
            "timezone": "Asia/Shanghai",
        }
    ]
}
FORECAST_RESPONSE = {
    "current": {
        "time": "2026-08-30T19:15",
        "temperature_2m": 26.0,
        "apparent_temperature": 27.4,
        "relative_humidity_2m": 61,
        "precipitation": 0.0,
        "weather_code": 1,
        "wind_speed_10m": 8.6,
    }
}


@pytest.fixture
async def weather_plugin() -> AsyncIterator[WeatherPlugin]:
    plugin = WeatherPlugin()
    await plugin.startup({})
    try:
        yield plugin
    finally:
        await plugin.shutdown()


def test_weather_manifest_declares_command_and_read_only_tool() -> None:
    manifest, package_hash = load_plugin_manifest(WEATHER_PACKAGE)

    assert manifest.plugin_id == "builtin.weather"
    assert manifest.commands[0].name == "weather"
    assert "天气" in manifest.commands[0].aliases
    assert manifest.tools[0].name == "plugin.weather.query"
    assert manifest.tools[0].effect_class == "READ_ONLY"
    assert manifest.tools[0].input_schema["required"] == ["city"]
    assert len(package_hash) == 64


async def test_weather_builtin_is_installable(admin_client: httpx.AsyncClient) -> None:
    connection = await admin_client.post(
        "/api/v1/connections",
        json={
            "name": "Weather workspace",
            "api_base_url": "https://api.gewe.test",
            "token": "weather-test-token",
        },
    )
    assert connection.status_code == 201

    installed = await admin_client.post(
        "/api/v1/plugins/builtins/builtin.weather/install",
        json={"workspace_id": connection.json()["workspace_id"]},
    )

    assert installed.status_code == 201
    assert installed.json()["plugin"]["plugin_id"] == "builtin.weather"
    assert installed.json()["package"]["manifest"]["tools"][0]["effect_class"] == "READ_ONLY"
    assert "package_path" not in installed.text


@respx.mock
async def test_weather_command_and_tool_return_stable_chinese_result(
    weather_plugin: WeatherPlugin,
) -> None:
    geocoding = respx.get(GEOCODING_URL).mock(
        return_value=httpx.Response(200, json=GEOCODING_RESPONSE)
    )
    forecast = respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(200, json=FORECAST_RESPONSE)
    )

    command_result = await weather_plugin.handle_event(
        {"command": {"name": "weather", "arguments": " 北京 "}}
    )
    tool_result = await weather_plugin.invoke_tool(
        "plugin.weather.query",
        {"city": "北京"},
        {},
    )

    expected_text = (
        "北京（北京市，中国）当前天气：大部晴朗，26°C，体感 27.4°C，湿度 61%，"
        "降水 0 mm，风速 8.6 km/h。观测时间：2026-08-30T19:15"
    )
    assert command_result == {
        "actions": [
            {
                "type": "reply.text",
                "action_key": "weather-result",
                "content": expected_text,
            }
        ]
    }
    assert tool_result["ok"] is True
    assert tool_result["provider"] == "Open-Meteo"
    assert tool_result["query"] == "北京"
    assert tool_result["current"]["weather"] == "大部晴朗"
    assert tool_result["text"] == expected_text
    assert geocoding.call_count == 2
    assert forecast.call_count == 2
    assert geocoding.calls[0].request.url.params["name"] == "北京"
    assert forecast.calls[0].request.url.params["timezone"] == "auto"


@respx.mock
async def test_unknown_city_returns_stable_error(weather_plugin: WeatherPlugin) -> None:
    respx.get(GEOCODING_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    result = await weather_plugin.invoke_tool(
        "plugin.weather.query",
        {"city": "不存在的城市"},
        {},
    )

    assert result == {
        "ok": False,
        "error": {"code": "CITY_NOT_FOUND", "message": CITY_NOT_FOUND_MESSAGE},
        "text": CITY_NOT_FOUND_MESSAGE,
    }


@pytest.mark.parametrize("failure", ["timeout", "http_error", "invalid_json"])
@respx.mock
async def test_upstream_failure_does_not_leak_details(
    weather_plugin: WeatherPlugin,
    failure: str,
) -> None:
    route = respx.get(GEOCODING_URL)
    if failure == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("private timeout detail"))
    elif failure == "http_error":
        route.mock(return_value=httpx.Response(503, text="private provider detail"))
    else:
        route.mock(return_value=httpx.Response(200, text="private invalid json detail"))

    result = await weather_plugin.invoke_tool(
        "plugin.weather.query",
        {"city": "北京"},
        {},
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "WEATHER_SERVICE_UNAVAILABLE",
            "message": UPSTREAM_UNAVAILABLE_MESSAGE,
        },
        "text": UPSTREAM_UNAVAILABLE_MESSAGE,
    }
    assert "private" not in str(result)


@respx.mock
async def test_malformed_forecast_is_treated_as_upstream_failure(
    weather_plugin: WeatherPlugin,
) -> None:
    respx.get(GEOCODING_URL).mock(return_value=httpx.Response(200, json=GEOCODING_RESPONSE))
    respx.get(FORECAST_URL).mock(
        return_value=httpx.Response(
            200,
            json={"current": {"temperature_2m": "twenty six", "secret": "do not leak"}},
        )
    )

    result = await weather_plugin.invoke_tool(
        "plugin.weather.query",
        {"city": "北京"},
        {},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "WEATHER_SERVICE_UNAVAILABLE"
    assert "secret" not in str(result)


@pytest.mark.parametrize(
    "arguments, expected_message",
    [
        ({}, INVALID_CITY_MESSAGE),
        ({"city": 123}, INVALID_CITY_MESSAGE),
        ({"city": " "}, INVALID_CITY_MESSAGE),
        ({"city": "北\n京"}, INVALID_CITY_MESSAGE),
        ({"city": "城" * 81}, INVALID_CITY_MESSAGE),
        ({"city": "北京", "unexpected": True}, "只支持 city 参数。"),
    ],
)
async def test_tool_rejects_invalid_arguments_without_network(
    weather_plugin: WeatherPlugin,
    arguments: dict[str, object],
    expected_message: str,
) -> None:
    result = await weather_plugin.invoke_tool("plugin.weather.query", arguments, {})

    assert result["ok"] is False
    assert result["error"] == {"code": "INVALID_ARGUMENT", "message": expected_message}


async def test_command_without_city_returns_usage_and_plain_event_is_ignored(
    weather_plugin: WeatherPlugin,
) -> None:
    command_result = await weather_plugin.handle_event(
        {"command": {"name": "weather", "arguments": ""}}
    )
    plain_result = await weather_plugin.handle_event({"content": "北京天气怎么样"})

    assert command_result["actions"][0]["content"] == INVALID_CITY_MESSAGE
    assert plain_result == {"actions": []}


@pytest.mark.parametrize(
    "config",
    [
        {"geocoding_base_url": "ftp://example.test/search"},
        {"forecast_base_url": "https://user:password@example.test/forecast"},
        {"forecast_base_url": "/relative/forecast"},
        {"timeout_seconds": True},
        {"timeout_seconds": 0.1},
        {"unexpected": "value"},
    ],
)
async def test_startup_rejects_invalid_config(config: dict[str, object]) -> None:
    plugin = WeatherPlugin()

    with pytest.raises(ValueError):
        await plugin.startup(config)


@respx.mock
async def test_configurable_http_endpoints_are_used() -> None:
    plugin = WeatherPlugin()
    await plugin.startup(
        {
            "geocoding_base_url": "http://weather.test/geocode",
            "forecast_base_url": "https://weather.test/forecast",
            "timeout_seconds": 1,
        }
    )
    try:
        geocoding = respx.get("http://weather.test/geocode").mock(
            return_value=httpx.Response(200, json=GEOCODING_RESPONSE)
        )
        forecast = respx.get("https://weather.test/forecast").mock(
            return_value=httpx.Response(200, json=FORECAST_RESPONSE)
        )

        result = await plugin.invoke_tool(
            "plugin.weather.query",
            {"city": "北京"},
            {},
        )

        assert result["ok"] is True
        assert geocoding.called
        assert forecast.called
    finally:
        await plugin.shutdown()
