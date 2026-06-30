#!/usr/bin/env python
# -*- coding: UTF-8 -*-

'''
@Project: NarratoAI
@File   : 短剧解说脚本生成
@Author : 小林同学
@Date   : 2025/5/10 下午10:26 
'''
import os
import json
import time
import traceback
import html
import streamlit as st
from loguru import logger

from app.config import config
from app.services.SDE.short_drama_explanation import (
    analyze_subtitle,
    generate_narration_copy as generate_narration_copy_legacy,
    match_narration_copy_to_script as match_narration_copy_to_script_legacy,
)
from app.services.subtitle_text import read_subtitle_text
from app.services.short_drama_narration_validation import (
    normalize_script_video_sources,
)
from app.services.tavily_search import TavilySearchError, format_search_context, search_story_context
from app.utils import utils
# 导入新的LLM服务模块 - 确保提供商被注册
import app.services.llm  # 这会触发提供商注册
from app.services.llm.migration_adapter import SubtitleAnalyzerAdapter
import re


PUBLIC_SCRIPT_FIELDS = ["_id", "video_id", "video_name", "timestamp", "picture", "narration", "OST"]
EPISODE_SCRIPT_MATCH_CACHE_VERSION = 3
SHORT_DRAMA_PROMPT_CATEGORY = "short_drama_narration"
FILM_TV_PROMPT_CATEGORY = "film_tv_narration"
SHORT_DRAMA_SEARCH_KEYWORDS = "短剧 剧情 介绍 人物 结局"
FILM_TV_SEARCH_KEYWORDS = "影视 剧情 介绍 人物 结局 电影 电视剧"
NARRATION_UNIT_ID_RE = re.compile(r"N\d{3}")
NARRATION_SCOPE_OVERALL = "overall"
NARRATION_SCOPE_EPISODE = "episode"
EPISODE_NARRATION_MARKER = "[EPISODE_NARRATION_MODE]"
EPISODE_NARRATION_HEADING_RE = re.compile(r"^第\s*\d+\s*(集|话|話)(\s*[:：\-].{0,40})?$")
EPISODE_NARRATION_HEADING_CAPTURE_RE = re.compile(r"^第\s*(?P<episode_index>\d+)\s*(集|话|話)(\s*[:：\-].{0,40})?$")


class NarrationUnitCoverageError(Exception):
    def __init__(self, missing_unit_ids):
        self.missing_unit_ids = list(missing_unit_ids or [])
        super().__init__(", ".join(self.missing_unit_ids))


def _normalize_paths(paths):
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return []

    normalized_paths = []
    seen = set()
    for path in paths:
        if not isinstance(path, str):
            continue
        path = path.strip()
        if not path or path in seen:
            continue
        normalized_paths.append(path)
        seen.add(path)
    return normalized_paths


def _build_combined_subtitle_content(subtitle_paths, video_paths=None):
    sections = []
    video_paths = _normalize_paths(video_paths)
    for index, subtitle_path in enumerate(_normalize_paths(subtitle_paths), start=1):
        if not os.path.exists(subtitle_path):
            continue

        video_path = video_paths[index - 1] if index <= len(video_paths) else ""
        if video_path:
            header = (
                f"# 视频 {index}: {os.path.basename(video_path)}\n"
                f"字幕文件: {os.path.basename(subtitle_path)}"
            )
        else:
            header = f"# 视频 {index}\n字幕文件: {os.path.basename(subtitle_path)}"
        sections.append(f"{header}\n{read_subtitle_text(subtitle_path).text}".strip())

    return "\n\n".join(sections)


def _build_episode_subtitle_sections(subtitle_paths, video_paths=None):
    episodes = []
    video_paths = _normalize_paths(video_paths)
    for index, subtitle_path in enumerate(_normalize_paths(subtitle_paths), start=1):
        if not os.path.exists(subtitle_path):
            continue

        video_path = video_paths[index - 1] if index <= len(video_paths) else ""
        episodes.append({
            "episode_index": index,
            "subtitle_path": subtitle_path,
            "subtitle_name": os.path.basename(subtitle_path),
            "video_path": video_path,
            "video_name": os.path.basename(video_path) if video_path else "",
            "content": read_subtitle_text(subtitle_path).text,
        })
    return episodes


def _build_episode_narration_input(episode, total_episodes: int, target_chars: int) -> str:
    video_name = episode.get("video_name") or f"Episode {episode.get('episode_index')}"
    subtitle_name = episode.get("subtitle_name") or ""
    return f"""\
{EPISODE_NARRATION_MARKER}
episode_index: {episode.get("episode_index")}
episode_total: {total_episodes}
video_name: {video_name}
subtitle_file: {subtitle_name}
target_chars: {target_chars}

episode_requirements:
- Only write narration for this episode's subtitle content.
- Open with the strongest highlight or moral dilemma from this episode.
- Expand chronological plot narration after the hook; do not write a whole-season overview.
- Keep roughly 70% plot narration, 20% character motive/relationship, 10% theme.
- End with this episode's unresolved suspense or emotional turn if the subtitle supports it.

episode_subtitles:
{episode.get("content") or ""}
""".strip()


def _build_episode_plot_analysis_input(episode, total_episodes: int, series_analysis: str = "") -> str:
    video_name = episode.get("video_name") or f"Episode {episode.get('episode_index')}"
    subtitle_name = episode.get("subtitle_name") or ""
    series_analysis = str(series_analysis or "").strip()
    series_context = f"""
# 全局剧情理解参考
以下内容只用于统一人物关系、作品背景和前情，不得覆盖当前集字幕事实。
{series_analysis}
""".strip() if series_analysis else ""
    return f"""\
# 当前集信息
- episode_index: {episode.get("episode_index")}
- episode_total: {total_episodes}
- video_name: {video_name}
- subtitle_file: {subtitle_name}

{series_context}

# 当前集字幕
{episode.get("content") or ""}
""".strip()


def _build_episode_matching_subtitle_content(episode) -> str:
    episode_index = int(episode.get("episode_index") or 1)
    video_name = episode.get("video_name") or f"Episode {episode_index}"
    subtitle_name = episode.get("subtitle_name") or ""
    header = (
        f"# 视频 {episode_index}: {video_name}\n"
        f"字幕文件: {subtitle_name}"
    )
    return f"{header}\n{episode.get('content') or ''}".strip()


def _split_episode_narration_copy(narration_copy: str):
    sections = []
    current_episode_index = None
    current_lines = []

    def flush_current():
        if current_episode_index is None:
            return
        text = "\n".join(current_lines).strip()
        if text:
            sections.append({
                "episode_index": current_episode_index,
                "text": text,
            })

    for raw_line in str(narration_copy or "").splitlines():
        line = raw_line.strip()
        heading_match = EPISODE_NARRATION_HEADING_CAPTURE_RE.match(line)
        if heading_match:
            flush_current()
            current_episode_index = int(heading_match.group("episode_index"))
            current_lines = []
            continue
        if current_episode_index is not None:
            current_lines.append(raw_line)

    flush_current()
    return sections


def _normalize_narration_items_video_sources(items, video_paths):
    return normalize_script_video_sources(items, _normalize_paths(video_paths))


def _strip_planner_only_fields(items):
    return [
        {field: item[field] for field in PUBLIC_SCRIPT_FIELDS if field in item}
        for item in items
        if isinstance(item, dict)
    ]


def _split_narration_copy_into_units(narration_copy: str, max_chars: int = 140):
    """Split editable narration text into stable units for timeline matching."""
    text = str(narration_copy or "").strip()
    if not text:
        return []
    text = "\n".join(
        line for line in text.splitlines()
        if not EPISODE_NARRATION_HEADING_RE.match(line.strip())
    ).strip()
    if not text:
        return []

    fragments = []
    current = []
    for char in text:
        current.append(char)
        if char in "。！？!?；;…\n":
            fragment = "".join(current).strip()
            if fragment:
                fragments.append(fragment)
            current = []
    tail = "".join(current).strip()
    if tail:
        fragments.append(tail)

    normalized = []
    for fragment in fragments:
        fragment = re.sub(r"\s+", " ", fragment).strip()
        if not fragment:
            continue
        if normalized and len(fragment) <= 12:
            normalized[-1] = f"{normalized[-1]}{fragment}"
            continue
        if len(fragment) > max_chars:
            parts = re.split(r"(?<=[，,、])", fragment)
            buffer = ""
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if buffer and len(buffer) + len(part) > max_chars:
                    normalized.append(buffer)
                    buffer = part
                else:
                    buffer = f"{buffer}{part}" if buffer else part
            if buffer:
                normalized.append(buffer)
        else:
            normalized.append(fragment)

    units = []
    for index, unit_text in enumerate(normalized, start=1):
        unit_text = unit_text.strip()
        if unit_text:
            units.append({"id": f"N{index:03d}", "text": unit_text})
    return units


def _format_narration_units_for_prompt(units):
    return "\n".join(f"{unit['id']}: {unit['text']}" for unit in units)


def _collect_script_narration_unit_ids(items):
    covered_ids = []
    for item in items or []:
        if not isinstance(item, dict) or int(item.get("OST", 0) or 0) != 0:
            continue
        raw_ids = item.get("source_narration_ids") or item.get("narration_unit_ids") or []
        if isinstance(raw_ids, str):
            ids = NARRATION_UNIT_ID_RE.findall(raw_ids)
        elif isinstance(raw_ids, list):
            ids = []
            for raw_id in raw_ids:
                ids.extend(NARRATION_UNIT_ID_RE.findall(str(raw_id)))
        else:
            ids = []
        covered_ids.extend(ids)
    return covered_ids


def _validate_narration_unit_coverage(items, units):
    expected_ids = [unit["id"] for unit in units]
    if not expected_ids:
        return []
    covered_ids = _collect_script_narration_unit_ids(items)
    covered_set = set(covered_ids)
    return [unit_id for unit_id in expected_ids if unit_id not in covered_set]


def _normalize_match_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _fill_missing_narration_unit_ids_from_text(items, units):
    """Recover source_narration_ids when the model kept narration text but forgot ids."""
    if not items or not units:
        return items

    missing_ids = _validate_narration_unit_coverage(items, units)
    if not missing_ids:
        return items

    unit_by_id = {unit["id"]: unit for unit in units}
    recovered_ids = []
    for missing_id in missing_ids:
        unit_text = _normalize_match_text(unit_by_id.get(missing_id, {}).get("text", ""))
        if not unit_text:
            continue
        for item in items:
            if not isinstance(item, dict) or int(item.get("OST", 0) or 0) != 0:
                continue
            item_text = _normalize_match_text(item.get("narration", ""))
            if unit_text and (unit_text in item_text or item_text in unit_text):
                raw_ids = item.get("source_narration_ids") or item.get("narration_unit_ids") or []
                if isinstance(raw_ids, str):
                    source_ids = NARRATION_UNIT_ID_RE.findall(raw_ids)
                elif isinstance(raw_ids, list):
                    source_ids = [str(raw_id) for raw_id in raw_ids]
                else:
                    source_ids = []
                if missing_id not in source_ids:
                    source_ids.append(missing_id)
                source_ids.sort(key=lambda value: int(NARRATION_UNIT_ID_RE.search(str(value)).group()[1:]) if NARRATION_UNIT_ID_RE.search(str(value)) else 9999)
                item["source_narration_ids"] = source_ids
                recovered_ids.append(missing_id)
                break

    if recovered_ids:
        logger.info(f"已从 narration 文本自动补齐解说文案单元编号: {recovered_ids}")
    return items


def _parse_time_range_seconds(timestamp: str):
    match = re.match(
        r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})-(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})",
        str(timestamp or "").strip(),
    )
    if not match:
        return None
    start = (
        int(match.group("sh")) * 3600
        + int(match.group("sm")) * 60
        + int(match.group("ss"))
        + int(match.group("sms")) / 1000
    )
    end = (
        int(match.group("eh")) * 3600
        + int(match.group("em")) * 60
        + int(match.group("es"))
        + int(match.group("ems")) / 1000
    )
    return start, end


def _format_seconds_as_srt_time(seconds: float) -> str:
    seconds = max(float(seconds or 0), 0.0)
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis >= 1000:
        whole += 1
        millis -= 1000
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _make_time_range(start: float, end: float) -> str:
    return f"{_format_seconds_as_srt_time(start)}-{_format_seconds_as_srt_time(end)}"


def _episode_content_time_bounds(episode):
    ranges = [
        parsed
        for parsed in (
            _parse_time_range_seconds(match.group(0))
            for match in re.finditer(
                r"\d{2}:\d{2}:\d{2},\d{3}-\d{2}:\d{2}:\d{2},\d{3}",
                str(episode.get("content") or ""),
            )
        )
        if parsed
    ]
    if not ranges:
        return 0.0, 60.0
    return ranges[0][0], ranges[-1][1]


def _append_missing_narration_units_as_items(items, units, episode):
    missing_ids = _validate_narration_unit_coverage(items, units)
    if not missing_ids:
        return items

    unit_by_id = {unit["id"]: unit for unit in units}
    episode_index = int(episode.get("episode_index") or 1)
    episode_video_name = episode.get("video_name") or ""
    _, episode_end = _episode_content_time_bounds(episode)
    latest_end = 0.0
    for item in items or []:
        parsed = _parse_time_range_seconds(item.get("timestamp", ""))
        if parsed:
            latest_end = max(latest_end, parsed[1])

    cursor = min(max(latest_end + 0.2, 0.0), max(episode_end - 1.0, 0.0))
    next_id = max([int(item.get("_id", 0) or 0) for item in items or []] + [0]) + 1
    appended = []
    for missing_id in missing_ids:
        unit_text = str(unit_by_id.get(missing_id, {}).get("text") or "").strip()
        if not unit_text:
            continue
        duration = max(2.0, min(len(unit_text) / 5.0, 12.0))
        if cursor + duration > episode_end:
            cursor = max(0.0, episode_end - duration)
        end = min(cursor + duration, episode_end)
        if end <= cursor:
            end = cursor + duration
        item = {
            "_id": next_id,
            "video_id": episode_index,
            "video_name": episode_video_name,
            "timestamp": _make_time_range(cursor, end),
            "picture": f"第 {episode_index} 集中与解说单元 {missing_id} 对应的剧情承接画面",
            "narration": unit_text,
            "source_narration_ids": [missing_id],
            "OST": 0,
        }
        items.append(item)
        appended.append(missing_id)
        next_id += 1
        cursor = end + 0.2

    if appended:
        logger.warning(f"已追加缺失解说文案单元为兜底片段: {appended}")
    return items


def _episode_source_signature(episode):
    subtitle_path = str(episode.get("subtitle_path") or "")
    video_path = str(episode.get("video_path") or "")
    parts = [
        str(episode.get("episode_index") or ""),
        subtitle_path,
        video_path,
    ]
    for path in (subtitle_path, video_path):
        try:
            parts.append(str(os.path.getmtime(path)))
            parts.append(str(os.path.getsize(path)))
        except OSError:
            parts.append("")
            parts.append("")
    return "|".join(parts)


def _episode_match_cache_key(
    *,
    video_theme,
    episode,
    narration_text,
    plot_analysis,
    narration_language,
    drama_genre,
    original_sound_ratio,
    prompt_category,
):
    payload = {
        "version": EPISODE_SCRIPT_MATCH_CACHE_VERSION,
        "video_theme": str(video_theme or ""),
        "episode_source": _episode_source_signature(episode),
        "narration_text": str(narration_text or ""),
        "plot_analysis": str(plot_analysis or ""),
        "narration_language": str(narration_language or ""),
        "drama_genre": str(drama_genre or ""),
        "original_sound_ratio": int(original_sound_ratio or 0),
        "prompt_category": str(prompt_category or ""),
    }
    return utils.md5(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _episode_match_cache_path(cache_key):
    return os.path.join(utils.temp_dir("episode_script_cache"), f"{cache_key}.json")


def _load_episode_match_cache(cache_key):
    cache_path = _episode_match_cache_path(cache_key)
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except Exception as e:
        logger.warning(f"读取逐集剪辑脚本缓存失败，将重新生成: {cache_path}, {e}")
        return None
    if cached.get("status") != "success" or not isinstance(cached.get("items"), list):
        return None
    return cached


def _save_episode_match_cache(cache_key, payload):
    cache_path = _episode_match_cache_path(cache_key)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"写入逐集剪辑脚本缓存失败: {cache_path}, {e}")
    return cache_path


def _format_progress_status(progress, message: str = "", tr=lambda key: key):
    message = str(message or "").strip()
    if message:
        return message
    return f"{tr('Progress')}: {progress}%"


def parse_and_fix_json(json_string):
    """
    解析并修复JSON字符串

    Args:
        json_string: 待解析的JSON字符串

    Returns:
        dict: 解析后的字典，如果解析失败返回None
    """
    if not json_string or not json_string.strip():
        logger.error("JSON字符串为空")
        return None

    # 清理字符串
    json_string = json_string.strip()

    # 尝试直接解析
    try:
        return json.loads(json_string)
    except json.JSONDecodeError as e:
        logger.warning(f"直接JSON解析失败: {e}")

    # 尝试修复双大括号问题（LLM生成的常见问题）
    try:
        # 将双大括号替换为单大括号
        fixed_braces = json_string.replace('{{', '{').replace('}}', '}')
        logger.info("修复双大括号格式")
        return json.loads(fixed_braces)
    except json.JSONDecodeError:
        pass

    # 尝试提取JSON部分
    try:
        # 查找JSON代码块
        json_match = re.search(r'```json\s*(.*?)\s*```', json_string, re.DOTALL)
        if json_match:
            json_content = json_match.group(1).strip()
            logger.info("从代码块中提取JSON内容")
            return json.loads(json_content)
    except json.JSONDecodeError:
        pass

    # 尝试查找大括号包围的内容
    try:
        # 查找第一个 { 到最后一个 } 的内容
        start_idx = json_string.find('{')
        end_idx = json_string.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_content = json_string[start_idx:end_idx+1]
            logger.info("提取大括号包围的JSON内容")
            return json.loads(json_content)
    except json.JSONDecodeError:
        pass

    # 尝试综合修复JSON格式问题
    try:
        fixed_json = json_string

        # 1. 修复双大括号问题
        fixed_json = fixed_json.replace('{{', '{').replace('}}', '}')

        # 2. 提取JSON内容（如果有其他文本包围）
        start_idx = fixed_json.find('{')
        end_idx = fixed_json.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            fixed_json = fixed_json[start_idx:end_idx+1]

        # 3. 移除注释
        fixed_json = re.sub(r'#.*', '', fixed_json)
        fixed_json = re.sub(r'//.*', '', fixed_json)

        # 4. 移除多余的逗号
        fixed_json = re.sub(r',\s*}', '}', fixed_json)
        fixed_json = re.sub(r',\s*]', ']', fixed_json)

        # 5. 修复单引号
        fixed_json = re.sub(r"'([^']*)':", r'"\1":', fixed_json)

        # 6. 修复没有引号的属性名
        fixed_json = re.sub(r'(\w+)(\s*):', r'"\1"\2:', fixed_json)

        # 7. 修复重复的引号
        fixed_json = re.sub(r'""([^"]*?)""', r'"\1"', fixed_json)

        logger.info("尝试综合修复JSON格式问题后解析")
        return json.loads(fixed_json)
    except json.JSONDecodeError as e:
        logger.debug(f"综合修复失败: {e}")
        pass

    # 如果所有方法都失败，直接返回 None，避免生成不可剪辑的默认假脚本
    logger.error(f"所有JSON解析方法都失败，原始内容: {json_string[:200]}...")
    return None


def _get_tavily_api_key() -> str:
    return (
        st.session_state.get("tavily_api_key")
        or config.app.get("tavily_api_key")
        or ""
    ).strip()


def _build_tavily_context(
    title: str,
    tr=lambda key: key,
    search_keywords: str = SHORT_DRAMA_SEARCH_KEYWORDS,
    empty_title_message_key: str = "Please enter short drama name before web search",
) -> str | None:
    title = str(title or "").strip()
    if not title:
        st.error(tr(empty_title_message_key))
        return None

    api_key = _get_tavily_api_key()
    if not api_key:
        st.error(tr("Please configure Tavily API Key in Basic Settings"))
        return None

    try:
        search_data = search_story_context(
            title,
            api_key,
            search_keywords=search_keywords,
            empty_name_message=tr(empty_title_message_key),
            search_depth=config.app.get("tavily_search_depth", "basic"),
            max_results=config.app.get("tavily_max_results", 5),
        )
        return format_search_context(search_data)
    except TavilySearchError as e:
        logger.error(f"Tavily 短剧检索失败: {str(e)}")
        st.error(f"{tr('Tavily search failed')}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Tavily 短剧检索异常: {traceback.format_exc()}")
        st.error(f"{tr('Tavily search failed')}: {str(e)}")
        return None


def _build_plot_analysis_input(
    subtitle_content: str,
    short_name: str = "",
    enable_web_search: bool = False,
    tr=lambda key: key,
    search_keywords: str = SHORT_DRAMA_SEARCH_KEYWORDS,
    empty_title_message_key: str = "Please enter short drama name before web search",
    web_search_context_description: str = "短剧名称、人物关系、剧情背景和公开剧情梗概",
) -> str | None:
    subtitle_content = str(subtitle_content or "").strip()
    if not enable_web_search:
        return subtitle_content

    tavily_context = _build_tavily_context(
        short_name,
        tr,
        search_keywords=search_keywords,
        empty_title_message_key=empty_title_message_key,
    )
    if tavily_context is None:
        return None

    return f"""# 分析补充说明
请先参考 Tavily 联网检索结果理解{web_search_context_description}，再结合原始字幕完成剧情理解。
如果联网检索结果与字幕内容冲突，请以字幕内容为准；时间戳必须只从字幕内容中提取。

{tavily_context}

# 原始字幕
{subtitle_content}"""


def analyze_short_drama_plot(
    subtitle_path,
    temperature,
    tr=lambda key: key,
    subtitle_content=None,
    short_name: str = "",
    enable_web_search: bool = False,
    video_paths=None,
    prompt_category: str = SHORT_DRAMA_PROMPT_CATEGORY,
    search_keywords: str = SHORT_DRAMA_SEARCH_KEYWORDS,
    empty_title_message_key: str = "Please enter short drama name before web search",
    web_search_context_description: str = "短剧名称、人物关系、剧情背景和公开剧情梗概",
    narration_scope: str = NARRATION_SCOPE_OVERALL,
):
    """仅执行短剧字幕剧情理解，返回可编辑的剧情分析文本。"""
    subtitle_paths = _normalize_paths(subtitle_path)
    if not subtitle_paths:
        st.error(tr("Please generate or upload subtitles first"))
        return None
    missing_subtitle_paths = [path for path in subtitle_paths if not os.path.exists(path)]
    if missing_subtitle_paths:
        st.error(tr("Subtitle file does not exist"))
        return None

    text_provider = config.app.get('text_llm_provider', 'gemini').lower()
    text_api_key = config.app.get(f'text_{text_provider}_api_key')
    text_model = config.app.get(f'text_{text_provider}_model_name')
    text_base_url = config.app.get(f'text_{text_provider}_base_url')

    def run_plot_analysis(current_input: str, save_result: bool = True, log_context: str = ""):
        try:
            logger.info(f"使用新的LLM服务架构进行字幕分析{log_context}")
            analyzer = SubtitleAnalyzerAdapter(
                text_api_key,
                text_model,
                text_base_url,
                text_provider,
                prompt_category=prompt_category,
            )
            return analyzer.analyze_subtitle(current_input)
        except Exception as e:
            logger.warning(f"使用新LLM服务失败，回退到旧实现: {str(e)}")
            return analyze_subtitle(
                subtitle_content=current_input,
                api_key=text_api_key,
                model=text_model,
                base_url=text_base_url,
                save_result=save_result,
                temperature=temperature,
                provider=text_provider,
                prompt_category=prompt_category,
            )

    use_episode_plot_analysis = (
        prompt_category == FILM_TV_PROMPT_CATEGORY
        and narration_scope == NARRATION_SCOPE_EPISODE
        and len(subtitle_paths) > 1
    )
    if use_episode_plot_analysis:
        episodes = _build_episode_subtitle_sections(subtitle_paths, video_paths)
        if not episodes:
            st.error(tr("Subtitle file is empty or unreadable"))
            return None

        tavily_context = ""
        if enable_web_search:
            tavily_context = _build_tavily_context(
                short_name,
                tr,
                search_keywords=search_keywords,
                empty_title_message_key=empty_title_message_key,
            )
            if tavily_context is None:
                return None

        episode_analyses = []
        episode_status = st.empty()
        total_episodes = len(episodes)
        for episode in episodes:
            episode_index = episode["episode_index"]
            episode_status.text(f"{tr('Analyzing plot...')} ({episode_index}/{total_episodes})")
            episode_input = _build_episode_plot_analysis_input(episode, total_episodes)
            if tavily_context:
                episode_input = f"""# 联网检索参考
以下内容只用于辅助统一作品背景和人物称呼；具体时间戳、事件和分集内容必须以当前集字幕为准。
{tavily_context}

{episode_input}"""
            analysis_result = run_plot_analysis(
                episode_input,
                save_result=False,
                log_context=f"（第 {episode_index} 集）",
            )
            if analysis_result["status"] != "success":
                logger.error(f"第 {episode_index} 集剧情理解失败: {analysis_result['message']}")
                st.error(tr("Script generation failed check logs"))
                episode_status.empty()
                return None
            episode_analyses.append(
                f"## 第{episode_index}集：{episode.get('video_name') or episode.get('subtitle_name') or ''}\n"
                f"{analysis_result['analysis']}"
            )
        episode_status.empty()
        return (
            f"# 逐集剧情理解\n"
            f"- 作品名称：{short_name or ''}\n"
            f"- 字幕文件数：{total_episodes}\n"
            f"- 处理方式：逐集分析后合并，避免长剧整季字幕被模型截断或只识别前几集。\n\n"
            + "\n\n".join(episode_analyses)
        )

    subtitle_content = str(subtitle_content or "").strip() or _build_combined_subtitle_content(
        subtitle_paths,
        video_paths,
    )
    if not subtitle_content:
        st.error(tr("Subtitle file is empty or unreadable"))
        return None

    plot_analysis_input = _build_plot_analysis_input(
        subtitle_content,
        short_name=short_name,
        enable_web_search=enable_web_search,
        tr=tr,
        search_keywords=search_keywords,
        empty_title_message_key=empty_title_message_key,
        web_search_context_description=web_search_context_description,
    )
    if plot_analysis_input is None:
        return None

    analysis_result = run_plot_analysis(plot_analysis_input)

    if analysis_result["status"] != "success":
        logger.error(f"分析失败: {analysis_result['message']}")
        st.error(tr("Script generation failed check logs"))
        return None

    return analysis_result["analysis"]


def generate_short_drama_narration_copy(
    subtitle_path,
    video_theme,
    temperature,
    tr=lambda key: key,
    plot_analysis=None,
    subtitle_content=None,
    enable_web_search: bool = False,
    video_paths=None,
    narration_language: str = "简体中文（中国）",
    drama_genre: str = "逆袭/复仇",
    prompt_category: str = SHORT_DRAMA_PROMPT_CATEGORY,
    search_keywords: str = SHORT_DRAMA_SEARCH_KEYWORDS,
    empty_title_message_key: str = "Please enter short drama name before web search",
    web_search_context_description: str = "短剧名称、人物关系、剧情背景和公开剧情梗概",
    narration_scope: str = NARRATION_SCOPE_OVERALL,
    episode_target_chars: int = 1200,
    episode_plot_analysis: bool = True,
):
    """生成可由用户审核修改的短剧解说正文，不绑定时间戳。"""
    subtitle_paths = _normalize_paths(subtitle_path)
    if not subtitle_paths:
        st.error(tr("Please generate or upload subtitles first"))
        return None
    missing_subtitle_paths = [path for path in subtitle_paths if not os.path.exists(path)]
    if missing_subtitle_paths:
        st.error(tr("Subtitle file does not exist"))
        return None

    selected_video_paths = _normalize_paths(video_paths)
    subtitle_content = str(subtitle_content or "").strip() or _build_combined_subtitle_content(
        subtitle_paths,
        selected_video_paths,
    )
    if not subtitle_content:
        st.error(tr("Subtitle file is empty or unreadable"))
        return None

    analysis_text = str(plot_analysis or "").strip()
    if not analysis_text:
        analysis_text = analyze_short_drama_plot(
            subtitle_paths,
            temperature,
            tr,
            subtitle_content=subtitle_content,
            short_name=video_theme,
            enable_web_search=enable_web_search,
            video_paths=selected_video_paths,
            prompt_category=prompt_category,
            search_keywords=search_keywords,
            empty_title_message_key=empty_title_message_key,
            web_search_context_description=web_search_context_description,
            narration_scope=narration_scope,
        )
        if not analysis_text:
            return None

    text_provider = config.app.get('text_llm_provider', 'gemini').lower()
    text_api_key = config.app.get(f'text_{text_provider}_api_key')
    text_model = config.app.get(f'text_{text_provider}_model_name')
    text_base_url = config.app.get(f'text_{text_provider}_base_url')

    def analyze_plot_for_subtitles(current_subtitle_content: str, log_context: str = ""):
        try:
            logger.info(f"使用新的LLM服务架构进行字幕分析{log_context}")
            analyzer = SubtitleAnalyzerAdapter(
                text_api_key,
                text_model,
                text_base_url,
                text_provider,
                prompt_category=prompt_category,
            )
            return analyzer.analyze_subtitle(current_subtitle_content)
        except Exception as e:
            logger.warning(f"使用新LLM服务分析字幕失败，回退到旧实现: {str(e)}")
            return analyze_subtitle(
                subtitle_content=current_subtitle_content,
                api_key=text_api_key,
                model=text_model,
                base_url=text_base_url,
                save_result=False,
                temperature=temperature,
                provider=text_provider,
                prompt_category=prompt_category,
            )

    def generate_copy_for_subtitles(
        current_subtitle_content: str,
        log_context: str = "",
        current_plot_analysis: str | None = None,
    ):
        plot_analysis_for_copy = str(current_plot_analysis or analysis_text or "").strip()
        try:
            logger.info(f"使用新的LLM服务架构生成可审核解说文案{log_context}")
            analyzer = SubtitleAnalyzerAdapter(
                text_api_key,
                text_model,
                text_base_url,
                text_provider,
                prompt_category=prompt_category,
            )
            return analyzer.generate_narration_copy(
                short_name=video_theme,
                plot_analysis=plot_analysis_for_copy,
                subtitle_content=current_subtitle_content,
                temperature=temperature,
                narration_language=narration_language,
                drama_genre=drama_genre,
            )
        except Exception as e:
            logger.warning(f"使用新LLM服务生成文案失败，回退到旧实现: {str(e)}")
            return generate_narration_copy_legacy(
                short_name=video_theme,
                plot_analysis=plot_analysis_for_copy,
                subtitle_content=current_subtitle_content,
                api_key=text_api_key,
                model=text_model,
                base_url=text_base_url,
                temperature=temperature,
                provider=text_provider,
                narration_language=narration_language,
                drama_genre=drama_genre,
                prompt_category=prompt_category,
            )

    use_episode_scope = (
        prompt_category == FILM_TV_PROMPT_CATEGORY
        and narration_scope == NARRATION_SCOPE_EPISODE
    )
    if use_episode_scope:
        episodes = _build_episode_subtitle_sections(subtitle_paths, selected_video_paths)
        if not episodes:
            st.error(tr("Subtitle file is empty or unreadable"))
            return None

        episode_copies = []
        total_episodes = len(episodes)
        target_chars = max(600, min(int(episode_target_chars or 1200), 2500))
        episode_status = st.empty()
        for episode in episodes:
            episode_index = episode["episode_index"]
            episode_status.text(f"{tr('Generating narration copy...')} ({episode_index}/{total_episodes})")
            episode_analysis_text = analysis_text
            if episode_plot_analysis:
                episode_plot_input = _build_episode_plot_analysis_input(
                    episode,
                    total_episodes,
                    series_analysis=analysis_text,
                )
                episode_analysis_result = analyze_plot_for_subtitles(
                    episode_plot_input,
                    f"（第 {episode_index} 集）",
                )
                if episode_analysis_result.get("status") != "success":
                    logger.error(f"第 {episode_index} 集剧情理解失败: {episode_analysis_result.get('message')}")
                    st.error(tr("Script generation failed check logs"))
                    episode_status.empty()
                    return None
                episode_analysis_text = (
                    f"# 全局剧情理解参考\n{analysis_text}\n\n"
                    f"# 第 {episode_index} 集剧情理解\n{episode_analysis_result.get('analysis', '')}"
                ).strip()

            episode_input = _build_episode_narration_input(episode, total_episodes, target_chars)
            narration_result = generate_copy_for_subtitles(
                episode_input,
                f"（第 {episode_index} 集）",
                current_plot_analysis=episode_analysis_text,
            )
            if narration_result.get("status") != "success":
                logger.error(f"第 {episode_index} 集解说文案生成失败: {narration_result.get('message')}")
                st.error(tr("Script generation failed check logs"))
                episode_status.empty()
                return None

            episode_copy = str(narration_result.get("narration_copy", "")).strip()
            if not episode_copy:
                logger.error(f"第 {episode_index} 集模型返回空解说文案正文")
                st.error(tr("Generated narration copy is empty"))
                episode_status.empty()
                return None
            episode_copies.append(f"第{episode_index}集\n{episode_copy}")

        episode_status.empty()
        return {
            "narration_copy": "\n\n".join(episode_copies),
            "plot_analysis": analysis_text,
            "subtitle_content": subtitle_content,
        }

    narration_result = generate_copy_for_subtitles(subtitle_content)

    if narration_result.get("status") != "success":
        logger.error(f"解说文案正文生成失败: {narration_result.get('message')}")
        st.error(tr("Script generation failed check logs"))
        return None

    narration_copy = str(narration_result.get("narration_copy", "")).strip()
    if not narration_copy:
        logger.error("模型返回空解说文案正文")
        st.error(tr("Generated narration copy is empty"))
        return None

    return {
        "narration_copy": narration_copy,
        "plot_analysis": analysis_text,
        "subtitle_content": subtitle_content,
    }


def generate_script_short_sunmmary(
    params,
    subtitle_path,
    video_theme,
    temperature,
    tr=lambda key: key,
    plot_analysis=None,
    subtitle_content=None,
    enable_web_search: bool = False,
    video_paths=None,
    narration_language: str = "简体中文（中国）",
    narration_copy: str = "",
    drama_genre: str = "逆袭/复仇",
    original_sound_ratio: int = 30,
    prompt_category: str = SHORT_DRAMA_PROMPT_CATEGORY,
    search_keywords: str = SHORT_DRAMA_SEARCH_KEYWORDS,
    empty_title_message_key: str = "Please enter short drama name before web search",
    web_search_context_description: str = "短剧名称、人物关系、剧情背景和公开剧情梗概",
    narration_scope: str = NARRATION_SCOPE_OVERALL,
):
    """
    生成 短剧解说 视频脚本
    要求: 提供高质量短剧字幕
    适合场景: 短剧
    """
    progress_bar = st.empty()
    status_text = st.empty()
    stream_text = st.empty()
    stream_state = {
        "reasoning": "",
        "content": "",
        "last_update": 0.0,
    }

    def update_progress(progress: float, message: str = ""):
        progress_bar.progress(progress)
        status_text.text(_format_progress_status(progress, message, tr))

    def update_waiting(message: str = ""):
        progress_bar.empty()
        if message:
            status_text.text(message)
        else:
            status_text.empty()

    def update_stream_window(event):
        event = event or {}
        chunk_type = str(event.get("type") or "content")
        chunk_text = str(event.get("text") or "")
        if chunk_type == "done" or not chunk_text:
            return

        bucket = "reasoning" if chunk_type == "reasoning" else "content"
        stream_state[bucket] += chunk_text

        now = time.time()
        if now - stream_state["last_update"] < 0.12:
            return
        stream_state["last_update"] = now

        blocks = []
        if stream_state["reasoning"].strip():
            blocks.append(
                f"{tr('Model reasoning stream')}\n"
                f"{stream_state['reasoning'][-900:]}"
            )
        if stream_state["content"].strip():
            blocks.append(
                f"{tr('Model output preview')}\n"
                f"{stream_state['content'][-900:]}"
            )

        preview = "\n\n".join(blocks)[-1800:]
        escaped_preview = html.escape(preview)
        stream_text.markdown(
            f"""
            <div style="height:150px; overflow:hidden; border:1px solid #e5e7eb;
                        border-radius:8px; padding:10px 12px; background:#f8fafc;
                        color:#334155;">
              <div style="font-size:12px; font-weight:600; color:#64748b; margin-bottom:6px;">
                {html.escape(tr('LLM stream window title'))}
              </div>
              <pre style="white-space:pre-wrap; margin:0; font-size:12px; line-height:1.45;
                          font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;">{escaped_preview}</pre>
            </div>
            """,
            unsafe_allow_html=True,
        )

    try:
        with st.spinner(tr("Generating script...")):
            selected_video_paths = _normalize_paths(
                video_paths
                or getattr(params, "video_origin_paths", [])
                or getattr(params, "video_origin_path", "")
            )
            if not selected_video_paths:
                st.error(tr("Please select video file first"))
                return
            """
            1. 获取字幕
            """
            update_progress(30, tr("Parsing subtitles..."))
            # 判断字幕文件是否存在
            subtitle_paths = _normalize_paths(subtitle_path)
            missing_subtitle_paths = [path for path in subtitle_paths if not os.path.exists(path)]
            if not subtitle_paths or missing_subtitle_paths:
                st.error(tr("Subtitle file does not exist"))
                return

            """
            2. 分析字幕总结剧情 - 使用新的LLM服务架构
            """
            text_provider = config.app.get('text_llm_provider', 'gemini').lower()
            text_api_key = config.app.get(f'text_{text_provider}_api_key')
            text_model = config.app.get(f'text_{text_provider}_model_name')
            text_base_url = config.app.get(f'text_{text_provider}_base_url')

            # 读取字幕文件内容（无论使用哪种实现都需要）
            subtitle_content = str(subtitle_content or "").strip() or _build_combined_subtitle_content(
                subtitle_paths,
                selected_video_paths,
            )
            if not subtitle_content:
                st.error(tr("Subtitle file is empty or unreadable"))
                return

            narration_copy = str(narration_copy or "").strip()
            if not narration_copy:
                st.error(tr("Please generate and review narration copy first"))
                return
            narration_units = []
            narration_copy_for_matching = narration_copy
            if prompt_category == FILM_TV_PROMPT_CATEGORY:
                narration_units = _split_narration_copy_into_units(narration_copy)
                narration_copy_for_matching = _format_narration_units_for_prompt(narration_units)
                if not narration_copy_for_matching:
                    st.error(tr("Generated narration copy is empty"))
                    return

            analyzer = SubtitleAnalyzerAdapter(
                text_api_key,
                text_model,
                text_base_url,
                text_provider,
                prompt_category=prompt_category,
            )

            def match_copy_to_script(
                current_subtitle_content: str,
                current_narration_copy: str,
                current_plot_analysis: str,
                log_context: str = "",
            ):
                try:
                    logger.info(f"使用新的LLM服务架构将审核文案匹配到字幕画面{log_context}")
                    update_waiting(tr("Matching narration copy to footage..."))
                    stream_text.info(tr("Waiting for model stream..."))
                    return analyzer.match_narration_copy_to_script(
                        short_name=video_theme,
                        plot_analysis=current_plot_analysis,
                        subtitle_content=current_subtitle_content,
                        narration_copy=current_narration_copy,
                        temperature=temperature,
                        narration_language=narration_language,
                        drama_genre=drama_genre,
                        original_sound_ratio=original_sound_ratio,
                        stream_callback=update_stream_window,
                    )
                except Exception as e:
                    logger.warning(f"使用新LLM服务匹配画面失败，回退到旧实现: {str(e)}")
                    stream_text.info(tr("Streaming unavailable fallback waiting..."))
                    return match_narration_copy_to_script_legacy(
                        short_name=video_theme,
                        plot_analysis=current_plot_analysis,
                        subtitle_content=current_subtitle_content,
                        narration_copy=current_narration_copy,
                        api_key=text_api_key,
                        model=text_model,
                        base_url=text_base_url,
                        temperature=temperature,
                        provider=text_provider,
                        narration_language=narration_language,
                        drama_genre=drama_genre,
                        original_sound_ratio=original_sound_ratio,
                        prompt_category=prompt_category,
                    )

            def parse_matched_result_items(
                narration_result,
                expected_units,
                context_label: str = "",
                stop_on_missing_units: bool = True,
            ):
                if narration_result["status"] != "success":
                    logger.info(f"\n剪辑脚本匹配失败{context_label}: {narration_result['message']}")
                    st.error(tr("Script generation failed check logs"))
                    st.stop()

                narration_script_text = narration_result["narration_script"]
                narration_dict = parse_and_fix_json(narration_script_text)
                if narration_dict is None:
                    st.error(tr("Generated narration JSON parse failed"))
                    logger.error(f"JSON解析失败{context_label}，原始内容: {narration_script_text}")
                    st.stop()

                if 'items' not in narration_dict:
                    st.error(tr("Generated narration missing items field"))
                    logger.error(f"JSON结构错误{context_label}，缺少items字段: {narration_dict}")
                    st.stop()

                items = _normalize_narration_items_video_sources(
                    narration_dict['items'],
                    selected_video_paths,
                )
                if expected_units:
                    items = _fill_missing_narration_unit_ids_from_text(items, expected_units)
                    missing_unit_ids = _validate_narration_unit_coverage(items, expected_units)
                    if missing_unit_ids:
                        logger.error(f"剪辑脚本缺少解说文案单元{context_label}: {missing_unit_ids}")
                        if not stop_on_missing_units:
                            return items
                        st.error(f"{tr('Generated narration validation failed')}: missing {', '.join(missing_unit_ids)}. 已缓存前面成功集数，修正后可继续重跑。")
                        st.stop()
                return items

            if plot_analysis and str(plot_analysis).strip():
                logger.info("使用用户编辑后的剧情理解结果匹配剪辑脚本")
                analysis_result = {
                    "status": "success",
                    "analysis": str(plot_analysis).strip(),
                }
            else:
                plot_analysis_input = subtitle_content
                if enable_web_search:
                    update_waiting(tr("Searching short drama with Tavily..."))
                    plot_analysis_input = _build_plot_analysis_input(
                        subtitle_content,
                        short_name=video_theme,
                        enable_web_search=True,
                        tr=tr,
                        search_keywords=search_keywords,
                        empty_title_message_key=empty_title_message_key,
                        web_search_context_description=web_search_context_description,
                    )
                    if plot_analysis_input is None:
                        return
                try:
                    # 优先使用新的LLM服务架构
                    logger.info("使用新的LLM服务架构进行字幕分析")
                    update_waiting(tr("Analyzing subtitles with model..."))
                    analysis_result = analyzer.analyze_subtitle(plot_analysis_input)

                except Exception as e:
                    logger.warning(f"使用新LLM服务失败，回退到旧实现: {str(e)}")
                    # 回退到旧的实现
                    update_waiting(tr("Analyzing subtitles with model..."))
                    analysis_result = analyze_subtitle(
                        subtitle_content=plot_analysis_input,
                        api_key=text_api_key,
                        model=text_model,
                        base_url=text_base_url,
                        save_result=True,
                        temperature=temperature,
                        provider=text_provider,
                        prompt_category=prompt_category,
                    )
            """
            3. 根据用户审核后的文案匹配画面与时间戳
            """
            if analysis_result["status"] == "success":
                logger.info("字幕分析成功！")
                update_waiting()

                matched_narration_items = None
                use_episode_matching = (
                    prompt_category == FILM_TV_PROMPT_CATEGORY
                    and narration_scope == NARRATION_SCOPE_EPISODE
                )
                if use_episode_matching:
                    episode_narration_sections = _split_episode_narration_copy(narration_copy)
                    episode_subtitle_sections = {
                        episode["episode_index"]: episode
                        for episode in _build_episode_subtitle_sections(subtitle_paths, selected_video_paths)
                    }
                    if episode_narration_sections and episode_subtitle_sections:
                        matched_narration_items = []
                        total_episode_sections = len(episode_narration_sections)
                        for section_index, section in enumerate(episode_narration_sections, start=1):
                            episode_index = section["episode_index"]
                            episode = episode_subtitle_sections.get(episode_index)
                            if not episode:
                                logger.error(f"缺少第 {episode_index} 集对应的字幕文件，无法逐集匹配剪辑脚本")
                                st.error(tr("Subtitle file does not exist"))
                                st.stop()

                            update_waiting(
                                f"{tr('Matching narration copy to footage...')} ({section_index}/{total_episode_sections})"
                            )
                            episode_units = _split_narration_copy_into_units(section["text"])
                            episode_narration_copy_for_matching = _format_narration_units_for_prompt(episode_units)
                            if not episode_narration_copy_for_matching:
                                st.error(tr("Generated narration copy is empty"))
                                st.stop()

                            episode_cache_key = _episode_match_cache_key(
                                video_theme=video_theme,
                                episode=episode,
                                narration_text=section["text"],
                                plot_analysis=analysis_result["analysis"],
                                narration_language=narration_language,
                                drama_genre=drama_genre,
                                original_sound_ratio=original_sound_ratio,
                                prompt_category=prompt_category,
                            )
                            cached_episode = _load_episode_match_cache(episode_cache_key)
                            if cached_episode:
                                logger.info(f"使用逐集剪辑脚本缓存（第 {episode_index} 集）")
                                episode_items = cached_episode["items"]
                                episode_video_name = episode.get("video_name") or ""
                                for episode_item in episode_items:
                                    episode_item["video_id"] = episode_index
                                    if episode_video_name:
                                        episode_item["video_name"] = episode_video_name
                                matched_narration_items.extend(episode_items)
                                continue

                            episode_subtitle_content = _build_episode_matching_subtitle_content(episode)
                            episode_result = match_copy_to_script(
                                episode_subtitle_content,
                                episode_narration_copy_for_matching,
                                analysis_result["analysis"],
                                f"（第 {episode_index} 集）",
                            )
                            episode_items = parse_matched_result_items(
                                episode_result,
                                episode_units,
                                f"（第 {episode_index} 集）",
                                stop_on_missing_units=False,
                            )
                            episode_items = _append_missing_narration_units_as_items(
                                episode_items,
                                episode_units,
                                episode,
                            )
                            missing_after_fallback = _validate_narration_unit_coverage(
                                episode_items,
                                episode_units,
                            )
                            if missing_after_fallback:
                                st.error(
                                    f"{tr('Generated narration validation failed')}: "
                                    f"missing {', '.join(missing_after_fallback)}. 已缓存前面成功集数，修正后可继续重跑。"
                                )
                                st.stop()
                            episode_video_name = episode.get("video_name") or ""
                            for episode_item in episode_items:
                                episode_item["video_id"] = episode_index
                                if episode_video_name:
                                    episode_item["video_name"] = episode_video_name
                            _save_episode_match_cache(episode_cache_key, {
                                "status": "success",
                                "episode_index": episode_index,
                                "items": episode_items,
                                "narration_units": episode_units,
                                "updated_at": time.time(),
                            })
                            matched_narration_items.extend(episode_items)

                        for item_index, item in enumerate(matched_narration_items, start=1):
                            item["_id"] = item_index
                            try:
                                if int(item.get("OST", 0) or 0) == 1:
                                    item["narration"] = f"播放原片+{item_index}"
                            except (TypeError, ValueError):
                                pass
                        logger.info(f"\n逐集剪辑脚本匹配成功，共 {len(matched_narration_items)} 个片段")

                if matched_narration_items is None:
                    narration_result = match_copy_to_script(
                        subtitle_content,
                        narration_copy_for_matching,
                        analysis_result["analysis"],
                    )
                    if narration_result["status"] == "success":
                        logger.info("\n剪辑脚本匹配成功！")
                        logger.info(narration_result["narration_script"])
                    else:
                        logger.info(f"\n剪辑脚本匹配失败: {narration_result['message']}")
                        st.error(tr("Script generation failed check logs"))
                        st.stop()
            else:
                logger.error(f"分析失败: {analysis_result['message']}")
                st.error(tr("Script generation failed check logs"))
                st.stop()

            """
            4. 生成文案
            """
            logger.info("开始准备生成解说文案")

            if matched_narration_items is not None:
                narration_items = _normalize_narration_items_video_sources(
                    matched_narration_items,
                    selected_video_paths,
                )
            else:
                narration_items = parse_matched_result_items(narration_result, narration_units)
            narration_items = _strip_planner_only_fields(narration_items)
            script = json.dumps(narration_items, ensure_ascii=False, indent=2)

            if script is None:
                st.error(tr("Script generation failed check logs"))
                st.stop()
            logger.success(f"剪辑脚本生成完成")
            if isinstance(script, list):
                st.session_state['video_clip_json'] = script
            elif isinstance(script, str):
                st.session_state['video_clip_json'] = json.loads(script)
            update_progress(90, tr("Preparing output..."))

        time.sleep(0.1)
        progress_bar.progress(100)
        status_text.text(tr("Script generation completed!"))
        st.success(tr("Video script generated successfully"))

    except Exception as err:
        st.error(f"{tr('Generation error')}: {str(err)}")
        logger.exception(f"生成脚本时发生错误\n{traceback.format_exc()}")
    finally:
        time.sleep(2)
        progress_bar.empty()
        status_text.empty()
        stream_text.empty()
