"""
Bilingual email digest sender.

Translates per-video summaries to Chinese via Gemini, then composes
and sends an HTML email via Gmail SMTP.

Required config:
  email.sender    — Gmail address to send from
  email.recipient — inbox to deliver to

Required env var:
  EMAIL_PASSWORD  — Gmail App Password (not your login password)
  Get one at: Google Account → Security → App Passwords
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google import genai
from google.genai import types as genai_types
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ── Translation ───────────────────────────────────────────────────────────────

_TRANSLATE_SYSTEM = (
    "You are a native Simplified Chinese writer who also reads English fluently. "
    "Translate the given JSON fields into natural, conversational Simplified Chinese — "
    "write as if explaining to a smart friend, not as a formal document. "
    "Use everyday vocabulary; avoid stiff or overly literal phrasing. "
    "Keep proper nouns, brand names, and technical terms in their common Chinese forms or as-is if no natural equivalent exists. "
    "Return only valid JSON with the same structure."
)


def translate_to_chinese(summaries: list[dict], config: dict, api_key: str) -> list[dict]:
    """
    Translate one_line_summary and key_points for each summary into Chinese.
    Returns a parallel list of dicts with keys: one_line_summary_zh, key_points_zh.
    Verbatim quotes are left in English (they are exact transcript excerpts).
    """
    # Build compact payload — only the fields we want translated
    payload = [
        {
            "id": s.get("video_id", str(i)),
            "one_line_summary": s.get("one_line_summary", ""),
            "key_points": s.get("key_points", []),
        }
        for i, s in enumerate(summaries)
    ]

    prompt = (
        "Translate the following JSON fields from English to Simplified Chinese. "
        "Return a JSON array with exactly the same structure and same 'id' values, "
        "but with 'one_line_summary' and 'key_points' translated.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )

    client = genai.Client(api_key=api_key)
    model_name = config["gemini"]["summarize_model"]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def _call() -> str:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=_TRANSLATE_SYSTEM,
                max_output_tokens=4096,
            ),
        )
        return response.text

    raw = _call()

    # Parse response — strip markdown fences if present
    import re
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    translated = json.loads(cleaned)

    # Index by id for easy lookup
    by_id = {item["id"]: item for item in translated}
    result = []
    for s in summaries:
        vid_id = s.get("video_id", "")
        tr = by_id.get(vid_id, {})
        result.append({
            "one_line_summary_zh": tr.get("one_line_summary", ""),
            "key_points_zh": tr.get("key_points", []),
        })
    return result


# ── HTML builder ──────────────────────────────────────────────────────────────

def _build_html(summaries: list[dict], translations: list[dict], date_str: str) -> str:
    video_sections = []
    for s, zh in zip(summaries, translations):
        title = s.get("title", "Untitled")
        channel = s.get("channel", "")
        url = s.get("url", "")
        one_line = s.get("one_line_summary", "")
        key_points = s.get("key_points", [])
        one_line_zh = zh.get("one_line_summary_zh", "")
        key_points_zh = zh.get("key_points_zh", [])

        en_points = "".join(f"<li>{p}</li>" for p in key_points)
        zh_points = "".join(f"<li>{p}</li>" for p in key_points_zh)

        video_sections.append(f"""
<div style="margin-bottom:36px;padding-bottom:24px;border-bottom:1px solid #eee;">
  <h2 style="margin:0 0 2px;font-size:18px;color:#111;">
    <a href="{url}" style="color:#111;text-decoration:none;">{title}</a>
  </h2>
  <p style="margin:0 0 12px;font-size:13px;color:#888;">{channel} &nbsp;·&nbsp;
    <a href="{url}" style="color:#111;">Watch on YouTube ↗</a>
  </p>

  <p style="margin:0 0 6px;font-size:14px;"><strong>Summary:</strong> {one_line}</p>
  <ul style="margin:0 0 10px;padding-left:20px;font-size:14px;line-height:1.6;">
    {en_points}
  </ul>

  <div style="margin-top:14px;padding:12px 14px;background:#f9f9f9;border-radius:6px;">
    <p style="margin:0 0 6px;font-size:14px;color:#555;">
      <strong>摘要：</strong>{one_line_zh}
    </p>
    <ul style="margin:0;padding-left:20px;font-size:14px;color:#555;line-height:1.6;">
      {zh_points}
    </ul>
  </div>
</div>""")

    body = "\n".join(video_sections)
    n = len(summaries)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             max-width:680px;margin:0 auto;padding:24px;color:#222;">

  <div style="margin-bottom:28px;padding-bottom:16px;border-bottom:2px solid #111;">
    <h1 style="margin:0;font-size:22px;color:#111;">🍒 Cherry Digest</h1>
    <p style="margin:4px 0 0;font-size:14px;color:#888;">{date_str} &nbsp;·&nbsp; {n} video{"s" if n != 1 else ""}</p>
  </div>

  {body}

  <p style="font-size:12px;color:#aaa;margin-top:8px;">
    Generated by Cherry · Powered by Gemini 3.1 Flash-Lite
  </p>
</body>
</html>"""


# ── Email sender ──────────────────────────────────────────────────────────────

def send_digest_email(summaries: list[dict], config: dict, api_key: str):
    """Translate summaries to Chinese, compose HTML email, and send via Gmail."""
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled", True):
        logger.info("Email disabled in config, skipping.")
        return

    sender = email_cfg.get("sender", "")
    recipient = email_cfg.get("recipient", "")
    if not sender or not recipient:
        raise ValueError("config.email.sender and config.email.recipient must be set")

    password = os.environ.get("EMAIL_PASSWORD", "")
    if not password:
        raise ValueError("EMAIL_PASSWORD env var not set. Generate a Gmail App Password.")

    date_str = datetime.now().strftime("%A, %B %-d, %Y")
    n = len(summaries)
    subject = f"Cherry Digest — {date_str} ({n} video{'s' if n != 1 else ''})"

    if not summaries:
        logger.info("No summaries to email — skipping.")
        return

    logger.info("Translating summaries to Chinese...")
    translations = translate_to_chinese(summaries, config, api_key)

    logger.info("Composing email digest...")
    html = _build_html(summaries, translations, date_str)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))

    logger.info("Sending email to %s...", recipient)
    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(sender, password)
        smtp.sendmail(sender, recipient, msg.as_bytes())

    logger.info("Email sent: %s", subject)
