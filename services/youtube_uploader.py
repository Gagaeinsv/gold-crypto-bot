import os
import pickle
import logging
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger("youtube_uploader")

# Scope required for uploading
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

class YouTubeUploader:
    @staticmethod
    def get_authenticated_service():
        credentials = None
        # Paths to auth files (they should be in the root of the project where main.py runs)
        token_path = "token.pickle"
        
        if os.path.exists(token_path):
            try:
                with open(token_path, "rb") as token:
                    credentials = pickle.load(token)
            except Exception as e:
                logger.error(f"Failed to load token.pickle: {e}")
                
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                try:
                    logger.info("Refreshing expired YouTube access token...")
                    credentials.refresh(Request())
                    # Save refreshed credentials
                    with open(token_path, "wb") as token:
                        pickle.dump(credentials, token)
                except Exception as e:
                    logger.error(f"Failed to refresh YouTube token: {e}")
                    return None
            else:
                logger.error("No valid credentials found. Ensure token.pickle is present and valid.")
                return None
                
        return build("youtube", "v3", credentials=credentials)
        
    @staticmethod
    def upload_video(video_path: str, title: str, description: str, tags: list, privacy_status: str = "public") -> bool:
        """
        Uploads a video to YouTube.
        Returns True if successful, False otherwise.
        """
        try:
            logger.info(f"Starting YouTube upload for {video_path}...")
            
            if not os.path.exists(video_path):
                logger.error(f"Video file not found at {video_path}")
                return False
                
            youtube = YouTubeUploader.get_authenticated_service()
            if not youtube:
                logger.error("Could not get authenticated YouTube service.")
                return False
                
            body = {
                "snippet": {
                    "title": title,
                    "description": description,
                    "tags": tags,
                    "categoryId": "27"  # Education
                },
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": False
                }
            }
            
            media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
            
            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media
            )
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    logger.info(f"Upload progress: {int(status.progress() * 100)}%")
                    
            video_id = response.get('id')
            logger.info(f"Video successfully uploaded to YouTube! Video URL: https://youtube.com/shorts/{video_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to upload video to YouTube: {e}")
            return False
