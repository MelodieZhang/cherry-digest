# Cherry 🍒

A daily YouTube digest that runs every morning, summarizes new videos from your subscribed channels using Gemini, and emails you a bilingual (English + Chinese) digest.

## How it works

1. **Fetch** — scans your configured YouTube channels for videos posted in the last 24 hours
2. **Summarize** — transcribes and summarizes each video with Gemini (results cached locally)
3. **Translate** — translates key points to Chinese in a natural, conversational tone
4. **Email** — sends a clean HTML digest to your Gmail inbox

Runs automatically every morning via macOS launchd. No API key needed for YouTube — transcripts are fetched directly.

## Setup

### 1. Prerequisites

- macOS (uses launchd for scheduling)
- Python 3.9+
- A [Gemini API key](https://aistudio.google.com/) (free tier works)
- A Gmail account with [App Password](https://myaccount.google.com/apppasswords) enabled

### 2. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Set environment variables

Copy `.env.example` and fill in your keys:

```bash
# Add to ~/.zshrc or ~/.bash_profile
export GEMINI_API_KEY=AIza...
export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"
```

### 4. Configure channels and email

Edit `config.yaml`:

```yaml
channels:
  - name: "Your Channel"
    url: "https://www.youtube.com/@yourchannel"

email:
  sender: "you@gmail.com"
  recipient: "you@gmail.com"
```

### 5. Test a manual run

```bash
source venv/bin/activate
python main.py
```

### 6. Schedule daily runs (7 AM)

```bash
# Fill in your API keys in the plist first
nano launchd/com.user.youtubedigest.plist

cp launchd/com.user.youtubedigest.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.youtubedigest.plist
```

## Cost

Uses [Gemini 2.0 Flash-Lite](https://ai.google.dev/pricing) — approximately **$0.04–0.05/day** for a typical digest of 3–6 videos.

## Project structure

```
main.py                  # Pipeline entrypoint
config.yaml              # Channels, email, model settings
modules/
  fetcher.py             # YouTube channel scanning + transcript fetching
  summarizer.py          # Gemini summarization (with disk cache)
  emailer.py             # Chinese translation + Gmail delivery
  state_manager.py       # Tracks processed videos across runs
launchd/
  com.user.youtubedigest.plist   # macOS launchd schedule (7 AM daily)
```
