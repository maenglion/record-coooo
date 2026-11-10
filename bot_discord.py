import os
import sys
import glob
import asyncio
import discord
from discord.ext import commands
import yt_dlp

def get_ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None

def download_video(url: str, output_dir: str = "./downloads"):
    os.makedirs(output_dir, exist_ok=True)
    out_tmpl = os.path.join(output_dir, '%(title).50s [%(id)s].%(ext)s')

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
    }
    ffmpeg_exe = get_ffmpeg_path()
    if ffmpeg_exe:
        ydl_opts['ffmpeg_location'] = ffmpeg_exe

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # 실제 저장된 파일 경로 찾기
        filepath = ydl.prepare_filename(info)
        # mp4로 병합된 파일 경로 보정
        base, _ = os.path.splitext(filepath)
        merged = base + '.mp4'
        if os.path.exists(merged):
            return merged, info.get('title', '영상')
        return filepath, info.get('title', '영상')

# 디스코드 봇 설정
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"\n[성공] 디스코드 봇 로그인 완료: {bot.user}")
    print("스마트폰 디스코드 앱에서 봇에게 인스타/트위터 링크를 보내보세요.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    # 인스타그램 또는 트위터(X) 링크인지 판별
    # 인스타그램, 트위터(X), 유튜브(쇼츠 포함), 틱톡 링크 감지
    supported = ["instagram.com/reel", "instagram.com/p/", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com"]
    if any(k in content for k in supported):
        status_msg = await message.reply("⏳ 영상 다운로드 중입니다. 잠시만 기다려주세요...")
        try:
            # yt-dlp 동기 함수를 비동기 스레드로 실행 (봇이 멈추지 않음)
            loop = asyncio.get_event_loop()
            video_path, title = await loop.run_in_executor(None, download_video, content)

            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            # 디스코드 일반 서버 파일 업로드 한도(약 25MB) 체크
            if file_size_mb > 25:
                await status_msg.edit(content=f"⚠️ 영상 크기가 {file_size_mb:.1f}MB로 디스코드 전송 한도(25MB)를 초과했습니다. PC의 downloads 폴더에만 저장되었습니다.")
                return

            await status_msg.edit(content="📤 영상을 업로드하는 중입니다...")
            await message.reply(file=discord.File(video_path))
            await status_msg.delete()
        except yt_dlp.utils.DownloadError as e:
            await status_msg.edit(content=f"❌ 다운로드 실패 (비공개 또는 잘못된 링크): {e}")
        except Exception as e:
            await status_msg.edit(content=f"❌ 처리 중 오류 발생: {e}")

    await bot.process_commands(message)

if __name__ == "__main__":
    TOKEN = input("디스코드 봇 토큰(BOT TOKEN)을 입력하세요: ").strip()
    if not TOKEN:
        print("토큰이 입력되지 않았습니다.")
        sys.exit(1)
    bot.run(TOKEN)
