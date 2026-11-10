import sys
import os
import yt_dlp

def get_ffmpeg_path():
    """imageio-ffmpeg가 설치되어 있으면 해당 바이너리 경로를 자동 감지"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None

def download_video(url: str, output_dir: str = "./downloads"):
    """
    인스타그램 및 트위터(X) 링크에서 고화질 영상을 다운로드하는 핵심 함수
    """
    output_dir = output_dir.strip().strip('"').strip("'")
    if not output_dir:
        output_dir = "./downloads"

    os.makedirs(output_dir, exist_ok=True)

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': os.path.join(output_dir, '%(title).50s [%(id)s].%(ext)s'),
        'quiet': False,
        'no_warnings': True,
    }

    # imageio-ffmpeg 감지 시 자동 경로 설정
    ffmpeg_exe = get_ffmpeg_path()
    if ffmpeg_exe:
        ydl_opts['ffmpeg_location'] = ffmpeg_exe

    print(f"\n[다운로드 시작] {url}")
    print(f"[저장 대상 폴더] {os.path.abspath(output_dir)}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', '제목 없음')
            print(f"\n[성공] 다운로드 완료: {title}")
            print(f"저장 위치: {os.path.abspath(output_dir)}")
    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)
        if "ffmpeg is not installed" in err_msg:
            print("\n[안내] 영상 추출은 성공했으나, 비디오와 오디오를 합치기 위해 ffmpeg가 필요합니다.")
            print("터미널에서 아래 명령어를 한 번만 실행해 주세요:")
            print("pip install imageio-ffmpeg")
        else:
            print(f"\n[실패] 다운로드할 수 없습니다. (비공개 계정/차단 또는 링크 오류): {err_msg}")
    except Exception as e:
        print(f"\n[오류] 예기치 않은 오류가 발생했습니다: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_url = sys.argv[1].strip()
        save_path = sys.argv[2].strip() if len(sys.argv) > 2 else "./downloads"
    else:
        target_url = input("인스타그램 릴스 또는 트위터 영상 링크를 입력하세요: ").strip()
        save_path = input("저장할 폴더 경로를 입력하세요 (그냥 엔터 치면 기본 downloads 폴더): ").strip()

    if target_url:
        download_video(target_url, output_dir=save_path)
    else:
        print("링크가 입력되지 않았습니다.")
