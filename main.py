import os
import json
import uuid
import subprocess
import shutil
import time
import re
from pathlib import Path
from typing import Optional

import google.generativeai as genai
import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="ShortCut — YouTube Shorts Extractor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WORK_DIR = Path("./work")
WORK_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path("./outputs")
OUTPUT_DIR.mkdir(exist_ok=True)
RATINGS_FILE = Path("./ratings.json")
COOKIES_FILE = Path("./cookies.txt")

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
gemini = genai.GenerativeModel("gemini-2.5-flash")

INDIAN_LANG_CODES = ["hi", "ta", "te", "kn", "ml", "bn", "mr", "gu", "pa", "en"]


# ── Models ───────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    youtube_url: str
    short_duration: int
    num_shorts: int
    detection_modes: list[str]
    manual_timestamps: Optional[list[dict]] = None
    language: str = "auto"

class ExtractRequest(BaseModel):
    youtube_url: str
    segments: list[dict]
    job_id: str

class RateRequest(BaseModel):
    label: str
    reason: str
    rating: str


# ── Helpers: video ID ────────────────────────────────────────────────────────

def get_video_id(url: str) -> str:
    patterns = [r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})"]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    raise ValueError("Could not extract video ID from URL")


def base_ydl_opts() -> dict:
    """Base yt-dlp options applied to every call."""
    opts = {
        "nocheckcertificate": True,
        "remote_components": {"ejs:github"},
        "extractor_args": {"youtube": {"player_client": ["web_creator", "default"]}},
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


# ── Helpers: ratings ─────────────────────────────────────────────────────────

def load_ratings() -> dict:
    if RATINGS_FILE.exists():
        try:
            return json.loads(RATINGS_FILE.read_text())
        except Exception:
            pass
    return {"liked": [], "disliked": []}


def save_rating(label: str, reason: str, rating: str):
    data = load_ratings()
    entry = {"label": label, "reason": reason}
    if rating == "liked":
        if entry not in data["liked"]:
            data["liked"].append(entry)
    elif rating == "disliked":
        if entry not in data["disliked"]:
            data["disliked"].append(entry)
    data["liked"] = data["liked"][-20:]
    data["disliked"] = data["disliked"][-20:]
    RATINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def build_preference_context(ratings: dict) -> str:
    lines = []
    if ratings["liked"]:
        lines.append("CLIPS THE USER LIKED BEFORE (replicate these patterns):")
        for r in ratings["liked"][-5:]:
            lines.append("  ✓ " + r["label"] + " — " + r["reason"])
    if ratings["disliked"]:
        lines.append("CLIPS THE USER DISLIKED BEFORE (avoid these patterns):")
        for r in ratings["disliked"][-5:]:
            lines.append("  ✗ " + r["label"] + " — " + r["reason"])
    return "\n".join(lines) if lines else ""


# ── Helpers: transcript ───────────────────────────────────────────────────────

def get_transcript(video_id: str, job_dir: Path = None, language: str = "auto") -> list[dict]:
    """Fetch transcript. Tries youtube-transcript-api first, then yt-dlp."""
    if job_dir is None:
        job_dir = WORK_DIR / "transcript_tmp"
        job_dir.mkdir(exist_ok=True)

    langs_to_try = [language] if language != "auto" else INDIAN_LANG_CODES
    print(f"[TRANSCRIPT] Starting fetch for video_id={video_id}, language={language}")

    # Primary: youtube-transcript-api (less bot-detectable)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        for lang in langs_to_try:
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang])
                if transcript:
                    print(f"[TRANSCRIPT] youtube-transcript-api success, lang={lang}")
                    return transcript
            except Exception:
                time.sleep(1)
                continue
    except Exception as e:
        print(f"[TRANSCRIPT] youtube-transcript-api failed: {e}")

    # Fallback: yt-dlp subtitle download
    out_template = str(job_dir / "%(id)s")
    for write_auto in [True, False]:
        for lang in langs_to_try:
            print(f"[TRANSCRIPT] yt-dlp trying lang={lang}, write_auto={write_auto}")
            ydl_opts = {
                **base_ydl_opts(),
                "skip_download": True,
                "writeautomaticsub": write_auto,
                "writesubtitles": not write_auto,
                "subtitleslangs": [lang],
                "subtitlesformat": "vtt",
                "outtmpl": out_template,
                "quiet": True,
                "sleep_interval_subtitles": 2,
            }
            for attempt in range(3):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
                    for f in job_dir.iterdir():
                        if f.suffix == ".vtt" and f".{lang}." in f.name:
                            print(f"Found subtitle: {f.name} (lang={lang})")
                            return parse_subtitle_file(f)
                    break
                except Exception as e:
                    err = str(e)
                    if "429" in err:
                        wait = 5 * (attempt + 1)
                        print(f"Rate limited on {lang}, waiting {wait}s...")
                        time.sleep(wait)
                    else:
                        print(f"Subtitle fetch failed ({lang}): {e}")
                        break

    return []


def parse_subtitle_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json3":
        try:
            data = json.loads(text)
            result = []
            for event in data.get("events", []):
                start = event.get("tStartMs", 0) / 1000
                dur = event.get("dDurationMs", 0) / 1000
                segs = event.get("segs", [])
                line = "".join(s.get("utf8", "") for s in segs).strip()
                if line and line != "\n":
                    result.append({"text": line, "start": start, "duration": dur})
            return result
        except Exception:
            pass

    entries = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if "-->" in lines[i]:
            times = lines[i].split("-->")
            start_t = times[0].strip().split(".")[0]
            parts = [int(x) for x in start_t.split(":")]
            start = parts[0]*3600 + parts[1]*60 + parts[2] if len(parts)==3 else parts[0]*60+parts[1]
            texts = []
            i += 1
            while i < len(lines) and lines[i].strip() and "-->" not in lines[i]:
                clean = re.sub(r"<[^>]+>", "", lines[i]).strip()
                if clean:
                    texts.append(clean)
                i += 1
            if texts:
                entries.append({"text": " ".join(texts), "start": float(start), "duration": 3.0})
        i += 1
    return entries


# ── Helpers: AI analysis ──────────────────────────────────────────────────────

def analyze_with_ai(transcript: list[dict], duration: int, num_shorts: int) -> list[dict]:
    if not transcript:
        return []

    full_text = "\n".join(
        f"[{round(t['start'], 1)}s] {t['text']}" for t in transcript
    )

    ratings = load_ratings()
    preference_context = build_preference_context(ratings)
    preference_block = ("USER PREFERENCES (learn from these):\n" + preference_context + "\n") if preference_context else ""

    prompt = f"""You are an expert viral content editor specializing in Indian educational YouTube content for YouTube Shorts, Instagram Reels and TikTok.

LANGUAGE RULE: Detect the language of the transcript. Write ALL "label" and "reason" fields in THAT EXACT LANGUAGE (Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, or English).

{preference_block}

YOUR TASK: Find the {num_shorts} BEST clips from this transcript that will perform well as standalone short-form videos.

WHAT MAKES A GREAT EDUCATIONAL SHORT:
1. STRONG HOOK in first 3 seconds — starts with a question, bold claim, surprising fact, or relatable problem
2. COMPLETE THOUGHT — the clip must be fully self-contained. A viewer who has never seen this video must understand it completely
3. SATISFYING ENDING — ends with a conclusion, punchline, key insight, or resolution. Never ends mid-sentence
4. HIGH ENERGY — moments where the educator sounds passionate, confident or emotional perform best
5. SIMPLE LANGUAGE — concepts explained simply with examples, analogies or stories

STRICT TIMESTAMP RULES:
- "start" must be EXACTLY at the beginning of a sentence — check transcript carefully
- "end" must be EXACTLY at the end of a complete sentence — never cut mid-word
- Read the transcript around your chosen timestamps carefully to verify natural boundaries

AVOID these common mistakes:
- Starting mid-explanation where context is needed
- Ending before the punchline or key point is delivered
- Clips that reference "as I mentioned earlier" or "in the previous video"
- Clips that end with "so..." or "and..." (incomplete thought)

Transcript with timestamps (format: [seconds] text):
{full_text}

Return ONLY a valid JSON array, no markdown, no explanation:
[
  {{
    "start": <float — exact second where a sentence begins>,
    "end": <float — exact second where a sentence ends>,
    "label": "<viral title in the video language, max 8 words, creates curiosity>",
    "score": <0.0-1.0 virality score>,
    "reason": "<one sentence explaining why this clip works, in the video language>",
    "content_type": "<teaching|motivation|surprising_fact|problem_solving|hook>"
  }}
]

Duration rules:
- Target duration is {duration} seconds per clip
- HARD LIMIT: clips must be between {duration - 10} and {duration + 15} seconds maximum
- Never make a clip longer than {duration + 15} seconds under any circumstances
- Finish the complete thought but trim to the nearest sentence end before the hard limit
- Return exactly {num_shorts} segments, no overlaps, all timestamps within transcript range.
"""

    resp = gemini.generate_content(prompt)
    raw = resp.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    segments = json.loads(raw)

    # Add transcript snippets
    for seg in segments:
        snip = " ".join(
            t["text"] for t in transcript
            if seg["start"] <= t["start"] <= seg["end"]
        )[:200]
        seg["transcript_snippet"] = snip

    return segments


# ── Helpers: audio energy ─────────────────────────────────────────────────────

def analyze_audio_energy(video_path: Path, duration: int, num_shorts: int) -> list[dict]:
    try:
        audio_path = video_path.parent / "audio.wav"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video_path),
            "-ac", "1", "-ar", "16000", str(audio_path)
        ], capture_output=True, check=True)

        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(video_path)
        ], capture_output=True, text=True)
        info = json.loads(probe.stdout)
        total_duration = float(info["format"]["duration"])

        window_scores = []
        step = max(duration // 2, 5)

        for start in range(0, int(total_duration) - duration, step):
            result = subprocess.run([
                "ffmpeg", "-y",
                "-ss", str(start), "-t", str(duration),
                "-i", str(audio_path),
                "-filter:a", "volumedetect",
                "-f", "null", "-"
            ], capture_output=True, text=True)
            for line in result.stderr.splitlines():
                if "mean_volume" in line:
                    try:
                        vol = float(line.split(":")[1].strip().replace(" dB", ""))
                        window_scores.append((start, vol))
                    except Exception:
                        pass

        if not window_scores:
            return []

        window_scores.sort(key=lambda x: x[1], reverse=True)
        selected = []
        used_ranges = []
        for start, vol in window_scores:
            overlaps = any(abs(start - s) < duration for s in used_ranges)
            if not overlaps:
                selected.append({
                    "start": float(start),
                    "end": float(start + duration),
                    "label": f"High energy @ {start}s",
                    "score": round((vol + 60) / 60, 2),
                    "reason": f"High audio energy segment (volume: {vol:.1f} dB)",
                    "transcript_snippet": ""
                })
                used_ranges.append(start)
            if len(selected) >= num_shorts:
                break

        return selected
    except Exception as e:
        print(f"Audio analysis failed: {e}")
        return []


# ── Helpers: video download ───────────────────────────────────────────────────

def download_video(url: str, job_dir: Path) -> Path:
    out_template = str(job_dir / "video.%(ext)s")
    ydl_opts = {
        **base_ydl_opts(),
        "format": "bestvideo[vcodec!*=av01][ext=mp4]+bestaudio[ext=m4a]/bestvideo[vcodec!*=av01]+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": out_template,
        "quiet": False,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 60,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    for f in job_dir.iterdir():
        if f.stem == "video":
            return f
    raise FileNotFoundError("Download failed")


# ── Helpers: clip extraction ──────────────────────────────────────────────────

def clip_video(video_path: Path, start: float, end: float, out_path: Path):
    """Cut segment with smooth face-tracking portrait crop (9:16)."""
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(str(video_path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        crop_w = int(frame_h * 9 / 16)
        out_w, out_h = 1080, 1920
        total_frames = int((end - start) * fps)
        fade_frames = int(fps * 1.5)
        default_x = (frame_w - crop_w) // 2

        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )

        detect_interval = max(1, int(fps * 0.5))
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
        raw_positions = {}

        for idx in range(total_frames):
            ret, frame = cap.read()
            if not ret:
                break
            if idx % detect_interval == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(
                    gray, scaleFactor=1.1, minNeighbors=5, minSize=(50, 50)
                )
                if len(faces) > 0:
                    lf = max(faces, key=lambda f: f[2] * f[3])
                    cx = lf[0] + lf[2] // 2
                    raw_positions[idx] = max(0, min(cx - crop_w // 2, frame_w - crop_w))

        if raw_positions:
            keys = sorted(raw_positions)
            all_x = []
            for i in range(total_frames):
                before = [k for k in keys if k <= i]
                after  = [k for k in keys if k >= i]
                if before and after:
                    k1, k2 = before[-1], after[0]
                    if k1 == k2:
                        all_x.append(raw_positions[k1])
                    else:
                        t = (i - k1) / (k2 - k1)
                        all_x.append(int(raw_positions[k1] + t * (raw_positions[k2] - raw_positions[k1])))
                elif before:
                    all_x.append(raw_positions[before[-1]])
                else:
                    all_x.append(raw_positions[after[0]])
        else:
            all_x = [default_x] * total_frames

        # Exponential Moving Average smoothing (gimbal-like, alpha=0.04)
        alpha = 0.04
        smoothed = np.zeros(len(all_x), dtype=np.float32)
        smoothed[0] = all_x[0]
        for i in range(1, len(all_x)):
            smoothed[i] = alpha * all_x[i] + (1.0 - alpha) * smoothed[i - 1]
        smoothed = np.clip(smoothed, 0, frame_w - crop_w)

        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
        temp_video = out_path.parent / f"_tmp_{out_path.stem}.avi"
        writer = cv2.VideoWriter(
            str(temp_video),
            cv2.VideoWriter_fourcc(*"MJPG"),
            fps, (out_w, out_h)
        )

        for idx in range(total_frames):
            ret, frame = cap.read()
            if not ret:
                break
            x = int(smoothed[idx]) if idx < len(smoothed) else default_x
            cropped = frame[:, x: x + crop_w]
            resized = cv2.resize(cropped, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
            frames_left = total_frames - idx
            if frames_left <= fade_frames and fade_frames > 0:
                fade_alpha = frames_left / fade_frames
                resized = (resized.astype(np.float32) * fade_alpha).astype(np.uint8)
            writer.write(resized)

        cap.release()
        writer.release()

        duration = end - start
        fade_start_t = max(0, duration - 1.5)
        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(temp_video),
            "-ss", str(start), "-to", str(end), "-i", str(video_path),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-c:a", "aac",
            "-af", f"afade=t=out:st={fade_start_t:.2f}:d=1.5",
            "-movflags", "+faststart",
            str(out_path)
        ], capture_output=True, check=True)
        temp_video.unlink(missing_ok=True)
        print(f"Face-tracked clip saved: {out_path.name}")

    except ImportError:
        print("OpenCV not available — falling back to center crop")
        _clip_video_fallback(video_path, start, end, out_path)
    except Exception as e:
        print(f"Tracking failed ({e}) — falling back to center crop")
        _clip_video_fallback(video_path, start, end, out_path)


def _clip_video_fallback(video_path: Path, start: float, end: float, out_path: Path):
    duration = end - start
    fade_start = max(0, duration - 1.5)
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", str(start), "-to", str(end), "-i", str(video_path),
        "-vf", f"crop=ih*9/16:ih,scale=1080:1920,fade=t=out:st={fade_start:.2f}:d=1.5",
        "-af", f"afade=t=out:st={fade_start:.2f}:d=1.5",
        "-c:v", "libx264", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out_path)
    ], capture_output=True, check=True)


def merge_and_deduplicate(segments_list, duration, num_shorts):
    all_segments = []
    for segs in segments_list:
        all_segments.extend(segs)
    all_segments.sort(key=lambda x: x.get("score", 0), reverse=True)
    final = []
    used = []
    for seg in all_segments:
        overlaps = any(abs(seg["start"] - s) < duration * 0.8 for s in used)
        if not overlaps:
            final.append(seg)
            used.append(seg["start"])
        if len(final) >= num_shorts:
            break
    return final[:num_shorts]


def cleanup_job(job_dir: Path):
    time.sleep(300)
    shutil.rmtree(job_dir, ignore_errors=True)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    try:
        video_id = get_video_id(req.youtube_url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    segments_list = []

    if "ai" in req.detection_modes:
        transcript = get_transcript(video_id, job_dir, language=req.language)
        if transcript:
            try:
                ai_segs = analyze_with_ai(transcript, req.short_duration, req.num_shorts)
                segments_list.append(ai_segs)
            except Exception as e:
                print(f"AI analysis failed: {e}")

    if "audio" in req.detection_modes:
        try:
            video_path = download_video(req.youtube_url, job_dir)
            audio_segs = analyze_audio_energy(video_path, req.short_duration, req.num_shorts)
            segments_list.append(audio_segs)
        except Exception as e:
            print(f"Audio analysis failed: {e}")

    if "manual" in req.detection_modes and req.manual_timestamps:
        manual_segs = [
            {
                "start": float(t["start"]),
                "end": float(t["start"]) + req.short_duration,
                "label": t.get("label", f"Manual clip @ {t['start']}s"),
                "score": 1.0,
                "reason": "Manually specified timestamp",
                "transcript_snippet": ""
            }
            for t in req.manual_timestamps
        ]
        segments_list.append(manual_segs)

    if not segments_list:
        raise HTTPException(500, "No segments could be identified. Try a different detection mode.")

    final_segments = merge_and_deduplicate(segments_list, req.short_duration, req.num_shorts)

    return {
        "job_id": job_id,
        "segments": final_segments,
        "total_found": len(final_segments)
    }


@app.post("/api/extract")
async def extract(req: ExtractRequest, background_tasks: BackgroundTasks):
    job_dir = WORK_DIR / req.job_id
    job_dir.mkdir(exist_ok=True)

    video_path = None
    for f in job_dir.iterdir():
        if f.stem == "video":
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0", str(f)
            ], capture_output=True, text=True)
            if "video" in probe.stdout:
                video_path = f
                print(f"Reusing existing video: {f.name}")
            else:
                print(f"Found audio-only file {f.name}, will re-download with video")
                f.unlink()
            break

    if not video_path:
        try:
            ydl_opts = {
                **base_ydl_opts(),
                "format": "bestvideo[vcodec!*=av01][ext=mp4]+bestaudio[ext=m4a]/bestvideo[vcodec!*=av01]+bestaudio/best",
                "merge_output_format": "mp4",
                "outtmpl": str(job_dir / "video.%(ext)s"),
                "quiet": False,
                "retries": 10,
                "fragment_retries": 10,
                "socket_timeout": 60,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([req.youtube_url])
            for f in job_dir.iterdir():
                if f.stem == "video":
                    video_path = f
                    break
        except Exception as e:
            raise HTTPException(500, f"Download failed: {e}")

    clips = []
    for i, seg in enumerate(req.segments):
        clip_name = f"short_{i+1}_{int(seg['start'])}s.mp4"
        out_path = OUTPUT_DIR / f"{req.job_id}_{clip_name}"
        try:
            clip_video(video_path, seg["start"], seg["end"], out_path)
            clips.append({
                "filename": out_path.name,
                "download_url": f"/api/download/{out_path.name}",
                "label": seg.get("label", clip_name),
                "start": seg["start"],
                "end": seg["end"]
            })
        except Exception as e:
            print(f"Failed to clip segment {i}: {e}")

    background_tasks.add_task(cleanup_job, job_dir)
    return {"clips": clips}


@app.get("/api/download/{filename}")
async def download(filename: str):
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.post("/api/rate")
async def rate_clip(req: RateRequest):
    if req.rating not in ("liked", "disliked"):
        raise HTTPException(400, "rating must be 'liked' or 'disliked'")
    save_rating(req.label, req.reason, req.rating)
    ratings = load_ratings()
    return {
        "status": "saved",
        "total_liked": len(ratings["liked"]),
        "total_disliked": len(ratings["disliked"])
    }


@app.get("/api/ratings")
async def get_ratings():
    ratings = load_ratings()
    return {
        "total_liked": len(ratings["liked"]),
        "total_disliked": len(ratings["disliked"]),
        "liked": ratings["liked"][-5:],
        "disliked": ratings["disliked"][-5:]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
