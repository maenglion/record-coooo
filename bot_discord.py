import os
import sys
import re
import asyncio
import subprocess
from aiohttp import web
import discord
from discord.ext import commands
import yt_dlp

# --- ffmpeg 경로 자동 감지 ---
def get_ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"

# --- 영상 재생시간(초) 추출 ---
def get_video_duration(ffmpeg_exe: str, video_path: str):
    try:
        r = subprocess.run([ffmpeg_exe, "-i", video_path],
                           capture_output=True, text=True, timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", r.stderr or "")
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None

# --- 디스코드 25MB 한도 맞춤 자동 압축 ---
def compress_video(ffmpeg_exe: str, input_path: str, target_mb: float = 23.0) -> str:
    """
    영상 재생시간을 기반으로 최적 비트레이트를 역산하여
    target_mb 이하의 크기로 자동 압축된 MP4를 생성
    """
    out_path = input_path.rsplit(".", 1)[0] + "_compressed.mp4"
    duration = get_video_duration(ffmpeg_exe, input_path)

    if duration and duration > 0:
        total_bits = target_mb * 8 * 1024 * 1024
        total_bitrate = total_bits / duration
        audio_bitrate = min(96000, total_bitrate * 0.15)
        video_bitrate = max(120000, total_bitrate - audio_bitrate)

        # 비트레이트 수준에 맞춰 해상도 자동 다운스케일
        if video_bitrate < 400000:
            scale = "scale='min(854,iw)':-2"
        elif video_bitrate < 900000:
            scale = "scale='min(1280,iw)':-2"
        else:
            scale = "scale='min(1920,iw)':-2"

        cmd = [
            ffmpeg_exe, "-y", "-i", input_path,
            "-vf", scale,
            "-c:v", "libx264", "-b:v", str(int(video_bitrate)),
            "-maxrate", str(int(video_bitrate * 1.3)),
            "-bufsize", str(int(video_bitrate * 2)),
            "-preset", "fast",
            "-c:a", "aac", "-b:a", str(int(audio_bitrate)),
            "-movflags", "+faststart",
            out_path,
        ]
    else:
        # 재생시간을 읽지 못했을 때의 안전한 폴백(CRF 기반)
        cmd = [
            ffmpeg_exe, "-y", "-i", input_path,
            "-vf", "scale='min(1280,iw)':-2",
            "-c:v", "libx264", "-crf", "30",
            "-preset", "fast",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            out_path,
        ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "")[-600:])
    return out_path

# --- 미디어 다운로드 핵심 함수 ---
def download_video(url: str, output_dir: str = "./downloads"):
    os.makedirs(output_dir, exist_ok=True)
    out_tmpl = os.path.join(output_dir, '%(title).50s [%(id)s].%(ext)s')

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': out_tmpl,
        'nopart': True,
        'windowsfilenames': True,
        'quiet': True,
        'no_warnings': True,
    }
    ffmpeg_exe = get_ffmpeg_path()
    if ffmpeg_exe and ffmpeg_exe != "ffmpeg":
        ydl_opts['ffmpeg_location'] = ffmpeg_exe

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filepath)
        merged = base + '.mp4'
        if os.path.exists(merged):
            return merged, info.get('title', '영상')
        return filepath, info.get('title', '영상')

# --- 디스코드 봇 설정 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"\n[성공] 디스코드 봇 로그인 완료: {bot.user}")
    print("스마트폰 디스코드 앱에서 봇에게 인스타/트위터/유튜브/틱톡/스레드 링크를 보내보세요.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    supported = [
        "instagram.com/reel", "instagram.com/p/",
        "twitter.com", "x.com",
        "youtube.com", "youtu.be",
        "tiktok.com",
        "threads.net", "threads.com",
    ]
    if any(k in content for k in supported):
        status_msg = await message.reply("⏳ 영상 다운로드 중입니다. 잠시만 기다려주세요...")
        try:
            loop = asyncio.get_event_loop()
            video_path, title = await loop.run_in_executor(None, download_video, content)

            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            send_path = video_path

            # 25MB 초과 시 디스코드 전송용 자동 압축
            if file_size_mb > 24.5:
                await status_msg.edit(
                    content=f"⚠️ 영상 크기({file_size_mb:.1f}MB)가 디스코드 한도(25MB)를 초과하여 자동 압축 중입니다... "
                            f"(길이에 따라 30초~2분 소요)"
                )
                try:
                    ffmpeg_exe = get_ffmpeg_path()
                    compressed = await loop.run_in_executor(
                        None, compress_video, ffmpeg_exe, video_path, 23.0
                    )
                    new_size = os.path.getsize(compressed) / (1024 * 1024)
                    print(f"[압축 완료] {file_size_mb:.1f}MB -> {new_size:.1f}MB")
                    send_path = compressed
                except Exception as ce:
                    await status_msg.edit(
                        content=f"❌ 자동 압축 실패: {ce}\n원본 영상은 서버 downloads 폴더에만 보관되었습니다."
                    )
                    return

            await status_msg.edit(content="📤 영상을 업로드하는 중입니다...")
            await message.reply(file=discord.File(send_path))
            await status_msg.delete()

            # 압축 임시 파일 정리 (원본은 downloads에 계속 보관)
            if send_path != video_path and os.path.exists(send_path):
                try:
                    os.remove(send_path)
                except OSError:
                    pass

        except yt_dlp.utils.DownloadError as e:
            await status_msg.edit(content=f"❌ 다운로드 실패 (비공개 또는 잘못된 링크): {e}")
        except Exception as e:
            await status_msg.edit(content=f"❌ 처리 중 오류 발생: {e}")

    await bot.process_commands(message)

# --- 24시간 클라우드 상주용 헬스체크 웹 서버 ---
async def start_health_server():
    async def handle_health(request):
        return web.Response(text="MyVideoDownloader Discord Bot is running!")

    port = int(os.getenv("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[HTTP] 헬스체크 서버 시작: 포트 {port}")

# --- 엔트리포인트 ---
async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        try:
            token = input("디스코드 봇 토큰(BOT TOKEN)을 입력하세요: ").strip()
        except EOFError:
            token = ""
    if not token:
        print("[오류] DISCORD_TOKEN 환경변수 또는 토큰 입력이 필요합니다.")
        sys.exit(1)

    await start_health_server()
    await bot.start(token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n봇이 안전하게 종료되었습니다.")
