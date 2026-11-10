import os
import sys
import asyncio
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
        return None

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
    if ffmpeg_exe:
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
    # 지원 플랫폼 링크 감지
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
            # 디스코드 무료 봇 파일 업로드 한도(약 25MB) 체크
            if file_size_mb > 25:
                await status_msg.edit(
                    content=f"⚠️ 영상 크기({file_size_mb:.1f}MB)가 디스코드 전송 한도(25MB)를 초과하여 "
                            f"서버 downloads 폴더에만 보관되었습니다."
                )
                return

            await status_msg.edit(content="📤 영상을 업로드하는 중입니다...")
            await message.reply(file=discord.File(video_path))
            await status_msg.delete()
        except yt_dlp.utils.DownloadError as e:
            await status_msg.edit(content=f"❌ 다운로드 실패 (비공개 또는 잘못된 링크): {e}")
        except Exception as e:
            await status_msg.edit(content=f"❌ 처리 중 오류 발생: {e}")

    await bot.process_commands(message)

# --- 무료 클라우드(Render/Koyeb 등) 헬스체크 웹 서버 ---
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
    # 1) 환경변수 우선 조회, 없으면 터미널 입력 (로컬/클라우드 양방향 호환)
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        try:
            token = input("디스코드 봇 토큰(BOT TOKEN)을 입력하세요: ").strip()
        except EOFError:
            token = ""
    if not token:
        print("[오류] DISCORD_TOKEN 환경변수 또는 토큰 입력이 필요합니다.")
        sys.exit(1)

    # 2) 헬스체크 웹 서버 시작
    await start_health_server()

    # 3) 디스코드 봇 로그인 및 실행
    await bot.start(token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n봇이 안전하게 종료되었습니다.")
