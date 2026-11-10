import sys
import os
import yt_dlp

def download_video(url: str, output_dir: str = "./downloads"):
    """
    인스타그램 및 트위터(X) 링크에서 고화질 영상을 다운로드하는 핵심 함수
    """
    # 따옴표 및 양끝 공백 제거 (윈도우 폴더 드래그앤드롭 대비)
    output_dir = output_dir.strip().strip('"').strip("'")
    if not output_dir:
        output_dir = "./downloads"

    # 저장 폴더가 없으면 자동 생성
    os.makedirs(output_dir, exist_ok=True)

    # yt-dlp 옵션 설정
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': os.path.join(output_dir, '%(title).50s [%(id)s].%(ext)s'),
        'quiet': False,
        'no_warnings': True,
    }

    print(f"\n[다운로드 시작] {url}")
    print(f"[저장 대상 폴더] {os.path.abspath(output_dir)}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', '제목 없음')
            print(f"\n[성공] 다운로드 완료: {title}")
            print(f"저장 위치: {os.path.abspath(output_dir)}")
    except yt_dlp.utils.DownloadError as e:
        print(f"\n[실패] 다운로드할 수 없습니다. (비공개 계정이거나 차단된 링크): {e}")
    except Exception as e:
        print(f"\n[오류] 예기치 않은 오류가 발생했습니다: {e}")

if __name__ == "__main__":
    # 터미널에서 인자로 바로 넘겼을 때
    # 예: python downloader.py "https://..." "D:\영상폴더"
    if len(sys.argv) > 1:
        target_url = sys.argv[1].strip()
        save_path = sys.argv[2].strip() if len(sys.argv) > 2 else "./downloads"
    else:
        # 인자 없이 그냥 실행했을 때 콘솔에서 입력받기
        target_url = input("인스타그램 릴스 또는 트위터 영상 링크를 입력하세요: ").strip()
        save_path = input("저장할 폴더 경로를 입력하세요 (그냥 엔터 치면 기본 downloads 폴더): ").strip()

    if target_url:
        download_video(target_url, output_dir=save_path)
    else:
        print("링크가 입력되지 않았습니다.")
