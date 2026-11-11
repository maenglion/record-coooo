import os
import sys
import yt_dlp

def get_ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"

def download_video(url: str, output_dir: str = "./downloads"):
    os.makedirs(output_dir, exist_ok=True)
    out_tmpl = os.path.join(output_dir, '%(title).50s [%(id)s].%(ext)s')

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': out_tmpl,
        'nopart': True,
        'windowsfilenames': True,
        'quiet': False,
        'no_warnings': False,
        'max_downloads': 10,
    }
    ffmpeg_exe = get_ffmpeg_path()
    if ffmpeg_exe and ffmpeg_exe != "ffmpeg":
        ydl_opts['ffmpeg_location'] = ffmpeg_exe

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        def resolve(ent):
            if ent is None:
                return None
            fp = ydl.prepare_filename(ent)
            base, _ = os.path.splitext(fp)
            for cand in (base + '.mp4', fp):
                if cand and os.path.exists(cand):
                    return cand
            vid = ent.get('id', '')
            for name in os.listdir(output_dir):
                if vid and vid in name and not name.endswith('.part'):
                    return os.path.join(output_dir, name)
            return None

        entries = info.get('entries') if info.get('_type') == 'playlist' else None
        if entries is not None:
            results = [r for r in (resolve(e) for e in entries) if r]
        else:
            r = resolve(info)
            results = [r] if r else []

        if not results:
            raise RuntimeError("추출된 영상 파일을 찾을 수 없습니다.")
        return results, info.get('title', '영상')

def main():
    if len(sys.argv) > 1:
        url = sys.argv[1].strip()
        folder = sys.argv[2].strip() if len(sys.argv) > 2 else "./downloads"
    else:
        print("=== SNS 영상 다운로더 (인스타 / 트위터 / 유튜브 / 틱톡 / 스레드) ===")
        url = input("SNS 영상 링크를 입력하세요: ").strip()
        folder = input("저장 폴더 (엔터 = 기본 downloads): ").strip() or "./downloads"

    if not url:
        print("링크가 입력되지 않았습니다.")
        return

    print(f"\n[다운로드 시작] {url}")
    print(f"[저장 대상 폴더] {os.path.abspath(folder)}")
    try:
        paths, title = download_video(url, folder)
        print(f"\n[성공] 제목: {title}")
        for p in paths:
            print(f"  → 파일: {os.path.abspath(p)}")
    except yt_dlp.utils.DownloadError as e:
        print(f"\n[실패] 다운로드할 수 없습니다. (비공개 계정/차단 또는 링크 오류): {e}")
    except Exception as e:
        print(f"\n[오류] 예기치 않은 오류가 발생했습니다: {e}")

if __name__ == "__main__":
    main()
