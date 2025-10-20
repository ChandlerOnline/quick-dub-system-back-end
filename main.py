import os
import uuid
import requests
import subprocess
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Supabase
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


def generate_thumbnail(video_path: str, user_id: str, dubbing_id: str) -> str:
    """Extract frame from video and upload to Supabase Storage"""
    try:
        # Generate thumbnail filename
        thumbnail_filename = f"{user_id}/{dubbing_id}_thumb.jpg"
        temp_thumbnail = f"temp_{dubbing_id}_thumb.jpg"
        
        # Extract frame at 1 second using ffmpeg
        subprocess.run([
            'ffmpeg', '-i', video_path, '-ss', '00:00:01.000',
            '-vframes', '1', '-vf', 'scale=320:-1', temp_thumbnail, '-y'
        ], check=True, capture_output=True)
        
        # Upload to Supabase Storage
        with open(temp_thumbnail, 'rb') as f:
            supabase.storage.from_('thumbnails').upload(
                thumbnail_filename,
                f,
                file_options={"content-type": "image/jpeg"}
            )
        
        # Get public URL
        SUPABASE_URL = os.getenv("SUPABASE_URL")
        thumbnail_url = f"{SUPABASE_URL}/storage/v1/object/public/thumbnails/{thumbnail_filename}"
        
        # Clean up temp thumbnail
        if os.path.exists(temp_thumbnail):
            os.remove(temp_thumbnail)
        
        print(f"✅ Thumbnail generated: {thumbnail_url}")
        return thumbnail_url
    except Exception as e:
        print(f"⚠️ Thumbnail generation failed: {e}")
        return None


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

        # Save initial record in Supabase
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
        ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
        SUPABASE_URL = os.getenv("SUPABASE_URL")
        if not ELEVENLABS_API_KEY:
            raise HTTPException(status_code=500, detail="ELEVENLABS_API_KEY not set")
        if not SUPABASE_URL:
            raise HTTPException(status_code=500, detail="SUPABASE_URL not set")

        # Get video record from database
        video_resp = supabase.table("videos").select("*").eq("dubbing_id", dubbing_id).single().execute()
        video = video_resp.data
        if not video:
            raise HTTPException(status_code=404, detail="Dubbing ID not found")

        # If already complete with URL, return it
        if video.get("dubbed_url"):
            return video

        # Check ElevenLabs status
        response = requests.get(
            f"{ELEVEN_BASE_URL}/{dubbing_id}",
            headers={"xi-api-key": ELEVENLABS_API_KEY}
        )
        response.raise_for_status()
        elevenlabs_status = response.json().get("status")

        # If ElevenLabs says complete, download the dubbed file
        if elevenlabs_status == "dubbed":
            target_lang = video.get("target_language")
            if not target_lang:
                raise HTTPException(status_code=500, detail="Target language not found in video record")

            # ✅ Use correct ElevenLabs endpoint with language code
            download_url = f"{ELEVEN_BASE_URL}/{dubbing_id}/audio/{target_lang}"
            
            max_retries = 10
            retry_delay = 5  # seconds
            
            for attempt in range(max_retries):
                print(f"Attempt {attempt + 1}/{max_retries}: Downloading from {download_url}")
                
                video_response = requests.get(
                    download_url,
                    headers={"xi-api-key": ELEVENLABS_API_KEY},
                    stream=True
                )
                
                if video_response.status_code == 200:
                    print("✅ Successfully got dubbed video!")
                    break
                elif video_response.status_code == 425:
                    # 425 Too Early - file not ready yet
                    print(f"⏳ File not ready yet (425), waiting {retry_delay}s...")
                    import time
                    time.sleep(retry_delay)
                elif video_response.status_code == 404:
                    # 404 - might still be processing
                    print(f"⏳ File not found (404), waiting {retry_delay}s...")
                    import time
                    time.sleep(retry_delay)
                else:
                    # Other error
                    error_text = video_response.text
                    print(f"❌ Error {video_response.status_code}: {error_text}")
                    video_response.raise_for_status()
            else:
                # Max retries reached, file still not ready
                print("⚠️ Max retries reached, video still processing on ElevenLabs")
                return {"status": "processing", "message": "Video is being prepared by ElevenLabs"}

            # Save temporary file
            filename = f"dubbed_{dubbing_id}.mp4"
            print(f"💾 Saving video to {filename}")
            with open(filename, "wb") as f:
                for chunk in video_response.iter_content(8192):
                    f.write(chunk)

            # Generate thumbnail from the video
            user_id = video.get("user_id")
            thumbnail_url = generate_thumbnail(filename, user_id, dubbing_id)

            # Upload to Supabase storage
            print(f"☁️ Uploading to Supabase storage...")
            with open(filename, "rb") as f:
                supabase.storage.from_("dubbed_videos").upload(
                    filename,
                    f,
                    {"content-type": "video/mp4"}
                )

            # Public URL
            dubbed_url = f"{SUPABASE_URL}/storage/v1/object/public/dubbed_videos/{filename}"
            print(f"✅ Video available at: {dubbed_url}")

            # Update database with both video and thumbnail URLs
            update_data = {
                "status": "complete",
                "dubbed_url": dubbed_url
            }
            if thumbnail_url:
                update_data["thumbnail_url"] = thumbnail_url
            
            supabase.table("videos").update(update_data).eq("dubbing_id", dubbing_id).execute()

            # Clean up temp file
            if os.path.exists(filename):
                os.remove(filename)

            # Return fresh record with dubbed_url and thumbnail_url
            video_resp = supabase.table("videos").select("*").eq("dubbing_id", dubbing_id).single().execute()
            return video_resp.data

        # Still processing
        return {"status": elevenlabs_status or "processing"}

    except Exception as e:
        print(f"❌ Error in get_dub_status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/projects/{user_id}")
def get_user_projects(user_id: str):
    try:
        response = supabase.table("videos").select("*").eq("user_id", user_id).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
