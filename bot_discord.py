import os
import sys
import re
import time
import base64
import shutil
import tempfile
import asyncio
import subprocess
import logging
import http.cookiejar
from contextlib import ExitStack
from logging.handlers import RotatingFileHandler
import traceback
from urllib.parse import urlsplit
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
JOB_SEMAPHORE = asyncio.Semaphore(3)
TASK_TIMEOUT = 180
RETRY_COOLDOWN_SECONDS = 600
failed_cache: dict[str, dict] = {}
auto_retry_tasks: dict[str, asyncio.Task] = {}

# --- 쿠키 주입 (유튜브 로그인 차단 및 인스타 익명 한도 우회) ---
COOKIE_FILE = os.path.join(LOG_DIR, "cookies.txt")
_cookie_b64 = (os.getenv("COOKIES_BASE64", "").strip()
               or os.getenv("YOUTUBE_COOKIES_BASE64", "").strip())
if _cookie_b64:
    try:
        with open(COOKIE_FILE, "wb") as _cf:
            _cf.write(base64.b64decode(_cookie_b64))
        logger.info("쿠키 파일 주입 완료")
    except Exception as _exc:
        logger.error(f"쿠키 디코딩 실패: {_exc}")

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

class DownloadDiagnostics:
    """Keep only known warning categories, never raw warnings or cookies."""
    def __init__(self):
        self.flags = set()

    def debug(self, message):
        pass

    def warning(self, message):
        text = message.lower()
        if 'cookies are no longer valid' in text or 'cookies have expired' in text:
            self.flags.add('cookie_invalid')
        if 'http error 403' in text:
            self.flags.add('http_403')
        if 'http error 429' in text or 'too many requests' in text:
            self.flags.add('http_429')
        if 'no supported javascript runtime' in text or 'no supported javascript runtimes' in text:
            self.flags.add('js_runtime_missing')
        if 'challenge solving failed' in text or 'challenge solver' in text and 'skipped' in text:
            self.flags.add('js_challenge_failed')
        if 'could not determine which post this link points to' in text:
            self.flags.add('threads_unverified_target')

    def error(self, message):
        self.warning(message)

def youtube_diagnostic_probe(mode: str, video_id: str = 'ty_mcAhgiaE'):
    """Run one metadata-only probe; return only predefined, non-secret result codes."""
    diagnostic = DownloadDiagnostics()
    opts = {
        'quiet': True,
        'logger': diagnostic,
        'skip_download': True,
        'noplaylist': True,
        'socket_timeout': 10,
        'retries': 0,
        'extractor_retries': 0,
        'remote_components': ['ejs:github'],
    }
    with ExitStack() as stack:
        if mode != 'anonymous':
            if not os.path.isfile(COOKIE_FILE) or not os.path.getsize(COOKIE_FILE):
                return '쿠키 파일 없음'
            directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='video-bot-check-'))
            cookie_copy = os.path.join(directory, 'cookies.txt')
            shutil.copyfile(COOKIE_FILE, cookie_copy)
            opts['cookiefile'] = cookie_copy
            if mode == 'current':
                opts['extractor_args'] = {'youtube': {'player_client': ['web', 'web_safari']}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
            outcome = '영상 정보 확인됨 (실제 다운로드는 미검증)' if info else '영상 정보 없음'
        except Exception as exc:
            text = str(exc).lower().replace('’', "'")
            diagnostic.warning(text)
            if 'sign in to confirm' in text or 'login_required' in text:
                outcome = '유튜브 로그인/봇 확인 요구'
            elif 'failed to extract any player response' in text:
                outcome = '유튜브 응답 추출 실패 (원인 미확정)'
            elif 'private video' in text or 'video unavailable' in text:
                outcome = '영상 접근 불가'
            else:
                outcome = '영상 정보 확인 실패 (세부 원인 미확정)'
    labels = {
        'cookie_invalid': '쿠키 무효 경고',
        'http_403': 'HTTP 403',
        'http_429': 'HTTP 429',
        'js_runtime_missing': 'JS 실행기 미발견',
        'js_challenge_failed': 'JS 처리 실패',
    }
    flags = [labels[x] for x in sorted(diagnostic.flags) if x in labels]
    return outcome + (f"; 경고: {', '.join(flags)}" if flags else '')

def youtube_cookie_status():
    """Check cookie domains only, never display their names or values."""
    if not os.path.isfile(COOKIE_FILE) or not os.path.getsize(COOKIE_FILE):
        return '쿠키 파일 없음'
    try:
        jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
        jar.load(ignore_discard=True, ignore_expires=True)
        present = any(c.domain.lstrip('.').lower() in ('youtube.com', 'www.youtube.com')
                      or c.domain.lstrip('.').lower().endswith('.youtube.com') for c in jar)
        return '유튜브 도메인 쿠키 있음 (유효 여부 미확인)' if present else '유튜브 도메인 쿠키 없음'
    except (OSError, http.cookiejar.LoadError, ValueError):
        return '쿠키 파일 형식 확인 실패'

# --- 미디어 다운로드 핵심 함수 (단일 영상 + 플레이리스트/다중영상 모두 처리) ---
def download_media(url: str, output_dir: str = "./downloads"):
    os.makedirs(output_dir, exist_ok=True)
    out_tmpl = os.path.join(output_dir, '%(title).50s [%(id)s].%(ext)s')

    diagnostics = DownloadDiagnostics()
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': out_tmpl,
        'nopart': True,
        'windowsfilenames': True,
        'quiet': True,
        'logger': diagnostics,
        'max_downloads': 10,
        'remote_components': ['ejs:github'],
    }
    ffmpeg_exe = get_ffmpeg_path()
    if ffmpeg_exe and ffmpeg_exe != "ffmpeg":
        ydl_opts['ffmpeg_location'] = ffmpeg_exe

    # yt-dlp saves its cookie jar on close. Give each request a private copy,
    # so concurrent downloads cannot overwrite one another's cookie file.
    with ExitStack() as stack:
        if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0:
            cookie_dir = stack.enter_context(tempfile.TemporaryDirectory(prefix='video-bot-cookies-'))
            cookie_copy = os.path.join(cookie_dir, 'cookies.txt')
            shutil.copyfile(COOKIE_FILE, cookie_copy)
            ydl_opts['cookiefile'] = cookie_copy
            ydl_opts['extractor_args'] = {'youtube': {'player_client': ['web', 'web_safari']}}

        ydl = stack.enter_context(yt_dlp.YoutubeDL(ydl_opts))
        try:
            info = ydl.extract_info(url, download=True)
            if 'threads_unverified_target' in diagnostics.flags:
                raise yt_dlp.utils.DownloadError(
                    'ERROR: [threads] Could not verify which post this share link points to'
                )
        except yt_dlp.utils.DownloadError as exc:
            exc.diagnostic_flags = frozenset(diagnostics.flags)
            if diagnostics.flags:
                logger.warning('다운로드 진단 분류: %s', ', '.join(sorted(diagnostics.flags)))
            raise

        def resolve(ent):
            if ent is None:
                return None
            fp = ydl.prepare_filename(ent)
            base, _ = os.path.splitext(fp)
            for cand in (base + '.mp4', base + '.webm', fp):
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

def first_supported_url(content: str):
    """Extract a URL with an exact supported host or a genuine subdomain."""
    domains = (
        'instagram.com', 'twitter.com', 'x.com', 'youtube.com', 'youtu.be',
        'tiktok.com', 'threads.net', 'threads.com', 'facebook.com', 'fb.watch',
    )
    for match in re.finditer(r'https?://[^\s<>]+', content, flags=re.IGNORECASE):
        url = match.group(0).rstrip('.,)')
        try:
            parts = urlsplit(url)
            host = (parts.hostname or '').lower()
            if parts.scheme.lower() != 'https' or parts.port not in (None, 443):
                continue
            if parts.username or parts.password or not any(
                host == domain or host.endswith('.' + domain) for domain in domains
            ):
                continue
            if host == 'instagram.com' or host.endswith('.instagram.com'):
                if not (parts.path.startswith('/reel') or parts.path.startswith('/p/')):
                    continue
            return url
        except ValueError:
            continue
    return None

@bot.event
async def on_ready():
    logger.info(f"디스코드 봇 로그인 완료: {bot.user}")
    print("스마트폰 디스코드 앱에서 봇에게 인스타/트위터/유튜브/틱톡/스레드 링크를 보내보세요.")

    announce_flag = os.path.join(LOG_DIR, ".announced_diagnostics_v1")
    if not os.path.exists(announce_flag):
        body = (
            "📢 **MyVideoDownloader 봇 업데이트 공지**\n"
            "▫ 다운로드 요청별 쿠키 파일 충돌 방지\n"
            "▫ 유튜브 오류 원인을 구분할 수 있도록 진단 안내 개선\n"
            "※ 유튜브 다운로드는 여전히 실패할 수 있으며, 문제가 있으면 오류 안내를 관리자에게 알려주세요."
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

@bot.command(name='ytcheck')
@commands.has_guild_permissions(administrator=True)
@commands.cooldown(1, 180, commands.BucketType.guild)
async def youtube_check(ctx):
    """A bounded, manual diagnostic; no videos, raw errors or credentials are sent."""
    status = await ctx.reply('🔎 관리자 진단 중입니다. 같은 영상의 정보만 확인하며 다운로드하지 않습니다.')
    async with JOB_SEMAPHORE:
        def check():
            cookie_status = youtube_cookie_status()
            deno = '확인됨' if shutil.which('deno') else '찾지 못함'
            version = yt_dlp.version.__version__
            modes = [('현재 설정', 'current'), ('쿠키 + 기본 접속 방식', 'default_cookie'),
                     ('쿠키 없이 기본 접속 방식', 'anonymous')]
            results = [(label, youtube_diagnostic_probe(mode)) for label, mode in modes]
            return cookie_status, deno, version, results

        try:
            cookie_status, deno, version, results = await asyncio.to_thread(check)
            report = '\n'.join(f'• {label}: {result}' for label, result in results)
            await status.edit(content=(
                f'🔎 **유튜브 진단 결과**\nyt-dlp: {version} | Deno: {deno}\n'
                f'쿠키: {cookie_status}\n{report}\n'
                '※ 영상 정보 확인 결과이며, 다운로드 성공이나 쿠키 유효성을 보장하지 않습니다.'
            ))
        except Exception:
            logger.error('유튜브 진단 도중 내부 오류 (상세 내용 비공개)')
            await status.edit(content='⚠️ 진단을 마치지 못했습니다. 관리자에게 알려주세요.')

# --- 공지 수동 발송 명령어 (!공지 내용) ---
@bot.command(name="공지")
@commands.has_permissions(administrator=True)
async def announce(ctx, *, content: str):
    await ctx.message.delete()
    await ctx.send(f"📢 {content}")

# --- 에러 유형별 친절한 안내 문구 분류 ---
def friendly_error_message(err_str: str, diagnostics=()) -> tuple[str, bool]:
    lower = err_str.lower()
    if 'cookie_invalid' in diagnostics:
        return (
            '⚠️ 유튜브가 로그인 쿠키가 유효하지 않다고 응답했습니다. 관리자에게 알려주세요.',
            False,
        )
    if "sign in to confirm you're not a bot" in lower:
        return ('⚠️ 유튜브에서 봇 확인을 위한 로그인을 요구했습니다. 잠시 후 다시 시도해 주세요.', True)
    if 'player response' in lower:
        details = []
        if 'http_403' in diagnostics:
            details.append('유튜브 요청 일부가 HTTP 403으로 거절됨')
        if 'http_429' in diagnostics:
            details.append('유튜브 요청 한도 초과(429)')
        if 'js_runtime_missing' in diagnostics:
            details.append('자바스크립트 실행 환경을 찾지 못함')
        if 'js_challenge_failed' in diagnostics:
            details.append('유튜브 자바스크립트 처리에 실패함')
        detail = f"\n진단: {', '.join(details)}." if details else ''
        return (
            '⚠️ 유튜브의 영상 정보를 가져오지 못했습니다. 차단인지 다른 문제인지 아직 확인할 수 없습니다.'
            + detail + '\n잠시 후 다시 시도하거나 관리자에게 알려주세요.',
            True,
        )
    if '[threads]' in lower and ('no downloadable video' in lower or 'no video post found' in lower):
        return (
            '⚠️ 스레드 게시물에서 다운로드 가능한 영상을 찾지 못했어요. '
            '영상이 없는 게시물이거나, 공유 링크 또는 스레드 응답 형식을 추출기가 처리하지 못했을 수 있습니다.',
            False,
        )
    if '[threads]' in lower and 'could not verify which post' in lower:
        return (
            '⚠️ 공유 링크의 게시물을 정확히 확인하지 못했어요. 다른 영상을 잘못 보내지 않도록 중단했습니다. '
            '스레드에서 게시물 주소를 다시 복사해 보내 주세요.',
            False,
        )
    if ("redirected to the login page" in lower
            or "rate-limit for accessing posts anonymously" in lower
            or "instagram send empty" in lower
            or "login_required" in lower):
        return (
            "⚠️ 인스타그램이 로그인을 요구했습니다. 잠시 후 다시 시도하거나 관리자에게 알려주세요.",
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

# --- 10분 후 자동 재시도 워커 ---
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
        msg, _ = friendly_error_message(str(exc), getattr(exc, 'diagnostic_flags', ()))
        try:
            await channel.send(
                f"{user_mention} ❌ 10분 후 자동 재시도했으나 완료되지 않았습니다.\n{msg}\n"
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

# --- 독립 작업 처리 함수 (한 사람 실패가 다른 사람 요청에 영향 없음) ---
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
        failed_cache.pop(content, None)

    except asyncio.TimeoutError:
        log_error("다운로드 작업 타임아웃(3분 초과)", TimeoutError(), url=content)
        await status_msg.edit(
            content="❌ 영상 다운로드 시간이 3분을 초과하여 작업이 취소되었습니다. "
                    "서버가 혼잡하거나 매우 긴 영상일 수 있습니다. 잠시 후 다시 시도해 주세요."
        )
    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)
        log_error("다운로드 실패 (yt-dlp)", e, url=content, user=str(message.author))
        msg, recommend_retry = friendly_error_message(err_str, getattr(e, 'diagnostic_flags', ()))
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
        content = first_supported_url(content)
        if content:
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
                    f"⚠️ 이 영상은 방금 다운로드에 실패했습니다.\n"
                    f"⏳ **10분 후 봇이 자동으로 1회 재시도**해 드릴게요! 지금은 다른 영상을 먼저 보내주세요."
                )
                await bot.process_commands(message)
                return

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
