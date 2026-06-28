import re
import os
import json
import urllib.request
from datetime import datetime
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled
import anthropic
from prompts import MASTER_SYSTEM_PROMPT, STAGE1_PROMPT, STAGE2_PROMPT


def extract_video_id(url: str):
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"(?:youtu\.be\/)([0-9A-Za-z_-]{11})",
        r"(?:embed\/)([0-9A-Za-z_-]{11})",
        r"(?:shorts\/)([0-9A-Za-z_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_playlist_videos(playlist_url: str) -> list[dict]:
    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "extract_flat": True,
            "skip_download": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
            entries = info.get("entries", []) or []
            playlist_title = info.get("title", "Playlist")
            videos = []
            for e in entries:
                if not e or not e.get("id"):
                    continue
                videos.append({
                    "id": e["id"],
                    "title": e.get("title", "Unknown"),
                    "url": f"https://www.youtube.com/watch?v={e['id']}",
                    "duration": e.get("duration"),
                    "channel": e.get("uploader") or e.get("channel", ""),
                })
            return {"title": playlist_title, "videos": videos}
    except ImportError:
        raise ValueError("yt-dlp is not installed. Run: pip install yt-dlp")
    except Exception as e:
        raise ValueError(f"Could not load playlist: {str(e)}")


def fetch_video_metadata(video_id: str):
    try:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
            return data.get("title", ""), data.get("author_name", "")
    except Exception:
        return "", ""


def fetch_transcript(video_id: str):
    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_manually_created_transcript(["en"])
        except Exception:
            try:
                transcript = transcript_list.find_generated_transcript(["en"])
            except Exception:
                transcript = next(iter(transcript_list))

        fetched = transcript.fetch()
        text = " ".join(
            snippet.text if hasattr(snippet, "text") else snippet.get("text", "")
            for snippet in fetched
        )
        text = re.sub(r"\s+", " ", text).strip()
        return text, transcript.language_code

    except TranscriptsDisabled:
        raise ValueError("Transcripts are disabled for this video.")
    except NoTranscriptFound:
        raise ValueError("No transcript found. The creator may not have enabled captions.")
    except Exception as e:
        raise ValueError(f"Could not fetch transcript: {str(e)}")


def get_claude_client(api_key: str = None):
    key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("No Anthropic API key available. Add your key in Settings.")
    return anthropic.Anthropic(api_key=key)


def summarize_video(transcript, video_title="Unknown Title", channel_name="Unknown Channel",
                    video_url="", video_date="", api_key=None):
    client = get_claude_client(api_key)
    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    max_chars = 80000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars] + "\n\n[Transcript truncated for length]"

    video_date_str = video_date or datetime.today().strftime("%B %d, %Y")
    user_prompt = STAGE1_PROMPT
    user_prompt = user_prompt.replace("{channel_name}", channel_name or "Unknown Channel")
    user_prompt = user_prompt.replace("{video_title}", video_title or "Unknown Title")
    user_prompt = user_prompt.replace("{video_url}", video_url or "")
    user_prompt = user_prompt.replace("{video_date}", video_date_str)
    user_prompt = user_prompt.replace("{transcript}", transcript)

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        system=MASTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text


def generate_weekly_digest(summaries, week_range="", api_key=None):
    client = get_claude_client(api_key)
    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    if not week_range:
        week_range = datetime.today().strftime("%B %d, %Y")

    combined = "\n\n---\n\n".join(
        f"## Video {i+1}: {s.get('title', 'Untitled')}\n\n{s['summary']}"
        for i, s in enumerate(summaries)
    )

    user_prompt = STAGE2_PROMPT
    user_prompt = user_prompt.replace("{week_range}", week_range)
    user_prompt = user_prompt.replace("{video_count}", str(len(summaries)))
    user_prompt = user_prompt.replace("{summaries}", combined)

    message = client.messages.create(
        model=model,
        max_tokens=8192,
        system=MASTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text
