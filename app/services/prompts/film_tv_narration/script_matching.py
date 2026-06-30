#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
@Project: 影视解说-文案画面匹配
@File   : script_matching.py
@Description: 将用户审核后的影视解说文案匹配到字幕时间戳并生成最终剪辑脚本
"""

from ..base import ParameterizedPrompt, PromptMetadata, ModelType, OutputFormat


class ScriptMatchingPrompt(ParameterizedPrompt):
    """影视解说文案画面匹配提示词"""

    def __init__(self):
        metadata = PromptMetadata(
            name="script_matching",
            category="film_tv_narration",
            version="v1.0",
            description="将审核后的影视解说文案按叙事节奏拆分，并匹配到字幕时间戳生成最终剪辑JSON",
            model_type=ModelType.TEXT,
            output_format=OutputFormat.JSON,
            tags=["影视", "画面匹配", "剪辑脚本", "时间戳", "用户文案"],
            parameters=[
                "drama_name",
                "drama_genre",
                "plot_analysis",
                "subtitle_content",
                "narration_copy",
                "narration_language",
                "original_sound_ratio",
            ],
        )
        super().__init__(
            metadata,
            required_parameters=["drama_name", "subtitle_content", "narration_copy"],
        )

        self._system_prompt = (
            "你是一位懂影视叙事节奏的剪辑师。你必须严格输出JSON，"
            "核心任务是把带编号的用户审核解说文案完整映射到最合适的原视频字幕时间戳，"
            "不得跳过、删改或重写任何解说文案单元。"
        )

    def get_template(self) -> str:
        return """# 影视解说文案画面匹配任务

## 目标
用户已经审核并修改了解说文案。请根据这份文案和原始字幕，生成最终可剪辑 JSON 脚本。

## 作品名
${drama_name}

## 剧情理解材料
<plot>
${plot_analysis}
</plot>

## 用户审核后的解说文案单元
<narration_copy>
${narration_copy}
</narration_copy>

## 原始字幕（含视频编号和局部时间戳）
<subtitles>
${subtitle_content}
</subtitles>

## 输出语言
<narration_language>
${narration_language}
</narration_language>

## 用户选择的影视类型
<drama_genre>
${drama_genre}
</drama_genre>

## 用户选择的原片占比
<original_sound_ratio>
${original_sound_ratio}%
</original_sound_ratio>

## 匹配流程
1. narration_copy 已经由系统拆成稳定编号单元，格式类似 N001: 文案。必须以这些编号单元为准，不要重新自由拆分整段文案。
2. 每个编号单元必须被覆盖一次且只覆盖一次。允许一个 OST=0 合并多个相邻编号单元，但禁止跳号、乱序、重复或合并不相邻编号。
3. OST=0 narration 必须由对应编号单元的原文拼接而成；可以去掉编号本身和多余空格。除“平台安全表达”要求的敏感词替换外，不得改写、删句、压缩、总结或替换其中的深度旁白。
4. 严禁把解说文案匹配到片头、片尾、演职员表、版权声明、平台水印展示、下集预告、花絮、赞助口播、商品露出、贴片广告、中插广告、片中广告或任何与主线剧情无关的推广片段；这些内容绝对不能进入最终 items。
5. 如果字幕或画面文字出现“广告”“赞助”“推广”“片头”“片尾”“预告”“下集”“扫码”“购买”“会员”“关注”等明显非剧情信号，必须跳过对应时间段，不得用作 OST=0 或 OST=1。
6. 为每个解说单元或相邻单元组寻找最匹配的原始字幕画面，优先选择能表达该文案核心含义、人物状态、信息转折、价值冲突或主题意味的画面。
7. 使用公式估算所需画面时长：所需秒数 = 解说字数 / 5。匹配画面时长尽量接近，误差优先控制在 ±0.5 秒。
8. 如果一个编号单元太长，可以把同一个编号单元拆成多个连续 OST=0 片段，但这些片段的 source_narration_ids 都必须包含该编号，且 narration 合起来必须完整保留原文。
9. timestamp 必须使用对应 video_id 内部局部时间戳，不得换算为多个视频拼接后的累计时间。
10. 同一 video_id 内时间段不得交叉或重叠。
11. 第一段必须是 OST=0 解说钩子，不能直接播放原片。
12. OST=1 原声片段的总时长占比要尽量接近用户选择的 ${original_sound_ratio}%。这里按最终 items 的 timestamp 总时长估算，不按片段数量估算。
13. 不要自行判断或改写影视类型；画面匹配和 picture 描述要服务用户选择的 ${drama_genre} 叙事重点。
14. 最终 JSON 中每个 OST=0 item 必须包含 source_narration_ids 字段，列出它覆盖的编号，例如 ["N001"] 或 ["N002", "N003"]。OST=1 可以省略该字段或填空数组。
15. 输出前自查：所有输入的 N001 到最后一个编号都必须出现在 source_narration_ids 中，不能缺失。

## 原片占比规则
- ${original_sound_ratio}% = 0% 时，不要输出 OST=1，全部使用解说承接。
- ${original_sound_ratio}% 在 10%-30% 时，只保留关键对白、信息反转、情绪爆发或名场面原声。
- ${original_sound_ratio}% 在 40%-60% 时，解说负责串联因果，原片负责承载关键场面和对白。
- ${original_sound_ratio}% 在 70%-90% 时，以原片对白和表演为主，解说只做开场钩子、转场桥和必要补充。
- 如果原片占比与“第一段必须 OST=0”冲突，优先保证第一段是 OST=0，然后在后续片段提高 OST=1 时长占比。
- 选择高原片占比时，可以把相邻编号单元合并成更少的 OST=0 桥段，但仍必须完整覆盖所有编号单元，不能删减用户文案。

## 字段规则
- _id：从 1 开始连续递增。
- video_id：来自字幕分段标题，例如“视频 2”就填 2。
- video_name：对应视频文件名，必须从字幕分段标题提取。
- timestamp：格式为 "HH:MM:SS,mmm-HH:MM:SS,mmm"。
- picture：描述匹配画面中人物、动作、情绪、场景和关键道具。
- narration：OST=0 时填写 source_narration_ids 对应编号单元的原文，去掉编号；OST=1 时填写“播放原片+_id”。
- OST：解说片段填 0，原声片段填 1。
- source_narration_ids：仅 OST=0 必填，用于校验文案覆盖率；必须是编号字符串数组。

## 平台安全表达
- 最终 narration 必须适合国内短视频平台审核，不要出现露骨敏感词，例如“口交”“性爱”“强奸”“性侵”“做爱”等。
- 如果用户审核文案单元或字幕中包含这类词，OST=0 narration 必须在不改变剧情含义的前提下改写为更安全表达，例如“发生亲密关系”“遭到侵害”“被控制”“被伤害”“制造受害者叙事”。
- 不要渲染具体性行为、伤害过程或血腥细节，只保留剧情所需的因果信息。

## 输出格式
只输出严格 JSON：

{
  "items": [
    {
      "_id": 1,
      "video_id": 1,
      "video_name": "1.mp4",
      "timestamp": "00:00:01,000-00:00:06,000",
      "picture": "主角站在走廊尽头，回头看向紧闭的房门",
      "narration": "他以为自己终于逃出了那间房，可真正的危险，其实才刚刚醒来。",
      "source_narration_ids": ["N001"],
      "OST": 0
    }
  ]
}

现在请基于用户审核后的解说文案生成最终剪辑脚本。"""
