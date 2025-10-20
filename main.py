import os
import uuid
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

# ✅ Enable CORS for Lovable Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or restrict to your Lovable frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ Initialize Supabase
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
)

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_BASE_URL = "https://api.elevenlabs.io/v1/dubbing"


def save_temp_file(file: UploadFile) -> str:
    path = f"temp_{uuid.uuid4()}.mp4"
    with open(path, "wb") as f:
        f.write(file.file.read())
    return path


@app.post("/dub")
async def create_dub(
    file: UploadFile,
    user_id: str = Form(...),
    project_name: str = Form("Untitled project"),
    source_lang: str = Form("auto"),
    target_lang: str = Form(...),
    num_speakers: str = Form("Detect"),
    start_time: str = Form(None),
    end_time: str = Form(None),
    disable_voice_cloning: bool = Form(False),
):
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")

    temp_path = save_temp_file(file)

    try:
        files = {"file": (file.filename, open(temp_path, "rb"), file.content_type)}
        data = {
            "target_lang": target_lang,
            "source_lang": source_lang,
            "watermark": "false",
        }
        if start_time and end_time:
            data["start_time"] = start_time
            data["end_time"] = end_time
        if num_speakers.lower() != "detect":
            data["num_speakers"] = num_speakers
        if disable_voice_cloning:
            data["disable_voice_cloning"] = "true"

        headers = {"xi-api-key": ELEVEN_API_KEY}
        resp = requests.post(ELEVEN_BASE_URL, headers=headers, files=files, data=data)
        resp.raise_for_status()

        dubbing_data = resp.json()
        dubbing_id = dubbing_data.get("dubbing_id")

        # ✅ Save initial record in Supabase
        supabase.table("videos").insert({
            "user_id": user_id,
            "title": project_name,
            "target_language": target_lang,
            "source_language": source_lang,
            "status": "processing",
            "dubbing_id": dubbing_id
        }).execute()

        return {"dubbing_id": dubbing_id, "status": "processing"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.get("/status/{dubbing_id}")
def get_dub_status(dubbing_id: str):
    try:
        # ✅ Get current video status from Supabase
        response = supabase.table("videos").select("*").eq("dubbing_id", dubbing_id).single().execute()
        video = response.data
        if not video:
            raise HTTPException(status_code=404, detail="Dubbing ID not found")

        # ✅ If still processing, check ElevenLabs API
        if video["status"] == "processing":
            headers = {"xi-api-key": ELEVEN_API_KEY}
            resp = requests.get(f"{ELEVEN_BASE_URL}/{dubbing_id}", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "complete":
                # Update Supabase
                supabase.table("videos").update({"status": "complete"}).eq("dubbing_id", dubbing_id).execute()
                video["status"] = "complete"

        return video

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/output/{dubbing_id}")
def get_dub_output(dubbing_id: str, user_id: str):
    try:
        headers = {"xi-api-key": ELEVEN_API_KEY}
        url = f"{ELEVEN_BASE_URL}/{dubbing_id}/output"
        resp = requests.get(url, headers=headers, stream=True)
        resp.raise_for_status()

        output_filename = f"dubbed_{dubbing_id}.mp4"
        with open(output_filename, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)

        # ✅ Upload to Supabase Storage
        with open(output_filename, "rb") as f:
            supabase.storage.from_("dubbed_videos").upload(output_filename, f, {"content-type": "video/mp4"})

        video_url = f"{os.getenv('SUPABASE_URL')}/storage/v1/object/public/dubbed_videos/{output_filename}"

        # ✅ Update Supabase record
        supabase.table("videos").update({
            "status": "complete",
            "dubbed_url": video_url
        }).eq("user_id", user_id).eq("dubbing_id", dubbing_id).execute()

        return {"video_url": video_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(output_filename):
            os.remove(output_filename)


@app.get("/projects/{user_id}")
def get_user_projects(user_id: str):
    try:
        response = supabase.table("videos").select("*").eq("user_id", user_id).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

