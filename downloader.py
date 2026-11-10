import sys
import os
import yt_dlp

def download_video(url: str, output_dir: str = "downloads"):
    """
    인스타그램 및 트위터(X) 링크에서 고화질 영상을 다운로드하는 핵심 함수
    """
    # 저장 폴더 생성
    os.makedirs(output_dir, exist_ok=True)

    # yt-dlp 옵션 설정
    ydl_opts = {
        # 최고 화질 비디오 + 오디오 병합하여 MP4로 저장
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        # 저장 경로 및 파일명 규칙
        'outtmpl': os.path.join(output_dir, '%(title).50s [%(id)s].%(ext)s'),
        'quiet': False,
        'no_warnings': True,
    }

    print(f"\n[다운로드 시작] {url}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', '제목 없음')
            print(f"\n[성공] 다운로드 완료: {title}")
            print(f"저장 위치: {os.path.abspath(output_dir)} 폴더")
    except yt_dlp.utils.DownloadError as e:
        print(f"\n[실패] 다운로드할 수 없습니다. (비공개 계정이거나 잘못된 링크): {e}")
    except Exception as e:
        print(f"\n[오류] 예기치 않은 오류가 발생했습니다: {e}")

if __name__ == "__main__":
    # 터미널에서 인자로 바로 넘겼을 때 (예: python downloader.py https://...)
    if len(sys.argv) > 1:
        target_url = sys.argv[1].strip()
    else:
        # 인자 없이 그냥 실행했을 때 직접 입력받기
        target_url = input("인스타그램 릴스 또는 트위터 영상 링크를 입력하세요: ").strip()

    if target_url:
        download_video(target_url)
    else:
        print("링크가 입력되지 않았습니다.")
