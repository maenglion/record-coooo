import os
import sys
import re
import time
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

# --- 동시성·큐 격리 (한 사람의 실패가 다른 사람 요청에 영향 없도록) ---
JOB_SEMAPHORE = asyncio.Semaphore(3)   # 512MB RAM 보호용 동시 최대 3개
TASK_TIMEOUT = 180                     # 작업당 최대 3분 하드 타임아웃
RETRY_COOLDOWN_SECONDS = 600           # 같은 실패 URL은 10분 후 자동 재시도
# url -> {"time": ts, "user": id, "channel_id": id, "retries": n, "scheduled": bool}
failed_cache: dict[str, dict] = {}
auto_retry_tasks: dict[str, asyncio.Task] = {}

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

SUPPORTED = [
    "instagram.com/reel", "instagram.com/p/",
    "twitter.com", "x.com",
    "youtube.com", "youtu.be",
    "tiktok.com",
    "threads.net", "threads.com",
]

@bot.event
async def on_ready():
    logger.info(f"디스코드 봇 로그인 완료: {bot.user}")
    print("스마트폰 디스코드 앱에서 봇에게 인스타/트위터/유튜브/틱톡/스레드 링크를 보내보세요.")

    # [요구사항 2] 버전별 최초 기동 시 복구·공지 1회 발송
    announce_flag = os.path.join(LOG_DIR, ".announced_v6")
    if not os.path.exists(announce_flag):
        body = (
            "📢 **MyVideoDownloader 봇 업데이트 공지**\n"
            "▫ 다중영상(1/2, 2/2) 분할 전송 기능 복구 완료\n"
            "▫ 작업 완전 분리(누군가의 실패가 다른 사람 요청에 영향 없음)\n"
            "▫ 차단 영상 10분 후 자동 재시도 안내 시스템 적용\n"
            "▫ 대용량 영상 25MB 이하 자동 압축 유지\n"
            "※ 유튜브는 클라우드 봇 차단 이슈로 쿠키 설정 후 정상화 예정 (인스타/X/틱톡/스레드는 정상)"
        )
        for guild in bot.guilds:
            for ch in guild.text_channels:
                perms = ch.permissions_for(guild.me)
                if perms.send_messages and ch.name in ("일반", "general", "영상다운로드방", "봇-공지"):
                    try:
                        await ch.send(body)
                        break
                    except Exception:
                        pass
        try:
            with open(announce_flag, "w", encoding="utf-8") as f:
                f.write("1")
        except OSError:
            pass

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

# --- 공지 수동 발송 명령어 (!공지 내용) ---
@bot.command(name="공지")
@commands.has_permissions(administrator=True)
async def announce(ctx, *, content: str):
    await ctx.message.delete()
    await ctx.send(f"📢 {content}")

# --- 에러 유형별 친절한 안내 문구 분류 ---
def friendly_error_message(err_str: str) -> tuple[str, bool]:
    """(안내문구, 자동재시도 추천 여부)"""
    if "player response" in err_str or "Sign in to confirm you're not a bot" in err_str:
        return (
            "⚠️ 유튜브에서 로그인을 요구하여 다운로드가 일시 차단되었습니다. "
            "같은 링크를 연속 전송하시면 차단이 더 길어집니다. ⏳ 10~15분 후 다시 시도해 주세요. "
            "(인스타그램·트위터·틱톡·스레드는 정상 작동 중입니다)",
            True,
        )
    if "429" in err_str:
        return (
            "⚠️ 해당 플랫폼의 요청 한도(Rate Limit)에 도달했습니다. "
            "⏳ 10~15분 후 다시 시도해 주세요.",
            True,
        )
    if "is not a valid URL" in err_str or "Unsupported URL" in err_str:
        return ("❌ 지원되지 않거나 잘못된 형식의 링크입니다.", False)
    if "Private video" in err_str or "This video is private" in err_str:
        return ("❌ 비공개 계정 또는 비공개 영상입니다.", False)
    return (f"❌ 처리 중 오류 발생: {err_str[:400]}", False)

# --- 10분 후 자동 재시도 워커 (요구사항 4) ---
async def auto_retry_worker(url: str, channel_id: int, user_mention: str, original_url: str):
    await asyncio.sleep(RETRY_COOLDOWN_SECONDS)
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    if channel is None:
        return

    logger.info(f"자동 재시도 시작: {url}")
    status_msg = None
    try:
        status_msg = await channel.send(
            f"🔄 {user_mention} 10분이 경과하여 아까 실패했던 영상 자동 재시도 중입니다..."
        )
        loop = asyncio.get_event_loop()
        results = await asyncio.wait_for(
            loop.run_in_executor(None, download_media, url),
            timeout=TASK_TIMEOUT,
        )
        for idx, (video_path, title) in enumerate(results, 1):
            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            send_path = video_path
            if file_size_mb > 24.5:
                ffmpeg_exe = get_ffmpeg_path()
                compressed = await loop.run_in_executor(
                    None, compress_video, ffmpeg_exe, video_path, 23.0
                )
                send_path = compressed
            tag = f" ({idx}/{len(results)})" if len(results) > 1 else ""
            await channel.send(
                f"✅ [자동 재시도 성공] 요청하셨던 영상입니다{tag}:",
                file=discord.File(send_path),
            )
            if send_path != video_path and os.path.exists(send_path):
                try:
                    os.remove(send_path)
                except OSError:
                    pass
        failed_cache.pop(url, None)
    except Exception as exc:
        log_error("자동 재시도 최종 실패", exc, url=url)
        msg, _ = friendly_error_message(str(exc))
        try:
            await channel.send(
                f"{user_mention} ❌ 10분 후 자동 재시도했으나 여전히 차단 상태입니다.\n{msg}\n"
                f"나중에 다시 올려주세요. (상세 로그: `!logs`)"
            )
        except Exception:
            pass
    finally:
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        auto_retry_tasks.pop(url, None)

# --- 독립 작업 처리 함수 (요구사항 1: 한 사람 실패가 다른 사람 요청에 영향 없음) ---
async def process_job(message: discord.Message, content: str):
    logger.info(f"다운로드 요청: user={message.author} channel={message.channel} url={content}")
    status_msg = await message.reply("⏳ 영상 다운로드 중입니다. 잠시만 기다려주세요...")
    try:
        loop = asyncio.get_event_loop()
        async with JOB_SEMAPHORE:
            results = await asyncio.wait_for(
                loop.run_in_executor(None, download_media, content),
                timeout=TASK_TIMEOUT,
            )

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
        failed_cache.pop(content, None)  # 성공 시 실패 캐시 해제

    except asyncio.TimeoutError:
        log_error("다운로드 작업 타임아웃(3분 초과)", TimeoutError(), url=content)
        await status_msg.edit(
            content="❌ 영상 다운로드 시간이 3분을 초과하여 작업이 취소되었습니다. "
                    "서버가 혼잡하거나 매우 긴 영상일 수 있습니다. 잠시 후 다시 시도해 주세요."
        )
    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)
        log_error("다운로드 실패 (yt-dlp)", e, url=content, user=str(message.author))
        msg, recommend_retry = friendly_error_message(err_str)
        if recommend_retry:
            failed_cache[content] = {"time": time.time(), "user": message.author.id, "scheduled": False}
        await status_msg.edit(
            content=f"{msg}\n(상세 로그는 `!logs` 명령어로 확인 가능합니다.)"
        )
    except Exception as e:
        log_error("처리 중 예외 발생", e, url=content, user=str(message.author))
        msg, _ = friendly_error_message(str(e))
        await status_msg.edit(
            content=f"{msg}\n(상세 로그는 `!logs` 명령어로 확인 가능합니다.)"
        )

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()

    if not content.startswith(bot.command_prefix):
        if any(k in content for k in SUPPORTED):
            # [요구사항 4] 같은 실패 URL의 반복 전송 방어 + 10분 후 자동 재시도
            last = failed_cache.get(content)
            now = time.time()
            if last and (now - last["time"]) < RETRY_COOLDOWN_SECONDS:
                eta_sec = int(RETRY_COOLDOWN_SECONDS - (now - last["time"]))
                eta_min = max(1, eta_sec // 60)
                if last.get("scheduled"):
                    await message.reply(
                        f"⏰ 해당 영상은 이미 자동 재시도 대기열에 등록되어 있습니다 (남은 대기: 약 {eta_min}분). "
                        f"지금은 다른 영상을 먼저 보내주세요!"
                    )
                    await bot.process_commands(message)
                    return
                last["scheduled"] = True
                task = asyncio.create_task(
                    auto_retry_worker(content, message.channel.id, message.author.mention, content)
                )
                auto_retry_tasks[content] = task
                await message.reply(
                    f"⚠️ 이 영상은 플랫폼 일시 차단으로 방금 실패했습니다. 같은 링크를 연속 전송하면 차단이 길어집니다.\n"
                    f"⏳ **10분 후 봇이 자동으로 1회 재시도**해 드릴게요! 지금은 다른 영상을 먼저 보내주세요."
                )
                await bot.process_commands(message)
                return

            # 독립 비동기 태스크로 실행 (작업 완전 분리)
            asyncio.create_task(process_job(message, content))

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
