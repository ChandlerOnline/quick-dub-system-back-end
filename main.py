import os
import uuid
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from supabase import create_client, Client
from elevenlabs.client import ElevenLabs

load_dotenv()

app = FastAPI()

# Initialize ElevenLabs + Supabase
client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
supabase: Client = create_client(
    os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
)

@app.post("/dub")
async def create_dub(
    file: UploadFile,
    language: str = Form(...),
    project_name: str = Form("Untitled"),
    user_id: str = Form(...)
):
    try:
        # Save the uploaded file temporarily
        temp_path = f"temp_{uuid.uuid4()}.mp4"
        with open(temp_path, "wb") as f:
            f.write(await file.read())

        # Create the dubbing job in ElevenLabs
        with open(temp_path, "rb") as f:
            dub = client.dubbing.dub_a_video(
                file=(file.filename, f, file.content_type),
                target_lang=language,
                watermark=False,
            )

        dubbing_id = dub.id

        # Store project metadata in Supabase
        supabase.table("projects").insert({
            "user_id": user_id,
            "project_name": project_name,
            "dubbing_id": dubbing_id,
            "language": language,
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

        # Upload to Supabase Storage
        supabase.storage.from_("dubbed_videos").upload(
            output_filename, output_filename, {"content-type": "video/mp4"}
        )

        video_url = (
            f"{os.getenv('SUPABASE_URL')}/storage/v1/object/public/dubbed_videos/{output_filename}"
        )

        # Update project record
        supabase.table("projects").update({
            "status": "complete",
            "video_url": video_url
        }).eq("dubbing_id", dubbing_id).execute()

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

