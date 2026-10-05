# ✂️ ShortCut — AI-Powered YouTube Shorts Extractor for Indian Educators

ShortCut extracts the best short-form clips from any Indian educational YouTube video. Paste a link, pick your language, and get portrait-ready Shorts in minutes — in Hindi, Telugu, Tamil, Kannada, Malayalam, Bengali, Marathi, Gujarati, and Punjabi.

---

## Features

- 🤖 **AI Analysis** — Gemini reads the transcript and picks the most engaging, self-contained moments
- 🎵 **Audio Energy Detection** — ffmpeg identifies high-energy moments
- 📌 **Manual Timestamps** — specify your own start times
- 🌐 **9 Indian Languages** — Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, Marathi, Gujarati, Punjabi
- 🎯 **Face-Tracking Portrait Crop** — OpenCV tracks the educator's face with gimbal-smooth movement
- ⏱️ **Flexible Timing** — nudge clip start/end by ±1 second per click
- 👍 **Rating System** — rate clips; AI learns your preferences over time
- 📱 **Portrait Output** — 1080×1920 (9:16) ready for YouTube Shorts, Instagram Reels, TikTok

---

## Project Structure

```
shortcut/
├── backend/
│   ├── main.py           # FastAPI backend
│   └── requirements.txt  # Python dependencies
├── frontend/
│   └── index.html        # Web UI
├── Dockerfile            # Docker image definition
├── docker-compose.yml    # Docker Compose config
├── .dockerignore
└── .gitignore
```

---

## Prerequisites

- Python 3.11+
- ffmpeg
- Docker (for deployment)
- A Gemini API key — [aistudio.google.com/apikey](https://aistudio.google.com/apikey)

---

## Local Setup (Mac/Linux)

### 1. Clone the repo
```bash
git clone https://github.com/YOUR_USERNAME/shortcut.git
cd shortcut
```

### 2. Install dependencies
```bash
cd backend
pip install -r requirements.txt
```

### 3. Install ffmpeg
```bash
# Mac
brew install ffmpeg

# Ubuntu
sudo apt install ffmpeg
```

### 4. Set your Gemini API key
```bash
# Add permanently to shell
echo 'export GEMINI_API_KEY=AIza...' >> ~/.zshrc
source ~/.zshrc
```

### 5. Start the backend
```bash
cd backend
python main.py
```

### 6. Open the frontend
```bash
open frontend/index.html
```

---

## Docker Deployment (GCP / Any Cloud VM)

### 1. Set up your `.env` file
```bash
echo "GEMINI_API_KEY=AIza..." > .env
```

### 2. Export YouTube cookies (to avoid bot detection)
```bash
# Run on your local machine (must have Chrome + be logged into YouTube)
yt-dlp --cookies-from-browser chrome --cookies cookies.txt --skip-download "https://youtube.com"
```

### 3. Build and run
```bash
docker compose up -d --build
```

### 4. Check logs
```bash
docker compose logs -f
```

Server runs at `http://localhost:8000`

---

## GCP Deployment (Full Guide)

### Create VM
```bash
gcloud compute instances create shortcut-server \
  --zone=asia-south1-a \
  --machine-type=e2-medium \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --tags=http-server
```

### Open firewall
```bash
gcloud compute firewall-rules create allow-shortcut \
  --allow=tcp:8000,tcp:8080 \
  --target-tags=http-server
```

### Upload files
```bash
gcloud compute scp --recurse backend/ frontend/ Dockerfile docker-compose.yml .dockerignore \
  YOUR_USERNAME@shortcut-server:~/shortcut-app/ --zone=asia-south1-a
```

### SSH into VM and run
```bash
gcloud compute ssh YOUR_USERNAME@shortcut-server --zone=asia-south1-a
cd ~/shortcut-app
echo "GEMINI_API_KEY=AIza..." > .env
docker compose up -d --build
```

### Update frontend to point to VM
In `frontend/index.html`, change:
```javascript
const API = 'http://localhost:8000';
// to:
const API = 'http://YOUR_VM_IP:8000';
```

---

## Usage

1. Open `frontend/index.html` in your browser
2. Paste a YouTube video URL
3. Select the **video language** (or leave on Auto-detect)
4. Set the **duration** (target clip length in seconds)
5. Set the **number of shorts** to generate
6. Choose **detection methods** (AI Analysis recommended)
7. Click **Analyze Video** — wait ~20-40 seconds
8. Review suggested clips with virality scores and AI reasoning
9. Adjust timing with +/- buttons if needed
10. Rate clips 👍/👎 to train the AI
11. Click **Extract Clips** — wait for face-tracked video processing
12. Download your portrait-mode `.mp4` files!

---

## Known Issues & Tips

| Issue | Fix |
|-------|-----|
| YouTube bot detection (429/sign-in error) | Re-export cookies from Chrome; cloud IPs are more prone to this |
| `AI analysis failed 403` | Ensure Gemini API key is linked to a project with billing enabled |
| Very long clips (200s+) | Gemini sometimes ignores duration limits; the prompt enforces ±15s max |
| AV1 codec errors on VM | Fixed — yt-dlp is configured to avoid AV1 format |
| Face tracking is slow | Normal — processes every frame; a 30s clip takes ~30s on e2-medium |

---

## Refreshing YouTube Cookies

YouTube rotates cookies regularly. When you see "Sign in to confirm you're not a bot":

```bash
# On your local Mac (Chrome must be open and logged into YouTube)
yt-dlp --cookies-from-browser chrome --cookies /tmp/fresh-cookies.txt --skip-download "https://youtube.com"

# Upload to VM
gcloud compute scp /tmp/fresh-cookies.txt YOUR_USERNAME@shortcut-server:~/shortcut-app/cookies.txt --zone=asia-south1-a

# Restart
docker compose restart
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | FastAPI (Python 3.11) |
| AI Analysis | Google Gemini 2.5 Flash |
| Video Download | yt-dlp |
| Video Processing | ffmpeg |
| Face Tracking | OpenCV + EMA smoothing |
| Frontend | Vanilla HTML/CSS/JS |
| Deployment | Docker + GCP Compute Engine |

---

## Cost Estimate (GCP)

| Resource | Cost |
|----------|------|
| e2-medium VM (Mumbai, 24/7) | ~$25-30/month |
| e2-standard-2 (faster processing) | ~$48/month |
| 30GB disk | ~$1.50/month |
| Gemini API (light usage) | ~$0.50-2/month |

**Tip:** Stop the VM when not in use to save costs — static IP stays reserved.
