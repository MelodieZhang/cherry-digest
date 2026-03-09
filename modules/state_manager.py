"""
Persistent state management for tracking processed video IDs.
Uses atomic file writes to prevent corruption on unexpected exits.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta


class StateManager:
    def __init__(self, state_path: str, keep_days: int = 30):
        self.state_path = state_path
        self.keep_days = keep_days
        self.data = self._load()
        self._prune_old_entries()

    def _load(self) -> dict:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return self._empty_state()
        return self._empty_state()

    def _empty_state(self) -> dict:
        return {
            "schema_version": 1,
            "processed_videos": {},
            "runs": [],
        }

    def _prune_old_entries(self):
        cutoff = (datetime.now() - timedelta(days=self.keep_days)).strftime("%Y-%m-%d")
        self.data["processed_videos"] = {
            vid_id: info
            for vid_id, info in self.data["processed_videos"].items()
            if info.get("processed_date", "9999-99-99") >= cutoff
        }
        self.data["runs"] = [
            r for r in self.data["runs"]
            if r.get("date", "9999-99-99") >= cutoff
        ]

    def is_processed(self, video_id: str) -> bool:
        return video_id in self.data["processed_videos"]

    def mark_processed(self, video_ids: list[str], titles: dict[str, str] | None = None):
        today = datetime.now().strftime("%Y-%m-%d")
        for vid_id in video_ids:
            self.data["processed_videos"][vid_id] = {
                "processed_date": today,
                "title": (titles or {}).get(vid_id, ""),
            }
        self._save()

    def record_run(self, run_info: dict):
        self.data["runs"].append(run_info)
        self._save()

    def already_ran_today(self) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        return any(r.get("date") == today and r.get("status") == "success"
                   for r in self.data["runs"])

    def _save(self):
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp_path, self.state_path)
