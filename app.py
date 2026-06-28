import os
import re
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
import markdown as md

# ---------------------------------------------------------------------------
# Notion markdown → blocks converter
# ---------------------------------------------------------------------------
_NOTION_LANGS = {
    "python","javascript","typescript","java","c","cpp","csharp","go","ruby",
    "rust","swift","kotlin","scala","php","bash","shell","sql","html","css",
    "json","yaml","xml","markdown","r","dart","lua","perl","haskell","elixir",
    "plain text","mermaid",
}

def _parse_inline(text: str) -> list:
    parts = []
    last = 0
    for m in re.finditer(r'\*\*(.+?)\*\*', text):
        if m.start() > last:
            parts.append({"type": "text", "text": {"content": text[last:m.start()]}})
        parts.append({"type": "text", "text": {"content": m.group(1)},
                      "annotations": {"bold": True}})
        last = m.end()
    if last < len(text):
        parts.append({"type": "text", "text": {"content": text[last:]}})
    return parts or [{"type": "text", "text": {"content": text}}]

def md_to_notion_blocks(text: str) -> list:
    blocks = []
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith('```'):
            lang = stripped[3:].strip().lower() or 'plain text'
            if lang not in _NOTION_LANGS:
                lang = 'plain text'
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            code = '\n'.join(code_lines)[:2000]
            if code.strip():
                blocks.append({
                    "object": "block", "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": code}}],
                        "language": lang,
                    }
                })
            i += 1
            continue
        if stripped.startswith('### '):
            blocks.append({"object":"block","type":"heading_3",
                           "heading_3":{"rich_text":_parse_inline(stripped[4:])}})
        elif stripped.startswith('## '):
            blocks.append({"object":"block","type":"heading_2",
                           "heading_2":{"rich_text":_parse_inline(stripped[3:])}})
        elif stripped.startswith('# '):
            blocks.append({"object":"block","type":"heading_1",
                           "heading_1":{"rich_text":_parse_inline(stripped[2:])}})
        elif stripped == '---':
            blocks.append({"object":"block","type":"divider","divider":{}})
        elif stripped.startswith('> '):
            blocks.append({"object":"block","type":"quote",
                           "quote":{"rich_text":_parse_inline(stripped[2:])}})
        elif stripped.startswith('- ') or stripped.startswith('* '):
            blocks.append({"object":"block","type":"bulleted_list_item",
                           "bulleted_list_item":{"rich_text":_parse_inline(stripped[2:])}})
        elif re.match(r'^\d+\.\s', stripped):
            txt = re.sub(r'^\d+\.\s+', '', stripped)
            blocks.append({"object":"block","type":"numbered_list_item",
                           "numbered_list_item":{"rich_text":_parse_inline(txt)}})
        elif stripped.startswith('*') and stripped.endswith('*') and not stripped.startswith('**'):
            blocks.append({"object":"block","type":"paragraph",
                           "paragraph":{"rich_text":[{"type":"text","text":{"content":stripped.strip('*')},
                                                      "annotations":{"italic":True}}]}})
        elif not stripped:
            pass
        else:
            blocks.append({"object":"block","type":"paragraph",
                           "paragraph":{"rich_text":_parse_inline(stripped)}})
        i += 1
    return blocks


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
load_dotenv()
from summarizer import extract_video_id, fetch_transcript, summarize_video, generate_weekly_digest, fetch_video_metadata, get_playlist_videos
from db import (
    db, init_db,
    get_user_by_email, get_user_by_id, create_user,
    save_summary, get_all_summaries, get_summary, delete_summary,
    delete_all_summaries, get_summary_by_video_id, search_summaries,
    check_and_increment_usage, get_usage_today,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")

from pathlib import Path
raw_db_url = os.getenv("DATABASE_URL", f"sqlite:///{Path(__file__).parent / 'summaries.db'}")
if raw_db_url.startswith("postgres://"):
    raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = raw_db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

init_db(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."

@login_manager.user_loader
def load_user(user_id):
    return get_user_by_id(int(user_id))

def to_html(text):
    try:
        return md.markdown(text, extensions=["extra", "nl2br", "fenced_code", "tables"])
    except Exception:
        return md.markdown(text)

def extract_title_from_summary(summary_md):
    for line in summary_md.splitlines():
        line = line.strip()
        if line.startswith("###"):
            title = re.sub(r'^#+\s*', '', line).strip()
            title = re.sub(r'^[^\w]+', '', title).strip()
            if title:
                return title[:120]
    return ""

DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "20"))

def get_anthropic_key():
    return os.getenv("ANTHROPIC_API_KEY", "")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email    = (request.form.get("email") or "").strip()
        password = (request.form.get("password") or "").strip()
        user = get_user_by_email(email)
        if user and user.check_password(password):
            login_user(user, remember=True)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))
        error = "Invalid email or password."
    return render_template("login.html", error=error)

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email    = (request.form.get("email") or "").strip()
        password = (request.form.get("password") or "").strip()
        confirm  = (request.form.get("confirm") or "").strip()
        if not email or not password:
            error = "Email and password are required."
        elif password != confirm:
            error = "Passwords do not match."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif get_user_by_email(email):
            error = "An account with that email already exists."
        else:
            user = create_user(email, password)
            login_user(user, remember=True)
            return redirect(url_for("index"))
    return render_template("register.html", error=error)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user)

@app.route("/api/usage", methods=["GET"])
@login_required
def api_usage():
    return jsonify(get_usage_today(current_user, DAILY_LIMIT))

@app.route("/api/summarize", methods=["POST"])
@login_required
def api_summarize():
    api_key = get_anthropic_key()
    if not api_key:
        return jsonify({"error": "Service not configured — contact the administrator."}), 503
    data         = request.get_json()
    url          = (data.get("url") or "").strip()
    video_title  = (data.get("title") or "").strip()
    channel_name = (data.get("channel") or "").strip()
    video_date   = (data.get("date") or datetime.today().strftime("%B %d, %Y")).strip()
    if not url:
        return jsonify({"error": "YouTube URL is required."}), 400
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "Could not parse a valid YouTube video ID from that URL."}), 400
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    if not video_title or not channel_name:
        fetched_title, fetched_channel = fetch_video_metadata(video_id)
        video_title  = video_title  or fetched_title
        channel_name = channel_name or fetched_channel
    force = (data.get("force") or "").strip().lower() == "true"
    if not force:
        existing = get_summary_by_video_id(video_id, user_id=current_user.id)
        if existing:
            return jsonify({"success": True, "cached": True, "id": existing["id"],
                "title": existing["title"], "channel": existing["channel"],
                "summary_md": existing["summary"], "summary_html": to_html(existing["summary"]),
                "transcript_length": 0, "lang": existing.get("lang", "en")})

    # Daily limit check (cached hits don't count against quota)
    allowed, remaining = check_and_increment_usage(current_user, DAILY_LIMIT)
    if not allowed:
        return jsonify({"error": f"Daily limit of {DAILY_LIMIT} summaries reached. Resets at midnight."}), 429

    try:
        transcript, lang = fetch_transcript(video_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    try:
        summary_md = summarize_video(transcript=transcript,
            video_title=video_title or "Unknown Title",
            channel_name=channel_name or "Unknown Channel",
            video_url=video_url, video_date=video_date, api_key=api_key)
    except Exception as e:
        return jsonify({"error": f"Claude API error: {str(e)}"}), 500
    resolved_title   = video_title or extract_title_from_summary(summary_md) or "Untitled Video"
    resolved_channel = channel_name or "Unknown Channel"
    summary_html     = to_html(summary_md)
    entry = {"video_id": video_id, "url": video_url, "title": resolved_title,
        "channel": resolved_channel, "date": video_date, "lang": lang,
        "summary": summary_md, "created_at": datetime.now().isoformat()}
    try:
        new_id = save_summary(entry, user_id=current_user.id)
    except Exception:
        new_id = 0
    return jsonify({"success": True, "id": new_id, "title": entry["title"],
        "channel": entry["channel"], "summary_md": summary_md,
        "summary_html": summary_html, "transcript_length": len(transcript), "lang": lang})

@app.route("/api/history", methods=["GET"])
@login_required
def api_history():
    return jsonify({"summaries": get_all_summaries(user_id=current_user.id)})

@app.route("/api/search", methods=["GET"])
@login_required
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    return jsonify({"results": search_summaries(q, user_id=current_user.id)})

@app.route("/api/playlist", methods=["POST"])
@login_required
def api_playlist():
    data = request.get_json()
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        return jsonify(get_playlist_videos(url))
    except ValueError as e:
        return jsonify({"error": str(e)}), 422

@app.route("/api/summary/<int:summary_id>/obsidian", methods=["GET"])
@login_required
def export_obsidian(summary_id):
    s = get_summary(summary_id, user_id=current_user.id)
    if not s:
        return jsonify({"error": "Not found"}), 404
    safe_title = re.sub(r'[\\/*?:"<>|]', "", s["title"])[:80].strip()
    content = f"""---\ntitle: "{s['title'].replace('"', "'")}"\nchannel: "{s['channel']}"\nurl: "{s['url']}"\ndate: "{s['date']}"\ntags: [youtube, summary]\n---\n\n{s['summary']}\n"""
    from flask import Response
    return Response(content, mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.md"'})

@app.route("/api/summary/<int:summary_id>/notion", methods=["POST"])
@login_required
def export_notion(summary_id):
    import requests as req
    notion_key = os.getenv("NOTION_API_KEY", "").strip()
    notion_db  = os.getenv("NOTION_DATABASE_ID", "").strip()
    if not notion_key or not notion_db:
        return jsonify({"error": "Add NOTION_API_KEY and NOTION_DATABASE_ID to your .env file"}), 400
    s = get_summary(summary_id, user_id=current_user.id)
    if not s:
        return jsonify({"error": "Not found"}), 404
    headers = {"Authorization": f"Bearer {notion_key}", "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"}
    children = md_to_notion_blocks(s["summary"])[:95]
    body = {"parent": {"database_id": notion_db},
        "properties": {
            "title": {"title": [{"text": {"content": s["title"][:255]}}]},
            "Channel": {"rich_text": [{"text": {"content": s["channel"]}}]},
            "URL": {"rich_text": [{"text": {"content": s["url"]}}]},
            "Date": {"rich_text": [{"text": {"content": s["date"]}}]},
        }, "children": children}
    resp = req.post("https://api.notion.com/v1/pages", headers=headers, json=body, timeout=15)
    if resp.ok:
        return jsonify({"url": resp.json().get("url", ""), "success": True})
    return jsonify({"error": resp.json().get("message", "Notion API error")}), 400

@app.route("/api/summary/<int:summary_id>", methods=["GET"])
@login_required
def api_get_summary(summary_id):
    s = get_summary(summary_id, user_id=current_user.id)
    if not s:
        return jsonify({"error": "Summary not found."}), 404
    return jsonify({**s, "summary_html": to_html(s["summary"])})

@app.route("/api/summary/<int:summary_id>", methods=["DELETE"])
@login_required
def api_delete_summary(summary_id):
    delete_summary(summary_id, user_id=current_user.id)
    return jsonify({"success": True})

@app.route("/api/clear", methods=["DELETE"])
@login_required
def api_clear():
    delete_all_summaries(user_id=current_user.id)
    return jsonify({"success": True})

@app.route("/api/digest", methods=["POST"])
@login_required
def api_digest():
    api_key = get_anthropic_key()
    if not api_key:
        return jsonify({"error": "Service not configured — contact the administrator."}), 503
    data       = request.get_json()
    ids        = data.get("ids")
    week_range = (data.get("week_range") or "").strip()
    all_s = get_all_summaries(user_id=current_user.id)
    if ids:
        summaries = [get_summary(i, user_id=current_user.id) for i in set(ids)]
    else:
        summaries = [get_summary(s["id"], user_id=current_user.id) for s in all_s]
    summaries = [s for s in summaries if s]
    if not summaries:
        return jsonify({"error": "No summaries available. Summarize some videos first."}), 400
    if not week_range:
        week_range = datetime.today().strftime("Week of %B %d, %Y")
    try:
        digest_md = generate_weekly_digest(
            summaries=[{"title": s["title"], "summary": s["summary"]} for s in summaries],
            week_range=week_range, api_key=api_key)
    except Exception as e:
        return jsonify({"error": f"Claude API error: {str(e)}"}), 500
    return jsonify({"success": True, "digest_md": digest_md,
        "digest_html": to_html(digest_md), "video_count": len(summaries),
        "week_range": week_range})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"\n YouTube Content Summarizer running at http://localhost:{port}\n")
    app.run(debug=True, port=port)
