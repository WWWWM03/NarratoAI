import copy
import json
import os
import re
import subprocess
import time
from os import path
from typing import Dict
from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoClipParams
from app.services import voice, clip_video, script_subtitle, subtitle_resolver, cover_service
from app.services.jianying_draft_builder import write_plaintext_jianying_draft
from app.services import state as sm
from app.utils import utils


def get_media_duration_ffprobe(media_file: str) -> float:
    """
    使用ffprobe获取媒体文件的精确时长（秒）
    
    Args:
        media_file: 媒体文件路径
        
    Returns:
        float: 媒体时长（秒），精确到微秒
    """
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'csv=p=0',
            media_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        duration = float(result.stdout.strip())
        logger.debug(f"使用ffprobe获取媒体时长: {duration:.6f}秒, 文件: {media_file}")
        return duration
    except subprocess.CalledProcessError as e:
        logger.error(f"ffprobe执行失败: {e.stderr}")
        raise
    except Exception as e:
        logger.error(f"获取媒体时长失败: {str(e)}")
        raise


def get_audio_duration_ffprobe(audio_file: str) -> float:
    return get_media_duration_ffprobe(audio_file)


def _strip_tts_voice_prefix(voice_name: str, prefix: str) -> str:
    voice_name = voice_name or ""
    if voice_name.startswith(prefix):
        return voice_name[len(prefix):]
    return voice_name


def _strip_indextts_prefix(voice_name: str) -> str:
    return _strip_tts_voice_prefix(
        config.normalize_indextts_voice_prefix(voice_name or ""),
        config.INDEXTTS_VOICE_PREFIX,
    )


def _floor_duration_to_milliseconds(duration: float) -> float:
    return int(duration * 1000) / 1000.0


def _format_seconds_for_trange(seconds: float) -> str:
    return f"{seconds:.3f}s"


def _get_cached_media_duration(media_file: str, duration_cache: Dict[str, float]) -> float:
    if media_file not in duration_cache:
        duration_cache[media_file] = _floor_duration_to_milliseconds(
            get_media_duration_ffprobe(media_file)
        )
    return duration_cache[media_file]


def _load_script_file(video_script_path: str) -> list[Dict]:
    if not path.exists(video_script_path):
        logger.error(f"解说脚本文件不存在: {video_script_path}，请先点击【保存脚本】按钮保存脚本后再生成视频")
        raise ValueError("解说脚本文件不存在，请先保存脚本后再导出")

    try:
        with open(video_script_path, "r", encoding="utf-8") as f:
            list_script = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"脚本 JSON 格式错误: {video_script_path}, line={e.lineno}, col={e.colno}, error={e.msg}")
        raise ValueError(f"脚本 JSON 格式错误：第 {e.lineno} 行第 {e.colno} 列，{e.msg}") from e
    except Exception as e:
        logger.error(f"无法读取视频json脚本: {video_script_path}, {e}")
        raise ValueError("无法读取视频json脚本，请检查脚本格式是否正确") from e

    if not isinstance(list_script, list) or not list_script:
        raise ValueError("脚本必须是非空 JSON 数组")

    _validate_script_items(list_script)
    video_script = " ".join(str(i.get("narration", "")) for i in list_script)
    logger.debug(f"解说完整脚本: \n{video_script}")
    logger.debug(f"解说 OST 列表: \n{[i.get('OST') for i in list_script]}")
    logger.debug(f"解说时间戳列表: \n{[i.get('timestamp') for i in list_script]}")
    return list_script


def _validate_script_items(list_script: list[Dict]) -> None:
    required_fields = ["_id", "timestamp", "narration", "OST"]
    timestamp_pattern = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3}-\d{2}:\d{2}:\d{2},\d{3}$")

    seen_ids = set()
    for index, item in enumerate(list_script, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"脚本第 {index} 项必须是对象")

        missing_fields = [field for field in required_fields if field not in item]
        if missing_fields:
            raise ValueError(f"脚本第 {index} 项缺少字段: {', '.join(missing_fields)}")

        item_id = item.get("_id")
        if item_id in seen_ids:
            raise ValueError(f"脚本存在重复 _id: {item_id}")
        seen_ids.add(item_id)

        timestamp = item.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp_pattern.match(timestamp):
            raise ValueError(f"脚本第 {index} 项时间戳格式错误: {timestamp}")

        try:
            start_time, end_time = script_subtitle.parse_time_range(timestamp)
        except ValueError as e:
            raise ValueError(f"脚本第 {index} 项时间戳无法解析: {timestamp}") from e
        if end_time <= start_time:
            raise ValueError(f"脚本第 {index} 项结束时间必须大于开始时间: {timestamp}")

        ost = item.get("OST")
        if not isinstance(ost, int) or ost not in (0, 1, 2):
            raise ValueError(f"脚本第 {index} 项 OST 必须是 0、1 或 2: {ost}")

        narration = item.get("narration")
        if not isinstance(narration, str) or not narration.strip():
            raise ValueError(f"脚本第 {index} 项 narration 不能为空")


def _clamp_duration_to_media(
    requested_duration: float,
    media_file: str,
    duration_cache: Dict[str, float],
    media_label: str,
    source_start_time: float = 0.0,
) -> float:
    requested_duration = _floor_duration_to_milliseconds(max(requested_duration, 0.0))
    actual_duration = _get_cached_media_duration(media_file, duration_cache)
    available_duration = _floor_duration_to_milliseconds(
        max(actual_duration - max(source_start_time, 0.0), 0.0)
    )
    safe_duration = min(requested_duration, available_duration)

    logger.info(
        f"{media_label}实际时长: {actual_duration:.6f}秒, "
        f"可用时长: {available_duration:.6f}秒, 请求时长: {requested_duration:.3f}秒"
    )
    if safe_duration < requested_duration:
        logger.warning(
            f"{media_label}短于脚本时长，已将剪映片段时长从 "
            f"{requested_duration:.3f}秒 调整为 {safe_duration:.3f}秒"
        )

    return safe_duration


def _normalize_indextts_reference_audio(params: VideoClipParams) -> None:
    """Ensure local clone TTS engines use configured reference audio instead of a stale UI voice."""
    params.tts_engine = config.normalize_tts_engine_name(params.tts_engine)
    if params.tts_engine == config.INDEXTTS_ENGINE:
        tts_config = config.indextts
        voice_prefix = config.INDEXTTS_VOICE_PREFIX
        display_name = "IndexTTS-1.5"
    elif params.tts_engine == config.INDEXTTS2_ENGINE:
        tts_config = config.indextts2
        voice_prefix = config.INDEXTTS2_VOICE_PREFIX
        display_name = "IndexTTS-2"
    elif params.tts_engine == config.OMNIVOICE_ENGINE:
        tts_config = config.omnivoice
        if tts_config.get("mode", "auto") != "voice_clone":
            return
        voice_prefix = config.OMNIVOICE_VOICE_PREFIX
        display_name = "OmniVoice"
    elif params.tts_engine == config.VOXCPM2_ENGINE:
        tts_config = config.voxcpm2
        voice_prefix = config.VOXCPM2_VOICE_PREFIX
        display_name = "VoxCPM2"
    else:
        return

    candidate = _strip_tts_voice_prefix(getattr(params, "voice_name", "") or "", voice_prefix)
    if candidate and os.path.isfile(candidate):
        params.voice_name = f"{voice_prefix}{candidate}"
        logger.info(f"{display_name} 使用参考音频: {candidate}")
        return

    configured_ref = _strip_tts_voice_prefix(tts_config.get("reference_audio", "") or "", voice_prefix)
    if configured_ref and os.path.isfile(configured_ref):
        params.voice_name = f"{voice_prefix}{configured_ref}"
        logger.info(f"{display_name} 使用配置中的参考音频: {configured_ref}")
        return

    raise ValueError(f"{display_name} 参考音频不存在，请在音频设置中上传或选择有效的参考音频")


def _index_tts_results(tts_results: list[Dict]) -> Dict:
    indexed = {}
    for tts_result in tts_results or []:
        item_id = tts_result.get("_id")
        timestamp = tts_result.get("timestamp")
        if item_id is not None:
            indexed[item_id] = tts_result
        if timestamp:
            indexed[timestamp] = tts_result
    return indexed


def _get_video_source_paths(params: VideoClipParams) -> list[str]:
    return clip_video._normalize_video_origin_paths(
        getattr(params, "video_origin_path", ""),
        getattr(params, "video_origin_paths", []),
    )


def _resolve_script_video_path(item: Dict, video_source_paths: list[str]) -> str:
    if not video_source_paths:
        return ""
    return clip_video._resolve_script_video_path(item, video_source_paths)


def _resolve_tts_result(item: Dict, tts_map: Dict) -> Dict:
    item_id = item.get("_id")
    timestamp = item.get("timestamp")
    if item_id is not None and item_id in tts_map:
        return tts_map[item_id]
    if timestamp in tts_map:
        return tts_map[timestamp]
    return {}


def _safe_audio_timestamp(timestamp: str) -> str:
    return str(timestamp or "").replace(":", "_")


def _find_cached_audio_file(task_id: str, item: Dict, allow_legacy_timestamp_cache: bool = True) -> str:
    timestamp = _safe_audio_timestamp(item.get("timestamp", ""))
    if not timestamp:
        return ""

    task_dir = utils.task_dir(task_id)
    cache_stems = [voice.get_tts_cache_stem(item)]
    if allow_legacy_timestamp_cache:
        legacy_stem = voice.get_legacy_tts_cache_stem(item)
        if legacy_stem not in cache_stems:
            cache_stems.append(legacy_stem)

    for extension in (".wav", ".mp3"):
        for cache_stem in cache_stems:
            audio_file = path.join(task_dir, f"{cache_stem}{extension}")
            if path.exists(audio_file) and path.getsize(audio_file) > 0:
                return audio_file
    return ""


def _build_cached_tts_results(task_id: str, list_script: list[Dict]) -> tuple[list[Dict], list[Dict]]:
    cached_results = []
    missing_segments = []
    duration_cache: Dict[str, float] = {}
    timestamp_counts: Dict[str, int] = {}
    for item in list_script:
        if item.get("OST") in (0, 2):
            timestamp = item.get("timestamp", "")
            timestamp_counts[timestamp] = timestamp_counts.get(timestamp, 0) + 1

    for item in list_script:
        if item.get("OST") not in (0, 2):
            continue

        allow_legacy_timestamp_cache = timestamp_counts.get(item.get("timestamp", ""), 0) <= 1
        audio_file = _find_cached_audio_file(
            task_id,
            item,
            allow_legacy_timestamp_cache=allow_legacy_timestamp_cache,
        )
        if not audio_file:
            missing_segments.append(item)
            continue

        try:
            duration = _get_cached_media_duration(audio_file, duration_cache)
        except Exception as e:
            logger.warning(f"Cached audio duration failed, regenerate _id={item.get('_id')}: {audio_file}, {e}")
            missing_segments.append(item)
            continue

        cached_results.append({
            "_id": item.get("_id"),
            "timestamp": item.get("timestamp"),
            "audio_file": audio_file,
            "subtitle_file": "",
            "duration": duration,
            "text": item.get("narration", ""),
            "from_cache": True,
        })

    return cached_results, missing_segments


def _generate_or_reuse_tts_results(
    task_id: str,
    list_script: list[Dict],
    params: VideoClipParams,
    reuse_tts_cache: bool = False,
) -> list[Dict]:
    tts_segments = [
        segment for segment in list_script
        if segment["OST"] in [0, 2]
    ]
    logger.debug(f"TTS segments: {len(tts_segments)}")

    if not reuse_tts_cache:
        return voice.tts_multiple(
            task_id=task_id,
            list_script=tts_segments,
            tts_engine=params.tts_engine,
            voice_name=params.voice_name,
            voice_rate=params.voice_rate,
            voice_pitch=params.voice_pitch,
        )

    cached_results, missing_segments = _build_cached_tts_results(task_id, list_script)
    logger.info(f"Reuse TTS cache: hit={len(cached_results)}, missing={len(missing_segments)}")

    generated_results = []
    if missing_segments:
        logger.info(
            "Regenerate missing TTS ids: "
            + ", ".join(str(item.get("_id")) for item in missing_segments[:20])
            + (" ..." if len(missing_segments) > 20 else "")
        )
        generated_results = voice.tts_multiple(
            task_id=task_id,
            list_script=missing_segments,
            tts_engine=params.tts_engine,
            voice_name=params.voice_name,
            voice_rate=params.voice_rate,
            voice_pitch=params.voice_pitch,
        )

    return cached_results + generated_results


def _build_jianying_draft_script(
    list_script: list[Dict],
    params: VideoClipParams,
    tts_results: list[Dict],
) -> list[Dict]:
    video_source_paths = _get_video_source_paths(params)
    if not video_source_paths:
        raise ValueError("视频文件不能为空")

    tts_map = _index_tts_results(tts_results)
    draft_script = []
    accumulated_duration = 0.0

    for item in list_script:
        item_copy = dict(item)
        timestamp = item_copy.get("timestamp", "")
        try:
            source_start, source_end = script_subtitle.parse_time_range(timestamp)
        except ValueError as e:
            logger.warning(f"解析剪映片段时间戳失败，跳过片段 {item_copy.get('_id')}: {e}")
            continue

        timestamp_duration = _floor_duration_to_milliseconds(source_end - source_start)
        if timestamp_duration <= 0:
            logger.warning(f"剪映片段时长无效，跳过片段 {item_copy.get('_id')}: {timestamp}")
            continue

        ost = int(item_copy.get("OST", 0) or 0)
        tts_result = _resolve_tts_result(item_copy, tts_map) if ost in [0, 2] else {}
        item_duration = timestamp_duration
        if tts_result.get("duration"):
            item_duration = _floor_duration_to_milliseconds(float(tts_result.get("duration") or 0.0))
        if item_duration <= 0:
            item_duration = timestamp_duration

        item_copy.update({
            "video": _resolve_script_video_path(item_copy, video_source_paths),
            "audio": tts_result.get("audio_file", ""),
            "subtitle": tts_result.get("subtitle_file", ""),
            "sourceTimeRange": timestamp,
            "start_time": source_start,
            "source_start_time": source_start,
            "duration": item_duration,
            "use_source_timerange": True,
            "editedTimeRange": (
                f"{script_subtitle.format_srt_time(accumulated_duration)}-"
                f"{script_subtitle.format_srt_time(accumulated_duration + item_duration)}"
            ),
        })
        accumulated_duration += item_duration
        draft_script.append(item_copy)

    if not draft_script:
        raise ValueError("没有可写入剪映草稿的视频片段")

    return draft_script


def _validate_draft_script_assets(draft_script: list[Dict]) -> None:
    missing_video_items = []
    missing_audio_items = []

    for item in draft_script:
        item_id = item.get("_id")
        video_file = item.get("video", "")
        if not video_file or not path.exists(video_file):
            missing_video_items.append(f"{item_id}:{item.get('timestamp', '')}")

        if item.get("OST") in (0, 2):
            audio_file = item.get("audio", "")
            if not audio_file or not path.exists(audio_file):
                missing_audio_items.append(f"{item_id}:{item.get('timestamp', '')}")

    if missing_video_items:
        raise ValueError(
            "Missing video assets: "
            + ", ".join(missing_video_items[:30])
            + (" ..." if len(missing_video_items) > 30 else "")
        )

    if missing_audio_items:
        raise ValueError(
            "Missing narration audio: "
            + ", ".join(missing_audio_items[:30])
            + (" ..." if len(missing_audio_items) > 30 else "")
        )


def _get_original_subtitle_paths(
    params: VideoClipParams,
    list_script: list[Dict] | None = None,
) -> list[str]:
    return subtitle_resolver.get_original_subtitle_paths(
        params,
        list_script=list_script,
        video_paths=_get_video_source_paths(params),
        log_prefix="剪映导出",
    )


def _video_stem_candidates(video_path: str) -> list[str]:
    stem = path.splitext(path.basename(str(video_path or "").strip()))[0]
    if not stem:
        return []

    candidates = [stem]
    timestamp_stripped = re.sub(r"_[0-9]{14}$", "", stem)
    if timestamp_stripped and timestamp_stripped not in candidates:
        candidates.append(timestamp_stripped)
    return candidates


def _find_original_subtitle_paths_for_videos(video_paths: list[str]) -> list[str]:
    return subtitle_resolver.find_original_subtitle_paths_for_videos(video_paths)

    subtitle_dir = utils.subtitle_dir()
    if not path.isdir(subtitle_dir):
        return []

    subtitle_files = [
        path.join(subtitle_dir, filename)
        for filename in os.listdir(subtitle_dir)
        if filename.lower().endswith(".srt")
    ]
    if not subtitle_files:
        return []

    resolved_paths = []
    seen = set()
    for video_path in video_paths:
        candidates = _video_stem_candidates(video_path)
        if not candidates:
            continue

        matches = []
        for subtitle_path in subtitle_files:
            subtitle_stem = path.splitext(path.basename(subtitle_path))[0]
            for candidate in candidates:
                if subtitle_stem == candidate or subtitle_stem.startswith(f"{candidate}_"):
                    matches.append(subtitle_path)
                    break

        if not matches:
            continue

        matches.sort(key=lambda item: path.getmtime(item), reverse=True)
        selected_path = matches[0]
        if selected_path not in seen:
            resolved_paths.append(selected_path)
            seen.add(selected_path)

    if resolved_paths:
        logger.info(f"剪映导出未从参数获取原片字幕，已按视频文件名自动匹配: {resolved_paths}")
    return resolved_paths


def _create_jianying_subtitle_file(
    task_id: str,
    draft_script: list[Dict],
    params: VideoClipParams,
) -> str:
    if not getattr(params, "subtitle_enabled", True):
        return ""

    try:
        return script_subtitle.create_script_subtitle_file(
            task_id=task_id,
            list_script=draft_script,
            original_subtitle_paths=_get_original_subtitle_paths(params, draft_script),
            video_origin_paths=_get_video_source_paths(params),
        )
    except Exception as e:
        logger.warning(f"剪映草稿字幕生成失败，将导出无字幕草稿: {e}")
        return ""


def start_export_jianying_draft(task_id: str, params: VideoClipParams):
    """
    导出到剪映草稿的后台任务
    
    Args:
        task_id: 任务ID
        params: 视频参数
    """
    logger.info(f"\n\n## 开始导出到剪映草稿任务: {task_id}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=0)

    """
    1. 加载剪辑脚本
    """
    logger.info("\n\n## 1. 加载视频脚本")
    video_script_path = path.join(params.video_clip_json_path)
    
    if path.exists(video_script_path):
        try:
            with open(video_script_path, "r", encoding="utf-8") as f:
                list_script = json.load(f)
                video_list = [i['narration'] for i in list_script]
                video_ost = [i['OST'] for i in list_script]
                time_list = [i['timestamp'] for i in list_script]

                video_script = " ".join(video_list)
                logger.debug(f"解说完整脚本: \n{video_script}")
                logger.debug(f"解说 OST 列表: \n{video_ost}")
                logger.debug(f"解说时间戳列表: \n{time_list}")
        except Exception as e:
            logger.error(f"无法读取视频json脚本，请检查脚本格式是否正确")
            raise ValueError("无法读取视频json脚本，请检查脚本格式是否正确")
    else:
        logger.error(f"解说脚本文件不存在: {video_script_path}，请先点击【保存脚本】按钮保存脚本后再生成视频")
        raise ValueError("解说脚本文件不存在！请先点击【保存脚本】按钮保存脚本后再生成视频。")

    """
    2. 使用 TTS 生成音频素材
    """
    logger.info("\n\n## 2. 根据OST设置生成音频列表")
    _normalize_indextts_reference_audio(params)
    tts_segments = [
        segment for segment in list_script 
        if segment['OST'] in [0, 2]
    ]
    logger.debug(f"需要生成TTS的片段数: {len(tts_segments)}")

    tts_results = voice.tts_multiple(
        task_id=task_id,
        list_script=tts_segments,  # 只传入需要TTS的片段
        tts_engine=params.tts_engine,
        voice_name=params.voice_name,
        voice_rate=params.voice_rate,
        voice_pitch=params.voice_pitch,
    )

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    """
    3. 准备剪映草稿时间线 - 直接引用原视频素材和源时间戳
    """
    logger.info("\n\n## 3. 准备剪映草稿时间线（不裁剪视频）")
    new_script_list = _build_jianying_draft_script(list_script, params, tts_results)
    subtitle_path = _create_jianying_subtitle_file(task_id, new_script_list, params)

    logger.info(f"剪映草稿时间线准备完成，处理了 {len(new_script_list)} 个视频片段")
    if subtitle_path:
        logger.info(f"剪映草稿字幕文件: {subtitle_path}")

    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=60)

    """
    4. 导出到剪映草稿
    """
    logger.info("\n\n## 4. 导出到剪映草稿")
    
    try:
        jianying_draft_path = config.ui.get("jianying_draft_path", "")
        if not jianying_draft_path:
            raise ValueError("剪映草稿路径未配置")
        
        # 使用从参数中获取的草稿名称，如果为空则使用默认名称
        draft_name = getattr(params, 'draft_name', "")
        logger.debug(f"从params获取的草稿名称: '{draft_name}' (类型: {type(draft_name)})")
        if not draft_name:
            draft_name = f"NarratoAI_{int(time.time())}"
            logger.debug(f"使用默认草稿名称: '{draft_name}'")

        output_dir = utils.task_dir(task_id)
        cover_path = cover_service.generate_cover(task_id, params)

        draft_path, draft_name = write_plaintext_jianying_draft(
            jianying_draft_path=jianying_draft_path,
            draft_name=draft_name,
            new_script_list=new_script_list,
            params=params,
            output_dir=output_dir,
            subtitle_path=subtitle_path,
            cover_image_path=cover_path,
        )
        
        logger.success(f"成功导出到剪映草稿: {draft_name}")
        logger.info(f"草稿已保存到: {draft_path}")
        
        # 更新任务状态
        task_kwargs = {"draft_path": draft_path, "draft_name": draft_name}
        if subtitle_path:
            task_kwargs["subtitles"] = [subtitle_path]
        if cover_path:
            task_kwargs["covers"] = [cover_path]
        sm.state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100, **task_kwargs)
        
        return task_kwargs
    except Exception as e:
        logger.error(f"导出到剪映草稿失败: {e}")
        import traceback
        logger.error(f"错误详情: {traceback.format_exc()}")
        raise Exception(f"导出到剪映草稿失败: {e}")
