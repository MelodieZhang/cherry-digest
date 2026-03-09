"""
Fetches new YouTube videos from configured channels and retrieves their transcripts.

Uses scrapetube (no API key needed) for video metadata and
youtube-transcript-api (no API key needed) for captions.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import scrapetube
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

_yta = YouTubeTranscriptApi()  # v1.x uses an instance, not classmethods

logger = logging.getLogger(__name__)


@dataclass
class VideoData:
    video_id: str
    title: str
    channel_name: str
    url: str
    duration_seconds: int
    transcript_text: str


def _parse_duration(duration_str: str) -> int:
    """Convert 'H:MM:SS' or 'M:SS' string to total seconds."""
    if not duration_str:
        return 0
    parts = duration_str.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return 0


def _parse_relative_time(text: str) -> datetime | None:
    """
    Convert YouTube relative time strings to datetime.
    Examples: "3 hours ago", "1 day ago", "2 weeks ago", "5 minutes ago"
    Returns UTC datetime or None if unparseable.
    """
    if not text:
        return None
    text = text.lower().strip()
    now = datetime.now(timezone.utc)

    patterns = [
        (r"(\d+)\s+second", timedelta(seconds=1)),
        (r"(\d+)\s+minute", timedelta(minutes=1)),
        (r"(\d+)\s+hour",   timedelta(hours=1)),
        (r"(\d+)\s+day",    timedelta(days=1)),
        (r"(\d+)\s+week",   timedelta(weeks=1)),
        (r"(\d+)\s+month",  timedelta(days=30)),
        (r"(\d+)\s+year",   timedelta(days=365)),
    ]
    for pattern, unit in patterns:
        m = re.search(pattern, text)
        if m:
            return now - int(m.group(1)) * unit

    # Fallback: "Streamed X ago" prefix
    text = re.sub(r"^streamed\s+", "", text)
    for pattern, unit in patterns:
        m = re.search(pattern, text)
        if m:
            return now - int(m.group(1)) * unit

    return None


def _clean_snippets(snippets) -> str:
    """Join transcript snippets, removing noise tokens like [Music]."""
    noise = re.compile(r"^\[.*?\]$")
    parts = []
    for s in snippets:
        text = s.text.strip()
        if text and not noise.match(text):
            parts.append(text)
    return " ".join(parts)


def _get_transcript(video_id: str) -> str | None:
    """
    Fetch an English transcript via youtube-transcript-api v1.x.
    Falls back to translating the first available language if no English exists.
    Returns cleaned text or None if unavailable.
    """
    # Fast path: try fetching English directly
    try:
        fetched = _yta.fetch(video_id, languages=["en", "en-US", "en-GB"])
        return _clean_snippets(fetched.snippets)
    except (TranscriptsDisabled, VideoUnavailable, NoTranscriptFound) as e:
        logger.debug("No direct English transcript for %s: %s", video_id, e)
    except Exception as e:
        msg = str(e)
        if "blocking" in msg or "IP" in msg or "RequestBlocked" in msg or "IpBlocked" in msg:
            logger.warning("YouTube rate limit hit for %s — skipping (will retry tomorrow)", video_id)
            return None
        logger.debug("Direct fetch failed for %s: %s", video_id, e)

    # Fall back: list all transcripts and translate the first available one
    try:
        transcript_list = _yta.list(video_id)
        available = list(transcript_list)
        if not available:
            return None
        # If any transcript is already English, fetch it directly
        english = [t for t in available if t.language_code.startswith("en")]
        if english:
            fetched = english[0].fetch()
            return _clean_snippets(fetched.snippets)
        # Otherwise translate the first available transcript
        transcript = (
            transcript_list.find_manually_created_transcript(
                [t.language_code for t in available]
            )
            if any(not t.is_generated for t in available)
            else available[0]
        )
        translated = transcript.translate("en")
        fetched = translated.fetch()
        return _clean_snippets(fetched.snippets)
    except (TranscriptsDisabled, VideoUnavailable) as e:
        logger.warning("Transcripts unavailable for %s: %s", video_id, e)
        return None
    except Exception as e:
        msg = str(e)
        if "blocking" in msg or "IP" in msg or "RequestBlocked" in msg or "IpBlocked" in msg:
            logger.warning("YouTube rate limit hit for %s — skipping (will retry tomorrow)", video_id)
        else:
            logger.warning("Failed to fetch transcript for %s: %s", video_id, e)
        return None


def fetch_new_videos(
    config: dict,
    state,
    transcript_cache_dir: str,
) -> list[VideoData]:
    """
    For each configured channel, retrieve videos published within
    lookback_hours, filter by duration and state, fetch transcripts,
    and return a list of VideoData objects.
    """
    lookback_hours = config["digest"]["lookback_hours"]
    min_duration = config["digest"]["min_duration_seconds"]
    max_videos = config["digest"]["max_videos_per_run"]
    max_transcript_chars = config["digest"]["max_transcript_chars"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    results: list[VideoData] = []

    for channel in config["channels"]:
        channel_name = channel["name"]

        # Accept either a URL/handle or a legacy channel_id
        channel_url = channel.get("url", "")
        channel_id = channel.get("channel_id", "")

        # Normalize bare @handle to a full URL
        if channel_url and channel_url.startswith("@"):
            channel_url = f"https://www.youtube.com/{channel_url}"

        logger.info("Scanning channel: %s", channel_name)

        try:
            if channel_url:
                videos_gen = scrapetube.get_channel(channel_url=channel_url, limit=20)
            elif channel_id:
                videos_gen = scrapetube.get_channel(channel_id, limit=20)
            else:
                logger.error("Channel '%s' has no url or channel_id — skipping", channel_name)
                continue
        except Exception as e:
            logger.error("Failed to fetch channel %s: %s", channel_name, e)
            continue

        for video in videos_gen:
            if len(results) >= max_videos:
                break

            video_id = video.get("videoId", "")
            if not video_id:
                continue

            # Skip already-processed videos
            if state.is_processed(video_id):
                continue

            # Parse title
            title = ""
            try:
                title = video["title"]["runs"][0]["text"]
            except (KeyError, IndexError):
                title = video_id

            # Parse publish time
            published_text = ""
            try:
                published_text = video["publishedTimeText"]["simpleText"]
            except KeyError:
                pass

            published_at = _parse_relative_time(published_text)
            if published_at is None:
                logger.debug("Could not parse time for '%s', skipping", title)
                continue

            if published_at < cutoff:
                # scrapetube returns videos newest-first; once we're past the
                # cutoff for this channel we can stop
                break

            # Parse duration
            duration_str = ""
            try:
                duration_str = video["lengthText"]["simpleText"]
            except KeyError:
                pass
            duration_secs = _parse_duration(duration_str)

            if duration_secs < min_duration:
                logger.debug("Skipping short video '%s' (%ds)", title, duration_secs)
                continue

            url = f"https://www.youtube.com/watch?v={video_id}"

            # Check transcript cache
            cache_path = os.path.join(transcript_cache_dir, f"{video_id}.txt")
            if os.path.exists(cache_path):
                with open(cache_path) as f:
                    transcript_text = f.read()
                logger.debug("Loaded cached transcript for '%s'", title)
            else:
                logger.info("Fetching transcript for '%s'", title)
                time.sleep(2)  # Polite delay to avoid YouTube rate limits
                transcript_text = _get_transcript(video_id)
                if transcript_text is None:
                    logger.warning("No transcript available for '%s', skipping", title)
                    continue
                # Truncate if needed
                if len(transcript_text) > max_transcript_chars:
                    transcript_text = transcript_text[:max_transcript_chars]
                    transcript_text += "\n[Transcript truncated for processing]"
                # Cache to disk
                with open(cache_path, "w") as f:
                    f.write(transcript_text)

            results.append(VideoData(
                video_id=video_id,
                title=title,
                channel_name=channel_name,
                url=url,
                duration_seconds=duration_secs,
                transcript_text=transcript_text,
            ))
            logger.info("Queued: '%s' from %s", title, channel_name)

    logger.info("Total new videos to process: %d", len(results))
    return results
