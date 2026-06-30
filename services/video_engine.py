import os
import asyncio
import logging
import numpy as np
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.Resampling.LANCZOS

from PIL import Image, ImageDraw, ImageFont
import edge_tts
from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip, ImageClip

logger = logging.getLogger("video_engine")

class VideoEngine:
    @staticmethod
    def _create_text_overlay(trade_data: dict, free_metrics: dict, vip_metrics: dict, width: int = 1080, height: int = 1920):
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Fonts
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
                
        try:
            if font_path:
                f_title = ImageFont.truetype(font_path, 60)
                f_asset = ImageFont.truetype(font_path, 90)
                f_prices = ImageFont.truetype(font_path, 55)
                f_pnl = ImageFont.truetype(font_path, 180)
                f_stats = ImageFont.truetype(font_path, 45)  # slightly smaller for 2 lines
                f_stats_vip = ImageFont.truetype(font_path, 50)
            else:
                f_title = f_asset = f_prices = f_pnl = f_stats = f_stats_vip = ImageFont.load_default()
        except Exception:
            f_title = f_asset = f_prices = f_pnl = f_stats = f_stats_vip = ImageFont.load_default()

        # Helper
        def draw_centered_text(y, text, font, fill):
            bbox = draw.textbbox((0, 0), text, font=font)
            w = bbox[2] - bbox[0]
            draw.text(((width - w) / 2, y), text, font=font, fill=fill)
            return bbox[3] - bbox[1] # return height

        # Draw a sleek semi-transparent card background
        card_y1 = height // 2 - 400
        card_y2 = height // 2 + 400
        margin = 80
        # Main card
        draw.rounded_rectangle([margin, card_y1, width - margin, card_y2], radius=40, fill=(15, 15, 20, 220), outline=(50, 50, 70, 255), width=4)
        
        # 1. Header
        draw_centered_text(card_y1 + 40, "🚀 AI SIGNAL CLOSED", f_title, (200, 200, 200, 255))
        
        # 2. Asset & Direction
        asset = trade_data.get("asset", "UNKNOWN")
        direction = trade_data.get("direction", "BUY")
        dir_color = (80, 255, 100, 255) if direction == "BUY" else (255, 80, 80, 255)
        dir_icon = "🟢" if direction == "BUY" else "🔴"
        draw_centered_text(card_y1 + 130, f"{dir_icon} {asset} {direction}", f_asset, dir_color)
        
        # 3. Prices
        entry = trade_data.get("entry_price", 0)
        exit_p = trade_data.get("exit_price", 0)
        
        def format_price(p):
            if p is None: return "N/A"
            return f"${p:.5f}" if p < 1 else f"${p:.2f}"
            
        prices_text = f"Entry: {format_price(entry)}  ➡️  Exit: {format_price(exit_p)}"
        draw_centered_text(card_y1 + 250, prices_text, f_prices, (255, 255, 255, 255))
        
        # 4. Giant PnL
        pnl = trade_data.get("pnl_percentage", 0.0)
        pnl_text = f"+{pnl:.2f}%"
        # Drop shadow for PnL
        bbox = draw.textbbox((0, 0), pnl_text, font=f_pnl)
        w = bbox[2] - bbox[0]
        x_pnl = (width - w) / 2
        y_pnl = card_y1 + 350
        draw.text((x_pnl + 8, y_pnl + 8), pnl_text, font=f_pnl, fill=(0, 0, 0, 255)) # Shadow
        draw.text((x_pnl, y_pnl), pnl_text, font=f_pnl, fill=(50, 255, 50, 255)) # Glowing green
        
        draw_centered_text(card_y1 + 550, "NET PROFIT", f_title, (200, 200, 200, 255))
        
        # 5. Stats line (FOMO Marketing logic)
        free_weekly = free_metrics.get("cumulative_weekly_pnl", 0.0)
        vip_weekly = vip_metrics.get("cumulative_weekly_pnl", 0.0)
        vip_win_rate = vip_metrics.get("win_rate", 0.0)
        
        stats_y = card_y1 + 650
        
        if vip_weekly > free_weekly and vip_weekly > 0 and free_weekly > 0:
            # Draw Two lines
            draw_centered_text(stats_y, f"📢 Free Channel PnL: +{free_weekly:.1f}%", f_stats, (180, 180, 180, 255))
            draw_centered_text(stats_y + 60, f"💎 VIP Premium PnL: +{vip_weekly:.1f}%", f_stats_vip, (255, 215, 0, 255)) # Gold color
        else:
            # Draw standard single line VIP stats
            stats_parts = []
            if vip_win_rate >= 50:
                stats_parts.append(f"VIP Win Rate: {vip_win_rate:.1f}%")
            if vip_weekly > 0:
                stats_parts.append(f"VIP Weekly PnL: +{vip_weekly:.1f}%")
                
            if stats_parts:
                stats_text = "   |   ".join(stats_parts)
                draw_centered_text(stats_y + 30, stats_text, f_stats_vip, (255, 215, 0, 255))

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
    def _generate_image_replicate(image_prompt: str, trade_id: int) -> str | None:
        """Generate a unique AI image via Replicate FLUX Schnell and return local path."""
        from config import Config
        import httpx
        import time

        if not Config.REPLICATE_API_TOKEN:
            return None

        try:
            headers = {
                "Authorization": f"Bearer {Config.REPLICATE_API_TOKEN}",
                "Content-Type": "application/json",
                "Prefer": "wait"
            }
            payload = {
                "input": {
                    "prompt": image_prompt,
                    "aspect_ratio": "9:16",
                    "num_outputs": 1,
                    "output_format": "webp",
                    "num_inference_steps": 4
                }
            }
            logger.info(f"Generating AI background image via Replicate FLUX...")
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                    headers=headers,
                    json=payload
                )
                resp.raise_for_status()
                data = resp.json()

                # Poll if not done yet
                poll_url = data.get("urls", {}).get("get", "")
                for _ in range(30):
                    if data.get("status") in ("succeeded", "failed", "canceled"):
                        break
                    if poll_url:
                        time.sleep(2)
                        poll_resp = client.get(poll_url, headers=headers)
                        data = poll_resp.json()

                output = data.get("output")
                if output:
                    image_url = output[0] if isinstance(output, list) else output
                    # Download image
                    img_path = f"storage/renders/temp_bg_{trade_id}.webp"
                    img_resp = client.get(image_url, timeout=30)
                    with open(img_path, 'wb') as f:
                        f.write(img_resp.content)
                    logger.info(f"AI background image saved: {img_path}")
                    return img_path
        except Exception as e:
            logger.error(f"Replicate image generation failed: {e}")
        return None

    @staticmethod
    def _generate_ai_script(trade_data: dict, free_weekly: float, vip_weekly: float, vip_win_rate: float) -> tuple[str | None, list[str]]:
        """Returns (tts_script, [image_prompt1, image_prompt2, image_prompt3])."""
        from config import Config
        import httpx

        direction = trade_data.get("direction", "BUY")
        asset = trade_data.get("asset", "UNKNOWN")
        pnl = trade_data.get("pnl_percentage", 0.0)

        # Fallback image prompts if Groq fails
        fallback_prompts = [
            f"Dramatic cinematic crypto trading, {asset} coin exploding upward, neon green chart, dark city background, 9:16 vertical",
            f"Epic profit dashboard, glowing green numbers, trader celebrating, {asset} logo, dark room with screens, 9:16 vertical",
            "Luxury lifestyle, sports car, yacht, VIP trader success, golden light, cinematic 9:16 vertical"
        ]

        if not Config.GROQ_API_KEY:
            return None, fallback_prompts

        prompt = f"""You are a brilliant financial copywriter for a crypto trading YouTube Shorts channel.
Your output MUST follow this EXACT format with these 4 separators:

--- SCRIPT ---
(60 words max. Exciting, FOMO-creating spoken script. No stage directions.)

--- IMAGE 1 ---
(Scene 1: HOOK image. Dramatic market chaos, fear/excitement. One sentence for AI image generator.)

--- IMAGE 2 ---
(Scene 2: PROFIT image. Celebration, green charts, profit numbers. One sentence for AI image generator.)

--- IMAGE 3 ---
(Scene 3: VIP/CTA image. Luxury lifestyle, success, premium feel. One sentence for AI image generator.)

All image prompts must be: cinematic, dramatic, 9:16 vertical, photorealistic or stylized 3D.

Trade context:
- Asset: {asset}, Direction: {direction}, Profit: +{pnl:.1f}%
- VIP win rate: {vip_win_rate:.1f}%
"""
        if vip_weekly > free_weekly and vip_weekly > 0 and free_weekly > 0:
            prompt += f"- Free signals: +{free_weekly:.1f}% this week, VIP signals: +{vip_weekly:.1f}% this week\n"
        elif vip_weekly > 0:
            prompt += f"- VIP members made +{vip_weekly:.1f}% profit this week\n"

        try:
            headers = {
                "Authorization": f"Bearer {Config.GROQ_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.75,
                "max_tokens": 400
            }
            with httpx.Client(timeout=15.0) as client:
                response = client.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
                if response.status_code != 200:
                    logger.error(f"Groq API Error {response.status_code}: {response.text}")
                response.raise_for_status()
                raw = response.json()["choices"][0]["message"]["content"].strip()
                raw = raw.replace("**", "")

                # Parse sections
                def extract(tag, text):
                    if f"--- {tag} ---" in text:
                        start = text.index(f"--- {tag} ---") + len(f"--- {tag} ---")
                        # Find next separator
                        next_sep = len(text)
                        for t in ["--- IMAGE 1 ---", "--- IMAGE 2 ---", "--- IMAGE 3 ---", "--- SCRIPT ---"]:
                            if t != f"--- {tag} ---" and t in text[start:]:
                                idx = text.index(t, start)
                                if idx < next_sep:
                                    next_sep = idx
                        return text[start:next_sep].strip()
                    return ""

                script = extract("SCRIPT", raw)
                img1 = extract("IMAGE 1", raw) or fallback_prompts[0]
                img2 = extract("IMAGE 2", raw) or fallback_prompts[1]
                img3 = extract("IMAGE 3", raw) or fallback_prompts[2]

                if len(script) > 20:
                    logger.info(f"AI script generated. Image prompts: [{img1[:40]}...]")
                    return script, [img1, img2, img3]
        except Exception as e:
            logger.error(f"Groq AI Script generation failed: {e}")
        return None, fallback_prompts

    @staticmethod
    def _generate_images_replicate(image_prompts: list[str], trade_id: int) -> list[str]:
        """Generate 3 AI images via Replicate FLUX Schnell in parallel. Returns list of local paths."""
        from config import Config
        import httpx
        import time

        if not Config.REPLICATE_API_TOKEN:
            return []

        headers = {
            "Authorization": f"Bearer {Config.REPLICATE_API_TOKEN}",
            "Content-Type": "application/json"
        }
        image_paths = []

        with httpx.Client(timeout=90.0) as client:
            # Submit all 3 predictions at once
            prediction_ids = []
            for i, prompt_text in enumerate(image_prompts):
                try:
                    payload = {
                        "input": {
                            "prompt": prompt_text,
                            "aspect_ratio": "9:16",
                            "num_outputs": 1,
                            "output_format": "webp",
                            "num_inference_steps": 4
                        }
                    }
                    resp = client.post(
                        "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                        headers=headers, json=payload
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    prediction_ids.append((i, data.get("id"), data.get("urls", {}).get("get", "")))
                    logger.info(f"Submitted image {i+1}/3 to Replicate (ID: {data.get('id')})")
                except Exception as e:
                    logger.error(f"Failed to submit image {i+1}: {e}")
                    prediction_ids.append((i, None, None))

            # Poll all predictions until done
            results = {}
            for attempt in range(40):
                all_done = True
                for (i, pred_id, poll_url) in prediction_ids:
                    if i in results or not poll_url:
                        continue
                    try:
                        r = client.get(poll_url, headers=headers)
                        data = r.json()
                        status = data.get("status")
                        if status == "succeeded":
                            output = data.get("output")
                            if output:
                                url = output[0] if isinstance(output, list) else output
                                results[i] = url
                                logger.info(f"Image {i+1}/3 ready!")
                        elif status in ("failed", "canceled"):
                            logger.error(f"Image {i+1} failed on Replicate")
                            results[i] = None
                        else:
                            all_done = False
                    except Exception as e:
                        logger.error(f"Poll error for image {i+1}: {e}")
                        all_done = False
                if all_done or len(results) == len(prediction_ids):
                    break
                time.sleep(2)

            # Download all images
            for i in range(len(image_prompts)):
                url = results.get(i)
                if url:
                    try:
                        img_path = f"storage/renders/temp_bg_{trade_id}_{i+1}.webp"
                        img_resp = client.get(url, timeout=30)
                        with open(img_path, 'wb') as f:
                            f.write(img_resp.content)
                        image_paths.append(img_path)
                        logger.info(f"Saved image {i+1}: {img_path}")
                    except Exception as e:
                        logger.error(f"Failed to download image {i+1}: {e}")

        return image_paths

    @staticmethod
    def generate_shorts(trade_data: dict, free_metrics: dict, vip_metrics: dict) -> str | None:
        """
        Generates a premium vertical video (YouTube Shorts format) from a trade result.
        """
        try:
            trade_id = trade_data.get("id")
            asset = trade_data.get("asset")
            direction = trade_data.get("direction")
            pnl = trade_data.get("pnl_percentage", 0.0)
            entry = trade_data.get("entry_price")
            
            # Extract both stats
            free_weekly = free_metrics.get("cumulative_weekly_pnl", 0.0)
            vip_weekly = vip_metrics.get("cumulative_weekly_pnl", 0.0)
            vip_win_rate = vip_metrics.get("win_rate", 0.0)
            
            # 1. AI Copywriter: script + 3 image prompts
            tts_text, image_prompts = VideoEngine._generate_ai_script(trade_data, free_weekly, vip_weekly, vip_win_rate)

            if not tts_text:
                logger.info("Falling back to standard script template.")
                if vip_weekly > free_weekly and vip_weekly > 0 and free_weekly > 0:
                    fomo_text = f"Our free signals secured {free_weekly:.1f} percent, but our VIP premium algorithm soared to {vip_weekly:.1f} percent this week! "
                elif vip_weekly > 0:
                    fomo_text = f"Our VIP premium algorithm is soaring with {vip_weekly:.1f} percent profit this week! "
                else:
                    fomo_text = "The premium algorithm is dominating the market right now. "
                    
                if vip_win_rate >= 50:
                    win_text = f"Win rate is holding strong at {vip_win_rate:.1f} percent. "
                else:
                    win_text = "We are finding the absolute best entries. "
                    
                tts_text = (f"Boom! Another successful trade! Our AI bot just nailed a {direction} position "
                            f"on {asset}, securing a massive {pnl:.1f} percent profit! "
                            f"{fomo_text}{win_text}"
                            f"Stop guessing and let the AI trade for you. Link in bio to upgrade to VIP!")
            
            # File paths
            audio_path = f"storage/renders/temp_audio_{trade_id}.mp3"
            output_path = f"storage/renders/trade_{trade_id}.mp4"
            
            # Generate Audio (need to run asyncio loop because we are in a sync thread)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(VideoEngine._generate_tts(tts_text, audio_path))
            loop.close()

            audio_clip = AudioFileClip(audio_path)
            duration = min(audio_clip.duration, 59.0)
            seg_duration = duration / 3

            # 2. Generate 3 AI images via Replicate
            image_paths = []
            if image_prompts:
                image_paths = VideoEngine._generate_images_replicate(image_prompts, trade_id)

            # 3. Build background: slideshow OR Pexels fallback
            if len(image_paths) >= 1:
                from moviepy.editor import concatenate_videoclips
                slides = []
                for img_path in image_paths:
                    seg_dur = duration / len(image_paths)
                    slide = ImageClip(img_path).set_duration(seg_dur).resize(newsize=(1080, 1920))
                    slides.append(slide)
                while len(slides) < 3:
                    slides.append(slides[-1].copy().set_duration(seg_duration))
                video_clip = concatenate_videoclips(slides, method="compose")
                logger.info(f"Built slideshow from {len(image_paths)} AI images.")
            else:
                logger.info("Replicate unavailable. Falling back to Pexels video.")
                bg_path = VideoEngine._download_dynamic_background(asset, str(trade_id))
                if not os.path.exists(bg_path):
                    logger.error(f"Background {bg_path} not found!")
                    return None
                video_clip = VideoFileClip(bg_path).resize(newsize=(1080, 1920))
                if video_clip.duration < duration:
                    import moviepy.video.fx.all as vfx
                    video_clip = video_clip.fx(vfx.loop, duration=duration)

            video_clip = video_clip.subclip(0, duration)

            # 4. Text overlay
            overlay_array = VideoEngine._create_text_overlay(trade_data, free_metrics, vip_metrics, width=1080, height=1920)
            overlay_clip = ImageClip(overlay_array).set_position('center').set_duration(duration)


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
                # Generate dynamic description with FOMO
                yt_desc = (
                    f"Our AI trading algorithm just secured another massive profit on {asset}!\n\n"
                    f"✅ Direction: {direction}\n"
                    f"💰 Profit: +{pnl:.2f}%\n"
                )
                
                if vip_weekly > free_weekly and vip_weekly > 0 and free_weekly > 0:
                    yt_desc += f"🆓 Free Channel 7-Day PnL: +{free_weekly:.1f}%\n"
                    yt_desc += f"💎 VIP Premium 7-Day PnL: +{vip_weekly:.1f}%\n"
                elif vip_weekly > 0:
                    yt_desc += f"💎 VIP Premium 7-Day PnL: +{vip_weekly:.1f}%\n"
                    
                yt_desc += (
                    f"📈 VIP Win Rate: {vip_win_rate:.1f}%\n\n"
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
