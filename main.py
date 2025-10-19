import os
import uuid
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from elevenlabs.client import ElevenLabs

load_dotenv()

app = FastAPI()

# ✅ Enable CORS for Lovable Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://your-lovable-app-domain.com",  # ⬅️ Replace with your actual Lovable app URL
        "http://localhost:3000",  # Optional: useful for local testing
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ Initialize ElevenLabs + Supabase
client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
)

@app.post("/dub")
async def create_dub(
    file: UploadFile,
    language: str = Form(...),
    project_name: str = Form("Untitled project"),
    user_id: str = Form(...),
    source_lang: str = Form("auto"),
    target_lang: str = Form(...),
    num_speakers: str = Form("Detect"),
    start_time: str = Form(None),
    end_time: str = Form(None),
    disable_voice_cloning: bool = Form(False),
):
    try:
        # Save uploaded file temporarily
        temp_path = f"temp_{uuid.uuid4()}.mp4"
        with open(temp_path, "wb") as f:
            f.write(await file.read())

        # Prepare kwargs for ElevenLabs API
        dub_kwargs = {
            "file": (file.filename, open(temp_path, "rb"), file.content_type),
            "target_lang": target_lang,
            "watermark": False,
        }

        if start_time and end_time:
            dub_kwargs["start_time"] = start_time
            dub_kwargs["end_time"] = end_time
        if num_speakers.lower() != "detect":
            dub_kwargs["num_speakers"] = int(num_speakers)
        if disable_voice_cloning:
            dub_kwargs["disable_voice_cloning"] = True

        # Create dubbing job
        dub = client.dubbing.dub_a_video(**dub_kwargs)
        dubbing_id = dub.id

        # Store in Lovable Cloud videos table (not projects)
        supabase.table("videos").insert({
            "user_id": user_id,
            "title": project_name,
            "target_language": target_lang,
            "source_language": source_lang,
            "status": "processing"
        }).execute()

        os.remove(temp_path)
        return {"dubbing_id": dubbing_id, "status": "processing"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/status/{dubbing_id}")
def get_dub_status(dubbing_id: str):
    try:
        meta = client.dubbing.get(dubbing_id)
        return {
            "status": meta.status,
            "progress": getattr(meta, "progress", None),
            "target_lang": getattr(meta, "target_lang", None)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/output/{dubbing_id}")
def get_dub_output(dubbing_id: str, user_id: str):
    try:
        # Get dubbed video from ElevenLabs
        base = "https://api.elevenlabs.io/v1/dubbing"
        url = f"{base}/{dubbing_id}/output"
        headers = {"xi-api-key": os.getenv("ELEVENLABS_API_KEY")}
        resp = requests.get(url, headers=headers, stream=True)

        if resp.status_code != 200:
            return JSONResponse({"error": "Dub not ready"}, status_code=404)

        output_filename = f"dubbed_{dubbing_id}.mp4"
        with open(output_filename, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)

        # Upload to Lovable Cloud Storage
        with open(output_filename, "rb") as f:
            supabase.storage.from_("dubbed_videos").upload(
                output_filename, 
                f,
                {"content-type": "video/mp4"}
            )

        video_url = f"https://rhkooynsxrrwjtnokeld.supabase.co/storage/v1/object/public/dubbed_videos/{output_filename}"

        # Update videos table
        supabase.table("videos").update({
            "status": "complete",
            "dubbed_url": video_url
        }).eq("user_id", user_id).eq("status", "processing").execute()

        os.remove(output_filename)
        return {"video_url": video_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/projects/{user_id}")
def get_user_projects(user_id: str):
    try:
        response = supabase.table("projects").select("*").eq("user_id", user_id).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

