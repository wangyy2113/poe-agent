"""PoE1 截图的受限视觉转录。

视觉模型在这里的职责只有 OCR：读取截图里**明确可见**的文字与数字，并映射到
一小组白名单字段。它不是 Build 计算器，也不是知识来源；识别结果必须先由用户确认，
确认后才会进入现有的确定性装备 / Build 分析链路。

图片只以内存中的 data URL 发给已配置的 OpenAI 兼容网关，不写磁盘、不进数据库，
日志也只记录 MIME、字节数和识别类型。
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from core.llm import LLMClient, LLMError
from parsers.pob_build import BuildGem, PobBuild, SkillGroup


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DATA_URL_CHARS = 7_100_000
SUPPORTED_MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp")

# 只接收现有 Build 报告确实消费的字段。让视觉模型随意发明键名会把拼写近似项
# 误接到别的指标上；未知标签应留在 transcript，而不是猜一个 PlayerStat。
NUMERIC_STAT_KEYS = frozenset({
    "FullDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS", "AverageHit",
    "TotalEHP",
    "PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken",
    "LightningMaximumHitTaken", "ChaosMaximumHitTaken",
    "FireResist", "ColdResist", "LightningResist", "ChaosResist",
    "FireResistOverCap", "ColdResistOverCap", "LightningResistOverCap",
    "ChaosResistOverCap",
    "FireResistMax", "ColdResistMax", "LightningResistMax", "ChaosResistMax",
    "EffectiveSpellSuppressionChance", "SpellSuppressionChance",
    "Armour", "Evasion", "EffectiveBlockChance", "BlockChance",
    "EffectiveSpellBlockChance", "SpellBlockChance", "ManaReserved",
    "LifeRecoup", "ManaRecoup", "EnergyShieldRecoup",
    "LifeLeechGainRate", "ManaLeechGainRate", "EnergyShieldLeechGainRate",
    "NetLifeRegen", "NetManaRegen", "NetEnergyShieldRegen",
    "LifeRegenRecovery", "ManaRegenRecovery", "EnergyShieldRegenRecovery",
    "LifeRegen", "ManaRegen", "EnergyShieldRegen",
    "AreaOfEffectRadiusMetres", "AreaOfEffectRadius", "ProjectileCount",
    "BounceCount",
})
TEXT_STAT_KEYS = frozenset({
    "PierceCountString", "ForkCountString", "ChainMaxString", "SplitCountString",
})
_BOSS_VALUES = frozenset({"Boss", "Pinnacle", "Uber", "Shaper", "Sirus"})

_PROMPT = """你是《流放之路 1》截图转录器，只做 OCR，不做游戏知识补全。

请只输出一个 JSON 对象，不要 Markdown、解释或推理。结构：
{
  "kind": "item" | "build" | "unknown",
  "transcript": "按画面顺序逐行转录的可见文字",
  "build": {
    "level": 只有明确可见时填整数，否则 null,
    "class_name": "只有明确可见时填，否则空串",
    "ascend_class_name": "只有明确可见时填，否则空串",
    "main_skill": "只有明确标为当前/主技能时填，否则空串",
    "player_stats": {"白名单键": 数值},
    "player_stat_text": {"白名单键": "画面原值"},
    "enemy_is_boss": "Boss|Pinnacle|Uber|Shaper|Sirus|空串"
  },
  "uncertain": ["无法辨认或可能有误的具体位置"]
}

规则：
1. item 仅指单件装备提示框；build 指 PoB/角色面板/BD 总览；其余为 unknown。
2. 只读画面中明确出现的内容，不从职业、装备、技能或常识推导任何隐藏数值。
3. 百分号去掉后填数值，例如 75% 填 75；千位分隔去掉。看不清就省略。
4. PoE2 截图必须返回 unknown，并在 uncertain 说明“不支持 PoE2”。
5. player_stats 只允许下列键，找不到精确对应就不要填：
""" + ", ".join(sorted(NUMERIC_STAT_KEYS)) + "\n" + (
    "6. player_stat_text 只允许下列键：" + ", ".join(sorted(TEXT_STAT_KEYS)) + "\n"
    "7. 若 kind=item，transcript 要整理成游戏 Ctrl+C 文本的形状：保留可见的 "
    "Item Class/Rarity（或中文对应标签）、名称、底材、属性与词缀，并用 -------- 表示"
    "画面中的分区。只可整理版式，不可补写画面没有的信息；缺少可确定的表头时返回 unknown。"
)


class ScreenshotError(ValueError):
    """截图输入或视觉响应不可用。`code` 可安全返回给前端。"""

    def __init__(self, code: str, reason: str, *, status_code: int = 422) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class ScreenshotBuildFields:
    level: int | None = None
    class_name: str = ""
    ascend_class_name: str = ""
    main_skill: str = ""
    player_stats: dict[str, float] = field(default_factory=dict)
    player_stat_text: dict[str, str] = field(default_factory=dict)
    enemy_is_boss: str = ""


@dataclass(frozen=True)
class ScreenshotRecognition:
    kind: str
    transcript: str
    build: ScreenshotBuildFields
    uncertain: tuple[str, ...] = ()
    model: str = ""


def validate_image_data_url(data_url: str) -> tuple[str, bytes]:
    """校验 MIME、base64、体积与文件签名；绝不只信浏览器声明的类型。"""
    if len(data_url) > MAX_DATA_URL_CHARS:
        raise ScreenshotError("image_too_large", "图片超过 5 MB 上限")
    match = re.fullmatch(
        r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)",
        data_url,
    )
    if not match:
        raise ScreenshotError(
            "unsupported_image",
            "只支持 PNG、JPEG、WebP 的 base64 data URL",
        )
    media_type, encoded = match.groups()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ScreenshotError("invalid_image_base64", "图片 base64 编码无效") from exc
    if not raw:
        raise ScreenshotError("empty_image", "图片内容为空")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ScreenshotError("image_too_large", "图片超过 5 MB 上限")

    valid_magic = {
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
        "image/webp": len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
    }
    if not valid_magic.get(media_type, False):
        raise ScreenshotError(
            "image_signature_mismatch",
            f"图片内容与声明的 {media_type} 格式不一致",
        )
    return media_type, raw


def recognize_screenshot(
    data_url: str,
    *,
    client: LLMClient,
    model: str,
) -> ScreenshotRecognition:
    """调用已配置的视觉模型并把响应收紧到白名单字段。"""
    if not model.strip():
        raise ScreenshotError(
            "vision_model_not_configured",
            "未配置 llm.vision_model，截图识别不可用",
            status_code=503,
        )
    validate_image_data_url(data_url)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "转录这张 PoE1 截图。严格按 JSON 契约输出。"},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
            ],
        },
    ]
    try:
        response = client.chat(messages, model=model, max_tokens=4096)
    except LLMError as exc:
        # 上游异常有机会包含请求摘要。这里不把原异常正文带到 API 或日志，避免图片
        # data URL 被某些网关回显后进入持久化日志。
        raise ScreenshotError(
            "vision_request_failed",
            "视觉模型调用失败；请检查凭证、模型权限与网关状态",
            status_code=502,
        ) from exc
    return parse_recognition(response.content, model=response.model or model)


def parse_recognition(content: str, *, model: str = "") -> ScreenshotRecognition:
    """解析视觉模型 JSON；形状不对就整体失败，不返回半份结果。"""
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ScreenshotError(
            "invalid_vision_response", "视觉模型没有返回合法 JSON，请重试"
        ) from exc
    if not isinstance(obj, dict):
        raise ScreenshotError("invalid_vision_response", "视觉模型响应必须是 JSON 对象")

    kind = str(obj.get("kind") or "unknown").strip().lower()
    if kind not in {"item", "build", "unknown"}:
        raise ScreenshotError("invalid_vision_response", f"未知截图类型：{kind}")
    transcript = _clean_text(obj.get("transcript"), 20_000)
    uncertain = tuple(
        text for text in (_clean_text(x, 300) for x in _as_list(obj.get("uncertain"))) if text
    )[:20]
    if re.search(r"(?:Path\s+of\s+Exile\s*2|流放之路\s*2|POE\s*2)", transcript, re.I):
        kind = "unknown"
        uncertain = ("检测到 PoE2 标识；本项目只支持 PoE1", *uncertain)[:20]
    build = _parse_build_fields(obj.get("build"))

    if kind == "item" and not transcript:
        raise ScreenshotError("empty_transcript", "装备截图没有识别出可确认的文字")
    if kind == "build" and not _has_build_content(build):
        raise ScreenshotError("empty_build", "Build 截图没有识别出可确认的字段")
    return ScreenshotRecognition(
        kind=kind,
        transcript=transcript,
        build=build,
        uncertain=uncertain,
        model=model,
    )


def screenshot_build(fields: ScreenshotBuildFields) -> PobBuild:
    """用户确认后的可见字段 → 部分 Build；没出现在截图里的维度保持未知。"""
    groups: list[SkillGroup] = []
    if fields.main_skill:
        groups.append(SkillGroup(
            is_main=True,
            active_skill_index=1,
            active_skill_calcs_index=1,
            gems=(BuildGem(name_en=fields.main_skill),),
        ))
    config = {"enemyIsBoss": fields.enemy_is_boss} if fields.enemy_is_boss else {}
    return PobBuild(
        level=fields.level,
        class_name=fields.class_name,
        ascend_class_name=fields.ascend_class_name,
        skill_groups=groups,
        main_socket_group=1 if groups else None,
        calcs_skill_group=1 if groups else None,
        config=config,
        player_stats=dict(fields.player_stats),
        player_stat_text={
            **{key: f"{value:g}" for key, value in fields.player_stats.items()},
            **fields.player_stat_text,
        },
        player_stats_basis="取自你确认的截图转录；只包含截图中明确可见的字段",
        tree_version_basis="截图未显示可核对的天赋树版本",
        passive_nodes_basis="截图未提供可核对的完整天赋节点列表",
        structure_complete=False,
        structure_basis="单张截图只覆盖画面中明确可见的结构，未拍到的部分视为未知",
        warnings=[
            "截图经过视觉模型转录，可能存在 OCR 错字；本报告只使用你确认提交的可见字段。",
            "截图之外的装备、配置、天赋与面板数值不会补全或推断。",
        ],
    )


def _parse_build_fields(value: Any) -> ScreenshotBuildFields:
    obj = value if isinstance(value, dict) else {}
    level_raw = obj.get("level")
    level = level_raw if isinstance(level_raw, int) and 1 <= level_raw <= 100 else None
    stats: dict[str, float] = {}
    for key, raw in _as_dict(obj.get("player_stats")).items():
        if key not in NUMERIC_STAT_KEYS or isinstance(raw, bool):
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            stats[key] = number
    text_stats = {
        key: _clean_text(raw, 100)
        for key, raw in _as_dict(obj.get("player_stat_text")).items()
        if key in TEXT_STAT_KEYS and _clean_text(raw, 100)
    }
    boss = _clean_text(obj.get("enemy_is_boss"), 20)
    if boss not in _BOSS_VALUES:
        boss = ""
    return ScreenshotBuildFields(
        level=level,
        class_name=_clean_text(obj.get("class_name"), 100),
        ascend_class_name=_clean_text(obj.get("ascend_class_name"), 100),
        main_skill=_clean_text(obj.get("main_skill"), 200),
        player_stats=stats,
        player_stat_text=text_stats,
        enemy_is_boss=boss,
    )


def _has_build_content(fields: ScreenshotBuildFields) -> bool:
    return bool(
        fields.level or fields.class_name or fields.ascend_class_name or fields.main_skill
        or fields.player_stats or fields.player_stat_text or fields.enemy_is_boss
    )


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\x00", "").strip()
    return text[:limit]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
