import json
import os
import re
import shutil
import subprocess
import uuid
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from loguru import logger

from app.config import config
from app.models.schema import VideoAspect, VideoClipParams
from app.services import generate_video
from app.utils import utils


COVER_FRAME_DURATION_SECONDS = 1 / 30
_AUTO_DRAFT_NAME_RE = re.compile(r"^NarratoAI(?:[_-].*)?$", re.IGNORECASE)
_RELEASE_TOKEN_RE = re.compile(
    r"\b(?:2160p|1440p|1080p|720p|480p|4k|uhd|web[-_. ]?dl|webrip|bluray|bdrip|"
    r"hdtv|aac\d*(?:[ .]\d+)?|h[ .]?264|h[ .]?265|x264|x265|hevc|mweb|tx|hdr|dv)\b",
    re.IGNORECASE,
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def is_cover_enabled(params: VideoClipParams) -> bool:
    fields_set = getattr(params, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(params, "__fields_set__", set())
    value = getattr(params, "cover_enabled", None)
    if "cover_enabled" not in fields_set:
        value = config.ui.get("cover_enabled", False)
    return _as_bool(value)


def _cover_api_url(params: VideoClipParams) -> str:
    return str(
        getattr(params, "cover_api_url", "")
        or config.ui.get("cover_api_url", "")
        or "http://127.0.0.1:8080"
    ).rstrip("/")


def _cover_style_hint(params: VideoClipParams) -> str:
    return str(
        getattr(params, "cover_style_hint", "")
        or config.ui.get("cover_style_hint", "")
        or "短视频爆款封面，强冲突，高点击率"
    ).strip()


def _clean_media_name(value: str) -> str:
    text = os.path.splitext(os.path.basename(str(value or "").strip()))[0]
    text = re.sub(r"[\._]+", " ", text)
    text = re.sub(r"[-]+", " ", text)
    text = _RELEASE_TOKEN_RE.sub(" ", text)
    text = re.sub(r"\bS\d{1,2}E\d{1,2}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -_")
    return text


def _is_auto_draft_name(value: str) -> bool:
    return bool(_AUTO_DRAFT_NAME_RE.fullmatch(str(value or "").strip()))


def _cover_name(params: VideoClipParams) -> str:
    candidates = [
        getattr(params, "cover_name", ""),
        config.ui.get("cover_name", ""),
        _clean_media_name(getattr(params, "video_origin_path", "") or ""),
    ]
    candidates.extend(_clean_media_name(path) for path in _as_list(getattr(params, "video_origin_paths", [])))
    candidates.append(getattr(params, "draft_name", ""))

    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and not _is_auto_draft_name(text):
            return text
    return ""


def _default_platforms(params: VideoClipParams) -> list[str]:
    return ["douyin"]


def _cover_platforms(params: VideoClipParams) -> list[str]:
    platforms = _as_list(getattr(params, "cover_platforms", None))
    if not platforms:
        platforms = _as_list(config.ui.get("cover_platforms", []))
    return platforms or _default_platforms(params)


def _response_json(response: requests.Response) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"封面 API 返回非 JSON 内容: {response.text[:300]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("封面 API 返回格式无效")
    return data


def _extract_renders(data: Dict[str, Any]) -> list[Dict[str, Any]]:
    candidates = [
        data.get("renders"),
        data.get("data", {}).get("renders") if isinstance(data.get("data"), dict) else None,
        data.get("result", {}).get("renders") if isinstance(data.get("result"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _resolve_download_url(api_url: str, render: Dict[str, Any]) -> str:
    for key in ("url", "download_url", "image_url"):
        value = str(render.get(key) or "").strip()
        if not value:
            continue
        if urlparse(value).scheme:
            return value
        return urljoin(f"{api_url}/", value.lstrip("/"))
    return ""


def _copy_or_download_render(api_url: str, render: Dict[str, Any], output_path: str) -> str:
    render_path = str(render.get("path") or "").strip()
    if render_path and os.path.exists(render_path):
        shutil.copyfile(render_path, output_path)
        return output_path

    download_url = _resolve_download_url(api_url, render)
    if not download_url:
        raise RuntimeError("封面 API 未返回可用的 path/url")

    response = requests.get(download_url, timeout=120)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(response.content)
    return output_path


def generate_cover(task_id: str, params: VideoClipParams) -> str:
    if not is_cover_enabled(params):
        return ""

    api_url = _cover_api_url(params)
    cover_name = _cover_name(params)
    if not cover_name:
        logger.warning("封面生成已启用，但未解析到有效影视名称；请在 [ui].cover_name 中填写作品名")
        return ""

    payload: Dict[str, Any] = {
        "name": cover_name,
        "media_type": "multi",
        "platforms": _cover_platforms(params),
        "poster_index": 0,
        "style_hint": _cover_style_hint(params),
        "image_format": "jpg",
        "use_llm": _as_bool(getattr(params, "cover_use_llm", None), True),
    }

    try:
        logger.info(f"开始通过 tmdb-image 生成封面: {api_url}/api/generate, name={payload['name']}")
        response = requests.post(f"{api_url}/api/generate", json=payload, timeout=300)
        if response.status_code >= 400:
            raise RuntimeError(f"封面 API {response.status_code}: {response.text[:500]}")
        response.raise_for_status()
        data = _response_json(response)
        renders = _extract_renders(data)
        if not renders:
            raise RuntimeError("封面 API 未返回 renders")

        cover_dir = os.path.join(utils.task_dir(task_id), "covers")
        os.makedirs(cover_dir, exist_ok=True)
        cover_path = os.path.join(cover_dir, f"cover_{uuid.uuid4().hex}.jpg")
        _copy_or_download_render(api_url, renders[0], cover_path)
        logger.success(f"封面生成完成: {cover_path}")
        return cover_path
    except Exception as exc:
        logger.warning(f"封面生成失败，跳过封面插入: {exc}")
        return ""


def _video_has_audio(probe: Dict[str, Any]) -> bool:
    if "has_audio" in probe:
        return bool(probe.get("has_audio"))
    return any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))


def _parse_fps(value: Any) -> float:
    text = str(value or "").strip()
    if "/" in text:
        num, den = text.split("/", 1)
        try:
            den_float = float(den)
            return float(num) / den_float if den_float else 30.0
        except ValueError:
            return 30.0
    try:
        return float(text) if text else 30.0
    except ValueError:
        return 30.0


def _probe_video_for_cover(video_path: str) -> tuple[int, int, float, bool]:
    ffmpeg_binary = generate_video._get_ffmpeg_binary()
    ffprobe_binary = generate_video._get_ffprobe_binary(ffmpeg_binary)
    cmd = [
        ffprobe_binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        video_path,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode == 0:
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams", [])
        video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if video_stream:
            width = int(video_stream.get("width") or 1080)
            height = int(video_stream.get("height") or 1920)
            fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
            fps = fps if fps > 0 else 30.0
            return width, height, fps, _video_has_audio(data)

    probe = generate_video._probe_video(video_path)
    return (
        int(probe.get("width") or 1080),
        int(probe.get("height") or 1920),
        30.0,
        _video_has_audio(probe),
    )


def prepend_cover_first_frame(
    video_path: str,
    cover_image_path: str,
    output_path: Optional[str] = None,
    threads: int = 4,
) -> str:
    if not cover_image_path or not os.path.exists(cover_image_path):
        return video_path
    if not video_path or not os.path.exists(video_path):
        return video_path

    try:
        width, height, fps, has_audio = _probe_video_for_cover(video_path)
        frame_duration = 1 / fps if fps > 0 else COVER_FRAME_DURATION_SECONDS
        ffmpeg_binary = generate_video._get_ffmpeg_binary()
        temp_output = output_path or os.path.join(
            os.path.dirname(video_path),
            f"{os.path.splitext(os.path.basename(video_path))[0]}_cover_tmp.mp4",
        )

        cover_filter = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={fps:.6f},format=yuv420p,setsar=1,"
            "setpts=PTS-STARTPTS[v0]"
        )
        video_filter = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps:.6f},"
            "format=yuv420p,setsar=1,setpts=PTS-STARTPTS[v1]"
        )

        cmd = [
            ffmpeg_binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-t",
            f"{frame_duration:.6f}",
            "-i",
            cover_image_path,
            "-i",
            video_path,
        ]

        if has_audio:
            cmd.extend([
                "-f",
                "lavfi",
                "-t",
                f"{frame_duration:.6f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ])
            filter_complex = ";".join([
                cover_filter,
                video_filter,
                "[2:a]atrim=0:{0:.6f},asetpts=PTS-STARTPTS[a0]".format(frame_duration),
                "[1:a]aformat=sample_rates=44100:channel_layouts=stereo,asetpts=PTS-STARTPTS[a1]",
                "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
            ])
            cmd.extend([
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ])
        else:
            filter_complex = ";".join([
                cover_filter,
                video_filter,
                "[v0][v1]concat=n=2:v=1:a=0[v]",
            ])
            cmd.extend(["-filter_complex", filter_complex, "-map", "[v]", "-an"])

        cmd.extend([
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-threads",
            str(threads),
            "-movflags",
            "+faststart",
            temp_output,
        ])

        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        if output_path:
            logger.success(f"已将封面插入视频首帧: {output_path}")
            return output_path

        os.replace(temp_output, video_path)
        logger.success(f"已将封面插入视频首帧: {video_path}")
        return video_path
    except subprocess.CalledProcessError as exc:
        logger.warning(f"封面首帧插入失败，保留原视频: {exc.stderr or exc}")
        return video_path
    except Exception as exc:
        logger.warning(f"封面首帧插入失败，保留原视频: {exc}")
        return video_path
