import time
from os import path
from typing import Dict

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoClipParams
from app.services import cover_service
from app.services import state as sm
from app.services.jianying_draft_builder import write_plaintext_jianying_draft
from app.services.jianying_task import (
    _build_jianying_draft_script,
    _create_jianying_subtitle_file,
    _generate_or_reuse_tts_results,
    _load_script_file,
    _normalize_indextts_reference_audio,
    _validate_draft_script_assets,
)
from app.utils import utils


def _write_jianying_draft_package(
    task_id: str,
    params: VideoClipParams,
    new_script_list: list[Dict],
    subtitle_path: str,
):
    jianying_draft_path = config.ui.get("jianying_draft_path", "")
    if not jianying_draft_path:
        raise ValueError("Jianying draft path is not configured")

    draft_name = getattr(params, "draft_name", "") or f"NarratoAI_{int(time.time())}"
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

    task_kwargs = {"draft_path": draft_path, "draft_name": draft_name}
    if subtitle_path:
        task_kwargs["subtitles"] = [subtitle_path]
    if cover_path:
        task_kwargs["covers"] = [cover_path]
    sm.state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100, **task_kwargs)
    return task_kwargs


def rebuild_jianying_draft_from_cache(task_id: str, params: VideoClipParams):
    """Rebuild a Jianying draft from an existing task directory, reusing cached TTS audio."""
    logger.info(f"Rebuild Jianying draft from cache task: {task_id}")
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=0)

    list_script = _load_script_file(path.join(params.video_clip_json_path))
    _normalize_indextts_reference_audio(params)
    tts_results = _generate_or_reuse_tts_results(
        task_id=task_id,
        list_script=list_script,
        params=params,
        reuse_tts_cache=True,
    )
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=20)

    new_script_list = _build_jianying_draft_script(list_script, params, tts_results)
    _validate_draft_script_assets(new_script_list)
    subtitle_path = _create_jianying_subtitle_file(task_id, new_script_list, params)
    sm.state.update_task(task_id, state=const.TASK_STATE_PROCESSING, progress=60)

    return _write_jianying_draft_package(task_id, params, new_script_list, subtitle_path)
