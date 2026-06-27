import os
import asyncio
import logging
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import edge_tts
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip, ImageClip

logger = logging.getLogger("video_engine")

class VideoEngine:
    @staticmethod
    def _create_text_overlay(text1: str, text2: str, text3: str, width: int = 1080, height: int = 1920) -> np.ndarray:
        # Create transparent background
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Try to load a good font, fallback to default
        try:
            # Common paths for Windows and Ubuntu
            font_paths = [
                "C:/Windows/Fonts/arialbd.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
            ]
            font_path = None
            for p in font_paths:
                if os.path.exists(p):
                    font_path = p
                    break
            
            if font_path:
                font1 = ImageFont.truetype(font_path, 80)
                font2 = ImageFont.truetype(font_path, 130)
                font3 = ImageFont.truetype(font_path, 60)
            else:
                font1 = font2 = font3 = ImageFont.load_default()
        except Exception:
            font1 = font2 = font3 = ImageFont.load_default()

        # Add a subtle dark background rectangle for text readability
        rect_height = 450
        rect_y = (height - rect_height) // 2
        draw.rectangle([0, rect_y, width, rect_y + rect_height], fill=(0, 0, 0, 180))
        
        # Helper to get text width
        def get_text_width(text, font):
            if hasattr(draw, 'textbbox'):
                bbox = draw.textbbox((0, 0), text, font=font)
                return bbox[2] - bbox[0]
            else:
                return font.getlength(text) if hasattr(font, 'getlength') else len(text)*20

        # Draw texts
        # Text 1: Asset and Profit
        color1 = (100, 255, 100, 255) # Light Green
        w1 = get_text_width(text1, font1)
        draw.text(((width - w1) / 2, rect_y + 50), text1, font=font1, fill=color1)
        
        # Text 2: PnL %
        w2 = get_text_width(text2, font2)
        draw.text(((width - w2) / 2, rect_y + 150), text2, font=font2, fill=(255, 255, 255, 255))
        
        # Text 3: Win rate
        w3 = get_text_width(text3, font3)
        draw.text(((width - w3) / 2, rect_y + 320), text3, font=font3, fill=(200, 200, 200, 255))
        
        return np.array(img)

    @staticmethod
    def _download_dynamic_background(asset: str, trade_id: str) -> str:
        import requests
        from config import Config
        
        pexels_key = getattr(Config, 'PEXELS_API_KEY', None)
        if not pexels_key:
            logger.warning("PEXELS_API_KEY is not set. Using fallback background.")
            return "templates/bg.mp4"
            
        asset_upper = asset.upper()
        if "XAU" in asset_upper or "GOLD" in asset_upper:
            query = "gold bars, gold bullion, gold trading"
        elif "BTC" in asset_upper or "BITCOIN" in asset_upper:
            query = "bitcoin, cryptocurrency"
        elif "ETH" in asset_upper or "ETHEREUM" in asset_upper:
            query = "ethereum crypto"
        else:
            query = "trading chart, finance, stock market"
            
        url = f"https://api.pexels.com/videos/search?query={query}&orientation=portrait&size=medium&per_page=15"
        headers = {"Authorization": pexels_key}
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                videos = data.get("videos", [])
                if videos:
                    import random
                    video = random.choice(videos)
                    video_files = video.get("video_files", [])
                    # Find a decent quality vertical video
                    hd_file = next((f for f in video_files if f.get("quality") == "hd" and f.get("height", 0) > f.get("width", 0)), None)
                    if not hd_file and video_files:
                        hd_file = video_files[0]
                        
                    if hd_file:
                        link = hd_file.get("link")
                        output_path = f"storage/renders/temp_bg_{trade_id}.mp4"
                        logger.info(f"Downloading dynamic Pexels video for {asset}...")
                        vid_resp = requests.get(link, stream=True, timeout=30)
                        with open(output_path, 'wb') as f:
                            for chunk in vid_resp.iter_content(chunk_size=8192):
                                f.write(chunk)
                        return output_path
                        
            logger.warning(f"No Pexels video found for query '{query}'. Using fallback background.")
        except Exception as e:
            logger.error(f"Failed to fetch Pexels video: {e}")
            
        return "templates/bg.mp4"

    @staticmethod
    async def _generate_tts(text: str, output_path: str):
        # en-US-ChristopherNeural is a great male voice
        communicate = edge_tts.Communicate(text, "en-US-ChristopherNeural")
        await communicate.save(output_path)

    @staticmethod
    def generate_shorts(trade_data: dict, metrics: dict) -> str | None:
        """
        Generates a 15-second vertical video for YouTube Shorts.
        This is a synchronous method (runs in its own thread in main.py)
        """
        try:
            trade_id = trade_data.get('id', 'unknown')
            logger.info(f"Starting video generation for trade #{trade_id}...")
            
            asset = trade_data.get('asset', 'UNKNOWN')
            direction = trade_data.get('direction', 'BUY')
            pnl = float(trade_data.get('pnl_percentage', 0.0))
            win_rate = float(metrics.get('win_rate', 0.0))
            
            # Prepare Texts
            text1 = f"${asset} PROFIT"
            text2 = f"+{pnl:.2f}% 🚀"
            text3 = f"Win Rate: {win_rate:.1f}%"
            
            tts_text = (f"Trade update. Our bot just closed a {direction} position on "
                        f"{asset} with a profit of {pnl:.2f} percent. "
                        f"Current algorithm win rate is {win_rate:.1f} percent. "
                        f"Join our Telegram for free signals.")
            
            # File paths
            audio_path = f"storage/renders/temp_audio_{trade_id}.mp3"
            output_path = f"storage/renders/trade_{trade_id}.mp4"
            
            # Fetch Dynamic Background from Pexels based on asset
            bg_path = VideoEngine._download_dynamic_background(asset, str(trade_id))
            
            if not os.path.exists(bg_path):
                logger.error(f"Background template {bg_path} not found! Cannot render video.")
                return None
                
            # Generate Audio (need to run asyncio loop because we are in a sync thread)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(VideoEngine._generate_tts(tts_text, audio_path))
            loop.close()
            
            # Create Text Overlay Clip
            overlay_array = VideoEngine._create_text_overlay(text1, text2, text3, width=1080, height=1920)
            overlay_clip = ImageClip(overlay_array).set_position('center')
            
            # Process Video (Ensure 1080x1920 for Shorts format)
            video_clip = VideoFileClip(bg_path).resize(newsize=(1080, 1920))
            audio_clip = AudioFileClip(audio_path)
            
            # Set duration based on audio + a small tail, up to 15 seconds
            duration = min(audio_clip.duration + 0.5, 15.0)
            
            if video_clip.duration < duration:
                logger.warning(f"Background video ({video_clip.duration}s) is shorter than required audio ({duration}s). Result will be truncated.")
                duration = video_clip.duration
                
            video_clip = video_clip.subclip(0, duration)
            overlay_clip = overlay_clip.set_duration(duration)
            
            # Combine video and text
            final_video = CompositeVideoClip([video_clip, overlay_clip])
            
            # Set audio
            final_audio = audio_clip.subclip(0, duration)
            final_video = final_video.set_audio(final_audio)
            
            # Render
            logger.info(f"Rendering video {output_path} (Duration: {duration:.1f}s)...")
            final_video.write_videofile(
                output_path,
                fps=30,
                codec="libx264",
                audio_codec="aac",
                threads=4,
                logger=None  # Disable terminal bar
            )
            
            # Cleanup temp audio, close clips to free memory
            video_clip.close()
            audio_clip.close()
            final_video.close()
            
            if os.path.exists(audio_path):
                os.remove(audio_path)
                
            # Cleanup temp Pexels video
            if bg_path != "templates/bg.mp4" and os.path.exists(bg_path):
                try:
                    os.remove(bg_path)
                except Exception as e:
                    logger.error(f"Failed to remove temp bg video: {e}")
                
            logger.info(f"Video successfully generated: {output_path}")
            
            # --- YOUTUBE UPLOAD INTEGRATION ---
            try:
                from services.youtube_uploader import YouTubeUploader
                
                yt_title = f"Trading Bot Closed a ${asset} {direction} Position with +{pnl:.1f}% Profit! 🚀 #shorts"
                yt_desc = (
                    f"Our fully automated AI trading bot just secured another profit on {asset}!\n\n"
                    f"✅ Direction: {direction}\n"
                    f"💰 Profit: +{pnl:.2f}%\n"
                    f"📈 Current Win Rate: {win_rate:.1f}%\n\n"
                    "Don't miss the next signal! Join our Telegram Bot for FREE real-time trading signals.\n\n"
                    "#crypto #trading #bitcoin #ethereum #investing #tradingbot #signals"
                )
                yt_tags = ["crypto", "trading", "bot", "signals", "bitcoin", "ethereum", "xauusd", "tradingbot"]
                
                logger.info("Initiating automatic YouTube upload...")
                success = YouTubeUploader.upload_video(
                    video_path=output_path,
                    title=yt_title,
                    description=yt_desc,
                    tags=yt_tags,
                    privacy_status="public" # Immediately public
                )
                
                if success:
                    # Optionally remove the local mp4 file after upload to save space
                    # os.remove(output_path)
                    pass
            except Exception as upload_err:
                logger.error(f"YouTube upload step failed: {upload_err}")
            
            return output_path
            
        except Exception as e:
            logger.error(f"Error generating video: {e}")
            return None
