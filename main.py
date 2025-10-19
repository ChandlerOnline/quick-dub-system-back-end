import os
import time
import requests
import subprocess
from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()
client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

app = FastAPI()

@app.get("/")
def root():
    return {"message": "QuickDub backend is live 🚀"}

@app.post("/dub")
async def dub_video(file: UploadFile, language: str = Form(...)):
    try:
        # Save uploaded file
        input_path = f"temp_{file.filename}"
        with open(input_path, "wb") as f:
            f.write(await file.read())

        # ✅ Start dubbing job using ElevenLabs (new parameter format)
        with open(input_path, "rb") as f:
            dubbing_job = client.dubbing.create(
                csv_file=None,
                foreground_audio_file=None,
                background_audio_file=None,
                file=f,
                target_lang=language,
                mode="automatic"
            )

        dubbing_id = dubbing_job.dubbing_id

       # ⏳ Poll job until done
        while True:
            meta = client.dubbing.get(dubbing_id)
            if meta.status in ("dubbed", "complete"):
                break
            elif meta.status in ("failed", "error"):
                return JSONResponse({"error": "Dubbing failed"}, status_code=500)
            time.sleep(5)

        # 🎧 Fetch dubbed audio
        base = "https://api.elevenlabs.io/v1/dubbing"
        audio_url = f"{base}/{dubbing_id}/audio/{language}"
        headers = {"xi-api-key": os.getenv("ELEVENLABS_API_KEY")}

        # Retry fetching up to 5 times (sometimes audio isn’t ready right away)
        for attempt in range(5):
            resp = requests.get(audio_url, headers=headers, stream=True)
            if resp.status_code == 200:
                break
            time.sleep(3)

        if resp.status_code != 200:
            # Try alternative output URL if audio endpoint not ready
            output_url = f"{base}/{dubbing_id}/output"
            resp = requests.get(output_url, headers=headers, stream=True)
            if resp.status_code != 200:
                return JSONResponse({"error": f"Failed to fetch dubbed audio ({resp.status_code})"}, status_code=500)

        # 🎧 Fetch dubbed audio
        base = "https://api.elevenlabs.io/v1/dubbing"
        audio_url = f"{base}/{dubbing_id}/audio/{language}"
        headers = {"xi-api-key": os.getenv("ELEVENLABS_API_KEY")}
        resp = requests.get(audio_url, headers=headers, stream=True)

        if resp.status_code != 200:
            return JSONResponse({"error": "Failed to fetch dubbed audio"}, status_code=500)

        dubbed_audio_path = "dubbed_audio.mp3"
        with open(dubbed_audio_path, "wb") as outf:
            for chunk in resp.iter_content(chunk_size=8192):
                outf.write(chunk)

        # 🎬 Combine dubbed audio + original video
        output_path = f"dubbed_{file.filename}"
        subprocess.run([
            "ffmpeg",
            "-i", input_path,
            "-i", dubbed_audio_path,
            "-c:v", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            output_path,
            "-y"
        ], check=True)

        return FileResponse(output_path, media_type="video/mp4", filename=output_path)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
