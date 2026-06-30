import os
import re
from os import path
from typing import Iterable, Sequence

from loguru import logger

from app.models.schema import VideoClipParams
from app.utils import utils


SCRIPT_SUBTITLE_LIST_KEYS = (
    "original_subtitle_paths",
    "subtitle_paths",
    "srt_paths",
)
SCRIPT_SUBTITLE_SINGLE_KEYS = (
    "original_subtitle_path",
    "subtitle_path",
    "srt_path",
)
EPISODE_TOKEN_RE = re.compile(r"\b[Ss](\d{1,2})[ ._-]*[Ee](\d{1,2})\b")


def _normalize_paths(paths_value) -> list[str]:
    if isinstance(paths_value, str):
        paths_value = [paths_value]
    if not paths_value:
        return []

    normalized_paths = []
    seen = set()
    for item in paths_value:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if item and item not in seen:
            normalized_paths.append(item)
            seen.add(item)
    return normalized_paths


def _append_unique(paths: list[str], seen: set[str], candidates: Iterable[str]) -> None:
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in seen:
            paths.append(candidate)
            seen.add(candidate)


def _resolve_subtitle_path(subtitle_path: str, script_path: str = "") -> str:
    subtitle_path = str(subtitle_path or "").strip()
    if not subtitle_path:
        return ""

    candidates = []
    if path.isabs(subtitle_path):
        candidates.append(subtitle_path)
    else:
        candidates.extend(
            [
                path.join(utils.subtitle_dir(), subtitle_path),
                path.join(utils.root_dir(), subtitle_path),
            ]
        )
        if script_path:
            candidates.append(path.join(path.dirname(script_path), subtitle_path))

    for candidate in candidates:
        if path.exists(candidate):
            return path.abspath(candidate)

    return subtitle_path


def _get_param_subtitle_paths(params: VideoClipParams) -> list[str]:
    subtitle_paths = _normalize_paths(getattr(params, "original_subtitle_paths", []))
    single_subtitle_path = str(getattr(params, "original_subtitle_path", "") or "").strip()
    if single_subtitle_path:
        subtitle_paths = [single_subtitle_path] + [
            item for item in subtitle_paths if item != single_subtitle_path
        ]
    return subtitle_paths


def _get_script_subtitle_paths(list_script: Sequence[dict], script_path: str = "") -> list[str]:
    resolved_paths = []
    seen = set()

    for item in list_script or []:
        if not isinstance(item, dict):
            continue

        for key in SCRIPT_SUBTITLE_LIST_KEYS:
            _append_unique(resolved_paths, seen, _normalize_paths(item.get(key)))

        for key in SCRIPT_SUBTITLE_SINGLE_KEYS:
            value = item.get(key)
            if isinstance(value, str):
                _append_unique(resolved_paths, seen, [value])

    return [_resolve_subtitle_path(item, script_path) for item in resolved_paths]


def _video_stem_candidates(video_path: str) -> list[str]:
    stem = path.splitext(path.basename(str(video_path or "").strip()))[0]
    if not stem:
        return []

    candidates = [stem]
    timestamp_stripped = re.sub(r"_[0-9]{14}$", "", stem)
    if timestamp_stripped and timestamp_stripped not in candidates:
        candidates.append(timestamp_stripped)

    for match in EPISODE_TOKEN_RE.finditer(stem):
        token = f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"
        if token not in candidates:
            candidates.append(token)

    return candidates


def find_original_subtitle_paths_for_videos(video_paths: list[str]) -> list[str]:
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
            lower_stem = subtitle_stem.lower()
            for candidate in candidates:
                lower_candidate = candidate.lower()
                if (
                    subtitle_stem == candidate
                    or subtitle_stem.startswith(f"{candidate}_")
                    or (
                        EPISODE_TOKEN_RE.fullmatch(candidate)
                        and lower_candidate in lower_stem
                    )
                ):
                    matches.append(subtitle_path)
                    break

        if not matches:
            continue

        matches.sort(key=lambda item: path.getmtime(item), reverse=True)
        selected_path = matches[0]
        if selected_path not in seen:
            resolved_paths.append(selected_path)
            seen.add(selected_path)

    return resolved_paths


def get_original_subtitle_paths(
    params: VideoClipParams,
    *,
    list_script: Sequence[dict] | None = None,
    video_paths: Sequence[str] | None = None,
    log_prefix: str = "",
) -> list[str]:
    script_path = str(getattr(params, "video_clip_json_path", "") or "").strip()
    resolved_paths = []
    seen = set()

    explicit_paths = [
        _resolve_subtitle_path(item, script_path)
        for item in _get_param_subtitle_paths(params)
    ]
    _append_unique(resolved_paths, seen, explicit_paths)
    if resolved_paths:
        return resolved_paths

    script_paths = _get_script_subtitle_paths(list_script or [], script_path)
    _append_unique(resolved_paths, seen, script_paths)
    if resolved_paths:
        logger.info(f"{log_prefix}从剪辑脚本读取原片字幕路径: {resolved_paths}")
        return resolved_paths

    fallback_paths = find_original_subtitle_paths_for_videos(list(video_paths or []))
    _append_unique(resolved_paths, seen, fallback_paths)
    if resolved_paths:
        logger.info(f"{log_prefix}未从参数或脚本获取原片字幕，已按视频文件名自动匹配: {resolved_paths}")

    return resolved_paths
