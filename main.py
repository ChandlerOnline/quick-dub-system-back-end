import os
import time
import requests
import subprocess
import tempfile
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

# Load environment variables
load_dotenv()

# Initialize ElevenLabs client
client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

app = FastAPI()

# Allow frontend access (Lovable, localhost, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # you can restrict this later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Utility: Get video duration (for token charging) ---
def get_video_duration(file_path: str) -> float:
    """Return video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0

# --- Endpoint 1: Analyze video length for token pricing ---
@app.post("/analyze_video")
async def analyze_video(file: UploadFile):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        duration_seconds = get_video_duration(tmp_path)
        duration_minutes = duration_seconds / 60
        token_cost = max(1, round(duration_minutes * 2))  # 2 tokens/minute

        return JSONResponse({
            "duration_seconds": duration_seconds,
            "duration_minutes": round(duration_minutes, 2),
            "tokens_cost": token_cost
        })

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Endpoint 2: Dubbing ---
@app.post("/dub")
async def dub_video(file: UploadFile, language: str = Form(...)):
    try:
        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(await file.read())
            input_path = tmp.name

        # Start dubbing job
        with open(input_path, "rb") as f:
            dubbing_job = client.dubbing.create(
                file=f,
                target_lang=language,
                mode="automatic"
            )

        dubbing_id = dubbing_job.dubbing_id

        # Poll until job finishes
        while True:
            meta = client.dubbing.get(dubbing_id)
            if meta.status == "dubbed":
                break
            elif meta.status in ("failed", "error"):
                return JSONResponse({"error": "Dubbing failed"}, status_code=500)
            time.sleep(5)

        # Fetch dubbed audio
        base = "https://api.elevenlabs.io/v1/dubbing"
        audio_url = f"{base}/{dubbing_id}/audio/{language}"
        headers = {"xi-api-key": os.getenv("ELEVENLABS_API_KEY")}
        resp = requests.get(audio_url, headers=headers, stream=True)

        if resp.status_code != 200:
            return JSONResponse({"error": "Failed to fetch dubbed audio"}, status_code=500)

        dubbed_audio_path = tempfile.mktemp(suffix=".mp3")
        with open(dubbed_audio_path, "wb") as outf:
            for chunk in resp.iter_content(chunk_size=8192):
                outf.write(chunk)

        # Combine audio + video using FFmpeg
        output_path = tempfile.mktemp(suffix=".mp4")
        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-i", dubbed_audio_path,
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            output_path,
            "-y"
        ]
        subprocess.run(cmd, check=True)

        return FileResponse(output_path, media_type="video/mp4", filename="dubbed_video.mp4")

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

