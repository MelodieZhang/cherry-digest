#!/usr/bin/env python3
"""
Cherry — YouTube Morning Digest

Entry point. Runs the full pipeline:
  1. Fetch new videos + transcripts
  2. Summarize each video (Gemini)
  3. Send bilingual email digest (English + Chinese)
  4. Mark videos processed
"""

import logging
import logging.handlers
import os
import sys
from datetime import datetime

import yaml


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def _setup_logging(config: dict):
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = os.path.expanduser(log_cfg.get("log_file", "logs/digest.log"))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(
        logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=log_cfg.get("max_bytes", 5_242_880),
            backupCount=log_cfg.get("backup_count", 3),
        )
    )

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        handlers=handlers,
    )


def _resolve_api_key(env_var: str, config_value: str) -> str:
    """Return env var value if set, else fall back to config, else raise."""
    value = os.environ.get(env_var) or config_value
    if not value:
        raise EnvironmentError(
            f"API key not found. Set the {env_var} environment variable "
            f"or add it to config.yaml under api_keys."
        )
    return value


def _expand_output_dirs(config: dict) -> dict[str, str]:
    base = os.path.expanduser(config["output"]["base_dir"])
    return {
        "base": base,
        "transcripts": os.path.join(base, "transcripts"),
        "summaries": os.path.join(base, "summaries"),
    }


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(project_root: str):
    config_path = os.path.join(project_root, "config.yaml")
    state_path = os.path.join(project_root, "state.json")

    config = _load_config(config_path)
    _setup_logging(config)
    logger = logging.getLogger("cherry")

    logger.info("=" * 60)
    logger.info("Cherry YouTube Digest — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 60)

    # Resolve API keys
    try:
        gemini_key = _resolve_api_key("GEMINI_API_KEY", config["api_keys"].get("gemini", ""))
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    # Set up output directories
    dirs = _expand_output_dirs(config)
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)

    # Load state
    from modules.state_manager import StateManager
    state = StateManager(state_path, keep_days=config["output"]["keep_days"])

    # Guard: skip if already ran successfully today
    if state.already_ran_today():
        logger.info("Digest already generated today. Exiting.")
        sys.exit(0)

    run_start = datetime.now()
    run_record: dict = {
        "run_id": run_start.isoformat(),
        "date": run_start.strftime("%Y-%m-%d"),
        "status": "failed",
    }

    try:
        # ── Step 1: Fetch new videos ──────────────────────────────────────
        from modules.fetcher import fetch_new_videos
        videos = fetch_new_videos(config, state, dirs["transcripts"])

        if not videos:
            logger.info("No new videos found. Nothing to digest today.")
            run_record["status"] = "success"
            run_record["videos_processed"] = 0
            state.record_run(run_record)
            sys.exit(0)

        # ── Step 2: Summarize ─────────────────────────────────────────────
        from modules.summarizer import summarize_all
        summaries = summarize_all(videos, config, gemini_key, dirs["summaries"])

        if not summaries:
            logger.error("All video summaries failed. Aborting run.")
            sys.exit(1)

        # ── Step 3: Send bilingual email digest ───────────────────────────
        from modules.emailer import send_digest_email
        send_digest_email(summaries, config, gemini_key)

        # ── Step 4: Mark videos as processed ─────────────────────────────
        processed_ids = [v.video_id for v in videos if
                         any(s.get("video_id") == v.video_id for s in summaries)]
        titles = {v.video_id: v.title for v in videos}
        state.mark_processed(processed_ids, titles)

        run_record.update({
            "status": "success",
            "videos_found": len(videos),
            "videos_processed": len(summaries),
        })
        state.record_run(run_record)

        logger.info("Done! Digest: %d videos emailed.", len(summaries))

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)
    except Exception as e:
        logger.exception("Unhandled error during digest run: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    project_root = os.path.dirname(os.path.abspath(__file__))
    run(project_root)
