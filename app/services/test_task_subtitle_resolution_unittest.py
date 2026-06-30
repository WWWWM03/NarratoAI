import tempfile
import time
import unittest
import os
from pathlib import Path

from app.models.schema import VideoClipParams
from app.services import task


class TaskSubtitleResolutionTests(unittest.TestCase):
    def test_get_original_subtitle_paths_falls_back_to_matching_video_name(self):
        original_subtitle_dir = task.utils.subtitle_dir

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            older = temp_path / "01_1080p_fun_asr.srt"
            newer = temp_path / "01_1080p_fun_asr_20260608010240.srt"
            unrelated = temp_path / "other_fun_asr.srt"
            older.write_text("older", encoding="utf-8")
            unrelated.write_text("other", encoding="utf-8")
            time.sleep(0.01)
            newer.write_text("newer", encoding="utf-8")

            task.utils.subtitle_dir = lambda: str(temp_path)
            params = VideoClipParams(
                video_origin_path="/tmp/01_1080p_20260608113314.mp4",
            )

            try:
                subtitle_paths = task._get_original_subtitle_paths(params)
            finally:
                task.utils.subtitle_dir = original_subtitle_dir

        self.assertEqual([str(newer)], subtitle_paths)

    def test_get_original_subtitle_paths_keeps_explicit_params(self):
        params = VideoClipParams(
            video_origin_path="/tmp/01_1080p_20260608113314.mp4",
            original_subtitle_paths=["/tmp/provided.srt"],
        )

        self.assertEqual(["/tmp/provided.srt"], task._get_original_subtitle_paths(params))

    def test_get_original_subtitle_paths_reads_script_metadata(self):
        original_subtitle_dir = task.utils.subtitle_dir

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            subtitle_path = temp_path / "episode.srt"
            subtitle_path.write_text("subtitle", encoding="utf-8")
            task.utils.subtitle_dir = lambda: str(temp_path)

            params = VideoClipParams(video_clip_json_path=str(temp_path / "script.json"))
            list_script = [
                {
                    "_id": 1,
                    "timestamp": "00:00:01,000-00:00:02,000",
                    "narration": "narration",
                    "OST": 0,
                    "original_subtitle_paths": ["episode.srt"],
                }
            ]

            try:
                subtitle_paths = task._get_original_subtitle_paths(params, list_script)
                self.assertEqual(1, len(subtitle_paths))
                self.assertTrue(os.path.samefile(subtitle_path, subtitle_paths[0]))
            finally:
                task.utils.subtitle_dir = original_subtitle_dir

    def test_get_original_subtitle_paths_matches_episode_token(self):
        original_subtitle_dir = task.utils.subtitle_dir

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            subtitle_path = temp_path / "Taxi.Driver.S01E01.1080p.srt"
            subtitle_path.write_text("subtitle", encoding="utf-8")
            task.utils.subtitle_dir = lambda: str(temp_path)

            params = VideoClipParams(
                video_origin_path="/tmp/模范出租车 - S01E01 - 第 1 集.mkv",
            )

            try:
                subtitle_paths = task._get_original_subtitle_paths(params)
            finally:
                task.utils.subtitle_dir = original_subtitle_dir

        self.assertEqual([str(subtitle_path)], subtitle_paths)


if __name__ == "__main__":
    unittest.main()
