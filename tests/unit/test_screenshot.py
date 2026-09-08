"""截图上传、视觉转录与确认后 Build 映射的离线契约。"""

from __future__ import annotations

import base64
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from api.build import ScreenshotBuildRequest, analyze_screenshot_build
from api.screenshot import ScreenshotRequest, screenshot_recognize
from core.config import Config
from core.llm import LLMResponse
from parsers.build_report import build_health_report
from parsers.screenshot import (
    ScreenshotBuildFields,
    ScreenshotError,
    parse_recognition,
    recognize_screenshot,
    screenshot_build,
    validate_image_data_url,
)


def _data_url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


PNG = _data_url("image/png", b"\x89PNG\r\n\x1a\nprobe")


class _StubClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages = None
        self.kwargs = None

    def chat(self, messages, **kwargs):  # noqa: ANN001
        self.messages = messages
        self.kwargs = kwargs
        return LLMResponse(content=self.content, model="vision-test")


class _NoQueryConnection:
    def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("部分截图不应查询缺失结构")


class ScreenshotInputTest(unittest.TestCase):
    def test_accepts_png_and_checks_real_signature(self) -> None:
        mime, raw = validate_image_data_url(PNG)
        self.assertEqual(mime, "image/png")
        self.assertTrue(raw.startswith(b"\x89PNG"))

        fake = _data_url("image/png", b"not-a-png")
        with self.assertRaises(ScreenshotError) as cm:
            validate_image_data_url(fake)
        self.assertEqual(cm.exception.code, "image_signature_mismatch")

    def test_rejects_unsupported_and_oversized_inputs(self) -> None:
        with self.assertRaises(ScreenshotError) as cm:
            validate_image_data_url(_data_url("image/gif", b"GIF89a"))
        self.assertEqual(cm.exception.code, "unsupported_image")

        with patch("parsers.screenshot.MAX_DATA_URL_CHARS", 10):
            with self.assertRaises(ScreenshotError) as cm:
                validate_image_data_url(PNG)
        self.assertEqual(cm.exception.code, "image_too_large")

    def test_api_returns_503_without_constructing_llm_client(self) -> None:
        with (
            patch("api.screenshot.get_config", return_value=Config(raw={})),
            patch("api.screenshot.get_llm_client") as get_client,
            self.assertRaises(HTTPException) as cm,
        ):
            screenshot_recognize(ScreenshotRequest(data_url=PNG), caller="test")
        self.assertEqual(cm.exception.status_code, 503)
        self.assertEqual(cm.exception.detail["error"], "vision_model_not_configured")
        get_client.assert_not_called()


class ScreenshotRecognitionTest(unittest.TestCase):
    def test_model_receives_image_and_only_whitelisted_fields_survive(self) -> None:
        content = json.dumps({
            "kind": "build",
            "transcript": "DPS 12,345\nFire Resistance 75%",
            "build": {
                "level": 95,
                "class_name": "Witch",
                "ascend_class_name": "Elementalist",
                "main_skill": "Spark",
                "player_stats": {
                    "FullDPS": 12345,
                    "FireResist": 75,
                    "InventedDamage": 999,
                    "TotalEHP": float("nan"),
                },
                "player_stat_text": {"ChainMaxString": "+2", "Unknown": "all"},
                "enemy_is_boss": "Pinnacle",
            },
            "uncertain": ["主技能名称边缘模糊"],
        })
        client = _StubClient(content)
        result = recognize_screenshot(PNG, client=client, model="vision-model")

        self.assertEqual(result.kind, "build")
        self.assertEqual(result.build.player_stats, {"FullDPS": 12345.0, "FireResist": 75.0})
        self.assertEqual(result.build.player_stat_text, {"ChainMaxString": "+2"})
        self.assertEqual(result.build.enemy_is_boss, "Pinnacle")
        self.assertEqual(result.model, "vision-test")
        self.assertEqual(client.kwargs["model"], "vision-model")
        image = client.messages[1]["content"][1]
        self.assertEqual(image["type"], "image_url")
        self.assertEqual(image["image_url"]["url"], PNG)

    def test_markdown_fenced_json_is_accepted(self) -> None:
        result = parse_recognition(
            '```json\n{"kind":"item","transcript":"Rarity: Rare","build":{},"uncertain":[]}\n```'
        )
        self.assertEqual(result.kind, "item")
        self.assertEqual(result.transcript, "Rarity: Rare")

    def test_invalid_or_empty_model_output_fails_as_a_whole(self) -> None:
        with self.assertRaises(ScreenshotError) as cm:
            parse_recognition("not json")
        self.assertEqual(cm.exception.code, "invalid_vision_response")

        with self.assertRaises(ScreenshotError) as cm:
            parse_recognition('{"kind":"build","transcript":"","build":{}}')
        self.assertEqual(cm.exception.code, "empty_build")

    def test_missing_model_is_explicitly_unavailable(self) -> None:
        client = _StubClient("{}")
        with self.assertRaises(ScreenshotError) as cm:
            recognize_screenshot(PNG, client=client, model="")
        self.assertEqual(cm.exception.code, "vision_model_not_configured")
        self.assertEqual(cm.exception.status_code, 503)

    def test_poe2_marker_is_rejected_even_if_model_calls_it_build(self) -> None:
        result = parse_recognition(json.dumps({
            "kind": "build",
            "transcript": "Path of Exile 2 character",
            "build": {"class_name": "Sorceress"},
        }))
        self.assertEqual(result.kind, "unknown")
        self.assertIn("只支持 PoE1", result.uncertain[0])


class ScreenshotBuildTest(unittest.TestCase):
    def test_confirmation_is_required_and_unknown_keys_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ScreenshotBuildRequest(class_name="Witch")
        with self.assertRaises(ValidationError):
            ScreenshotBuildRequest(
                confirmed=True,
                player_stats={"NotAPlayerStat": 1},
            )
        with self.assertRaises(ValidationError):
            ScreenshotBuildRequest(confirmed=True, player_stats={"FullDPS": True})

    def test_confirmed_fields_become_partial_build(self) -> None:
        build = screenshot_build(ScreenshotBuildFields(
            level=95,
            class_name="Witch",
            ascend_class_name="Elementalist",
            main_skill="Spark",
            player_stats={"FullDPS": 12345, "FireResist": 75},
            player_stat_text={"ChainMaxString": "+2"},
            enemy_is_boss="Pinnacle",
        ))
        self.assertFalse(build.structure_complete)
        self.assertEqual(build.main_group.active_gem.name_en, "Spark")
        self.assertEqual(build.config["enemyIsBoss"], "Pinnacle")
        self.assertIn("截图转录", build.player_stats_basis)

    def test_unseen_structure_is_unknown_not_zero(self) -> None:
        build = screenshot_build(ScreenshotBuildFields(class_name="Witch"))
        with patch("parsers.build_report.constants.get", return_value=90):
            report = build_health_report(_NoQueryConnection(), build)
        for key in ("flasks", "curses", "pantheon", "jewels", "ascendancy"):
            item = report.by_key(key)
            self.assertIsNotNone(item)
            self.assertFalse(item.known, key)
            self.assertIn("不把“未提供”判成“没有”", item.missing_reason)
        clear = report.by_key("output_clear")
        self.assertEqual(clear.key, "output_clear")
        self.assertFalse(clear.known)
        self.assertIn("AreaOfEffectRadiusMetres", clear.missing_reason)

    def test_confirmed_api_returns_screenshot_report(self) -> None:
        from calc import constants
        from core.db import SCHEMA

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO game_constant VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            [
                (key, "pobMisc", None, 0, "scalar", value, None,
                 "Path of Building", 2, "https://example/Data.lua", "test", "test")
                for key, value in (
                    (constants.MAX_RESIST_CAP, 90.0),
                    (constants.BLOCK_CHANCE_CAP, 90.0),
                    (constants.SUPPRESSION_CHANCE_CAP, 100.0),
                )
            ],
        )
        conn.commit()
        constants.load(conn, force=True)
        try:
            req = ScreenshotBuildRequest(
                confirmed=True,
                class_name="Witch",
                main_skill="Spark",
                player_stats={"FullDPS": 12345, "FireResist": 75},
            )
            with patch("api.build.dbmod.get_conn", return_value=conn):
                response = analyze_screenshot_build(req, caller="test")
        finally:
            constants.reset_cache()
            conn.close()

        self.assertEqual(response.input_type, "screenshot")
        self.assertIn("截图转录", response.player_stats_note)
        self.assertEqual(next(x for x in response.report if x.key == "fire_resist").value, "75%")
        self.assertFalse(next(x for x in response.report if x.key == "flasks").known)


if __name__ == "__main__":
    unittest.main()
