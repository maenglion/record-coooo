import os
import sys
import re
import base64
import asyncio
import subprocess
import logging
from logging.handlers import RotatingFileHandler
import traceback
from aiohttp import web
import discord
from discord.ext import commands
import yt_dlp

# --- 로깅 설정 (ERROR 전용 log + 앱 전체 log 분리, 파일 순환 보관) ---
LOG_DIR = os.getenv("LOG_DIR", "./logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("video_bot")
logger.setLevel(logging.INFO)

error_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "error.log"),
    maxBytes=2 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))

app_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "app.log"),
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
app_handler.setLevel(logging.INFO)
app_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

logger.addHandler(error_handler)
logger.addHandler(app_handler)
logger.addHandler(console_handler)

def log_error(context: str, exc: Exception, **meta):
    meta_str = " | ".join(f"{k}={v}" for k, v in meta.items() if v)
    tail = f" ({meta_str})" if meta_str else ""
    logger.error(
        f"{context}{tail}: {exc}\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}"
    )

# --- YouTube 쿠키 주입 (데이터센터 IP 봇 차단 우회용) ---
COOKIE_FILE = os.path.join(LOG_DIR, "youtube_cookies.txt")
_yt_cookie_b64 = os.getenv("YOUTUBE_COOKIES_BASE64", "").strip()
if _yt_cookie_b64:
    try:
        with open(COOKIE_FILE, "wb") as _cf:
            _cf.write(base64.b64decode(_yt_cookie_b64))
        logger.info("YouTube 쿠키 파일 주입 완료")
    except Exception as _exc:
        logger.error(f"YouTube 쿠키 디코딩 실패: {_exc}")

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
    except Exception as exc:
        log_error("영상 재생시간 파싱 실패", exc, file=video_path)
    return None

# --- 디스코드 25MB 한도 맞춤 자동 압축 ---
def compress_video(ffmpeg_exe: str, input_path: str, target_mb: float = 23.0) -> str:
    out_path = input_path.rsplit(".", 1)[0] + "_compressed.mp4"
    duration = get_video_duration(ffmpeg_exe, input_path)

    if duration and duration > 0:
        total_bits = target_mb * 8 * 1024 * 1024
        total_bitrate = total_bits / duration
        audio_bitrate = min(96000, total_bitrate * 0.15)
        video_bitrate = max(120000, total_bitrate - audio_bitrate)

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
        err = RuntimeError((r.stderr or "")[-600:])
        log_error("ffmpeg 압축 실패", err, file=input_path)
        raise err
    return out_path

# --- 미디어 다운로드 핵심 함수 (단일 영상 + 플레이리스트/다중영상 모두 처리) ---
def download_media(url: str, output_dir: str = "./downloads"):
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
        'max_downloads': 10,
    }
    ffmpeg_exe = get_ffmpeg_path()
    if ffmpeg_exe and ffmpeg_exe != "ffmpeg":
        ydl_opts['ffmpeg_location'] = ffmpeg_exe

    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0:
        ydl_opts['cookiefile'] = COOKIE_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        def resolve(ent):
            if ent is None:
                return None
            fp = ydl.prepare_filename(ent)
            base, _ = os.path.splitext(fp)
            for cand in (base + '.mp4', fp):
                if cand and os.path.exists(cand):
                    return cand, ent.get('title') or '영상'
            vid = ent.get('id', '')
            for name in os.listdir(output_dir):
                if vid and vid in name and not name.endswith('.part'):
                    return os.path.join(output_dir, name), ent.get('title') or '영상'
            return None

        entries = info.get('entries') if info.get('_type') == 'playlist' else None
        if entries is not None:
            results = [r for r in (resolve(e) for e in entries) if r]
        else:
            r = resolve(info)
            results = [r] if r else []

        if not results:
            raise RuntimeError("추출된 영상 파일을 찾을 수 없습니다.")
        return results

# --- 디스코드 봇 설정 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logger.info(f"디스코드 봇 로그인 완료: {bot.user}")
    print("스마트폰 디스코드 앱에서 봇에게 인스타/트위터/유튜브/틱톡/스레드 링크를 보내보세요.")

# --- 에러 로그 즉시 조회 명령어 (!logs / !log) ---
@bot.command(name="logs", aliases=["log"])
async def show_logs(ctx, lines: int = 25):
    log_file = os.path.join(LOG_DIR, "error.log")
    if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
        await ctx.reply("기록된 에러 로그가 없습니다. (정상 작동 중)")
        return
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = "".join(all_lines[-max(1, lines):])
        if len(tail) <= 1900:
            await ctx.reply(f"```{tail}```")
        else:
            await ctx.reply(
                "최근 에러 로그가 길어 파일로 첨부합니다:",
                file=discord.File(log_file, filename="error.log"),
            )
    except Exception as exc:
        log_error("!logs 명령어 처리 실패", exc)
        await ctx.reply(f"로그 파일 읽기 실패: {exc}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()

    if not content.startswith(bot.command_prefix):
        supported = [
            "instagram.com/reel", "instagram.com/p/",
            "twitter.com", "x.com",
            "youtube.com", "youtu.be",
            "tiktok.com",
            "threads.net", "threads.com",
        ]
        if any(k in content for k in supported):
            logger.info(f"다운로드 요청: user={message.author} channel={message.channel} url={content}")
            status_msg = await message.reply("⏳ 영상 다운로드 중입니다. 잠시만 기다려주세요...")
            try:
                loop = asyncio.get_event_loop()
                results = await loop.run_in_executor(None, download_media, content)

                send_queue = []
                for idx, (video_path, title) in enumerate(results, 1):
                    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
                    send_path = video_path

                    if file_size_mb > 24.5:
                        tag = f" ({idx}/{len(results)})" if len(results) > 1 else ""
                        await status_msg.edit(
                            content=f"⚠️ 영상{tag} 크기({file_size_mb:.1f}MB)가 디스코드 한도(25MB)를 초과하여 자동 압축 중입니다... "
                                    f"(30초~2분 소요)"
                        )
                        try:
                            ffmpeg_exe = get_ffmpeg_path()
                            compressed = await loop.run_in_executor(
                                None, compress_video, ffmpeg_exe, video_path, 23.0
                            )
                            new_size = os.path.getsize(compressed) / (1024 * 1024)
                            logger.info(f"압축 완료: {file_size_mb:.1f}MB -> {new_size:.1f}MB file={os.path.basename(video_path)}")
                            send_path = compressed
                        except Exception as ce:
                            log_error("자동 압축 처리 실패", ce, url=content, file=video_path)
                            await status_msg.edit(
                                content=f"❌ 자동 압축 실패: {ce}\n원본 영상은 서버 downloads 폴더에만 보관되었습니다.\n"
                                        f"(상세 로그는 `!logs` 명령어로 확인 가능합니다.)"
                            )
                            return
                    send_queue.append((send_path, video_path, title))

                await status_msg.edit(content="📤 영상을 업로드하는 중입니다...")
                for idx, (send_path, original_path, title) in enumerate(send_queue, 1):
                    tag = f"{idx}/{len(send_queue)}" if len(send_queue) > 1 else ""
                    await message.reply(tag or None, file=discord.File(send_path))
                    logger.info(f"전송 완료: {title} ({os.path.basename(send_path)})")
                    if send_path != original_path and os.path.exists(send_path):
                        try:
                            os.remove(send_path)
                        except OSError as exc:
                            log_error("압축 임시파일 삭제 실패", exc, path=send_path)

                await status_msg.delete()

            except yt_dlp.utils.DownloadError as e:
                err_str = str(e)
                log_error("다운로드 실패 (yt-dlp)", e, url=content, user=str(message.author))
                hint = ""
                if "player response" in err_str or "Sign in to confirm" in err_str:
                    hint = "\n💡 유튜브 측 봇 차단(로그인 요구)으로 인한 실패입니다. PC 터미널에서 다운로드하거나 쿠키 주입이 필요합니다."
                await status_msg.edit(
                    content=f"❌ 다운로드 실패 (비공개 또는 잘못된 링크): {err_str}{hint}\n"
                            f"(상세 로그는 `!logs` 명령어로 확인 가능합니다.)"
                )
            except Exception as e:
                log_error("처리 중 예외 발생", e, url=content, user=str(message.author))
                await status_msg.edit(
                    content=f"❌ 처리 중 오류 발생: {e}\n"
                            f"(상세 로그는 `!logs` 명령어로 확인 가능합니다.)"
                )

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
    logger.info(f"헬스체크 웹 서버 시작: 포트 {port}")

# --- 엔트리포인트 ---
async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        try:
            token = input("디스코드 봇 토큰(BOT TOKEN)을 입력하세요: ").strip()
        except EOFError:
            token = ""
    if not token:
        logger.error("DISCORD_TOKEN 환경변수 또는 토큰 입력이 필요합니다.")
        sys.exit(1)

    await start_health_server()
    await bot.start(token)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("봇이 안전하게 종료되었습니다.")
