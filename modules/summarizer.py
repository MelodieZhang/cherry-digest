"""
Per-video summarization using Gemini API.

Produces structured JSON with key points and verbatim transcript highlights.
Results are cached to disk so retries don't re-invoke the API.
"""

import json
import logging
import os
import re

from google import genai
from google.genai import types as genai_types
from tenacity import retry, stop_after_attempt, wait_exponential

from .fetcher import VideoData

logger = logging.getLogger(__name__)

SUMMARY_PROMPT_SYSTEM = """\
You are a podcast producer assistant. Given a YouTube video transcript, \
produce a concise, structured summary that captures the essence of the video \
for someone who hasn't watched it.\
"""

SUMMARY_PROMPT_TEMPLATE = """\
Video: "{title}"
Channel: {channel}
URL: {url}

Transcript:
{transcript}

Produce a JSON response with EXACTLY this structure (no other text, just JSON):
{{
  "title": "{title}",
  "channel": "{channel}",
  "key_points": [
    "2-3 sentences: state the idea, then ground it with the speaker's own words or a concrete example from the transcript",
    "Another key point in the same style"
  ],
  "one_line_summary": "A single sentence capturing the core thesis or main takeaway"
}}

Requirements:
- key_points: 4 to 7 items; each item is 2-3 sentences — first sentence states the idea clearly, the rest illustrate it using the speaker's actual phrasing, a specific example, or a short direct quote woven naturally into the text (no standalone quote blocks)
- one_line_summary: one sentence only
- Preserve the speaker's original language and concrete examples as much as possible — avoid generic paraphrasing
- Only include points that carry genuine insight — if the video is light on substance, fewer points is better than padding; cap at 500 words but don't force length
"""


def _extract_json(text: str) -> dict:
    """Extract JSON from a response that may have surrounding text."""
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Strip markdown code fences if present
    text_stripped = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        return json.loads(text_stripped)
    except json.JSONDecodeError:
        pass
    # Try to find JSON block via regex
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not extract valid JSON from response: {text[:300]}")


def _summarize_one(video: VideoData, client: genai.Client, model_name: str) -> dict:
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        title=video.title,
        channel=video.channel_name,
        url=video.url,
        transcript=video.transcript_text,
    )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        reraise=True,
    )
    def _call():
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=SUMMARY_PROMPT_SYSTEM,
                max_output_tokens=2500,
            ),
        )
        return response.text

    raw = _call()
    return _extract_json(raw)


def summarize_all(
    videos: list,
    config: dict,
    api_key: str,
    cache_dir: str,
) -> list[dict]:
    """
    Summarize each video, using disk cache to avoid re-calling the API.
    Returns a list of summary dicts (same order as input videos).
    """
    client = genai.Client(api_key=api_key)
    model_name = config["gemini"]["summarize_model"]
    summaries = []

    for video in videos:
        cache_path = os.path.join(cache_dir, f"{video.video_id}.json")

        if os.path.exists(cache_path):
            with open(cache_path) as f:
                summary = json.load(f)
            logger.debug("Loaded cached summary for '%s'", video.title)
        else:
            logger.info("Summarizing '%s'", video.title)
            try:
                summary = _summarize_one(video, client, model_name)
                # Ensure required fields exist
                summary.setdefault("title", video.title)
                summary.setdefault("channel", video.channel_name)
                summary.setdefault("url", video.url)
                summary.setdefault("video_id", video.video_id)
                summary["url"] = video.url
                summary["video_id"] = video.video_id
                with open(cache_path, "w") as f:
                    json.dump(summary, f, indent=2)
            except Exception as e:
                logger.error("Failed to summarize '%s': %s", video.title, e)
                continue  # Skip this video rather than aborting the run

        summaries.append(summary)

    return summaries
