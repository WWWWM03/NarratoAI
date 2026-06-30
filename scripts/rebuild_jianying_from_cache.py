import argparse
import os
from pathlib import Path

from app.config import config
from app.models.schema import VideoClipParams
from app.services.jianying_cache_rebuild import rebuild_jianying_draft_from_cache


def _collect_video_paths(videos_dir: str) -> list[str]:
    video_extensions = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".webm"}
    paths = [
        str(item)
        for item in Path(videos_dir).iterdir()
        if item.is_file() and item.suffix.lower() in video_extensions
    ]
    return sorted(paths, key=lambda value: value.lower())


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild Jianying draft from an existing NarratoAI task cache.")
    parser.add_argument("--task-id", required=True, help="Existing task id whose storage/tasks directory contains cached audio.")
    parser.add_argument("--script", required=True, help="Script JSON path.")
    parser.add_argument("--videos-dir", default="", help="Directory containing source episode videos.")
    parser.add_argument("--video", action="append", default=[], help="Source video path. Can be passed multiple times.")
    parser.add_argument("--draft-name", default="", help="New Jianying draft name.")
    parser.add_argument("--no-subtitle", action="store_true", help="Disable generated subtitle track.")
    parser.add_argument("--no-cover", action="store_true", help="Disable cover generation while rebuilding.")
    args = parser.parse_args()

    video_paths = list(args.video or [])
    if args.videos_dir:
        video_paths.extend(_collect_video_paths(args.videos_dir))
    video_paths = [os.path.abspath(item) for item in video_paths if item]
    if not video_paths:
        raise SystemExit("No source videos found. Use --videos-dir or --video.")

    params = VideoClipParams(
        video_clip_json_path=os.path.abspath(args.script),
        video_origin_path=video_paths[0],
        video_origin_paths=video_paths,
        tts_engine=config.app.get("tts_engine", ""),
        voice_name=config.app.get("voice_name", ""),
        voice_rate=float(config.app.get("voice_rate", 1.0) or 1.0),
        voice_pitch=float(config.app.get("voice_pitch", 1.0) or 1.0),
        subtitle_enabled=not args.no_subtitle,
        draft_name=args.draft_name,
        cover_enabled=not args.no_cover and bool(config.app.get("cover_enabled", False)),
        cover_api_url=config.app.get("cover_api_url", ""),
        cover_name=config.app.get("cover_name", ""),
        cover_platforms=config.app.get("cover_platforms", None),
        cover_style_hint=config.app.get("cover_style_hint", ""),
        cover_use_llm=bool(config.app.get("cover_use_llm", True)),
    )

    result = rebuild_jianying_draft_from_cache(args.task_id, params)
    print(result)


if __name__ == "__main__":
    main()
