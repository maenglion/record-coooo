# SNS Video Downloader (인스타그램 / 트위터 영상 다운로더)

인스타그램(릴스) 및 트위터(X) 링크를 넣으면 영상 원본 스트림을 고화질 MP4 파일로 자동 다운로드하는 파이썬 프로그램입니다.

## 1. 사전 준비 (설치)

파이썬(Python 3.10 이상)이 설치된 환경에서 아래 명령어를 실행하여 필수 라이브러리를 설치합니다.

```bash
pip install -r requirements.txt
```

> **참고**: 영상 병합 처리를 위해 컴퓨터에 [ffmpeg](https://ffmpeg.org/download.html)가 설치되어 있어야 합니다.

## 2. 실행 방법

터미널에서 아래 명령어로 실행하고 영상 링크를 붙여넣습니다:

```bash
python downloader.py
```

또는 링크를 명령어 뒤에 바로 붙여서 한 줄로 실행할 수도 있습니다:

```bash
python downloader.py "영상링크주소"
```

다운로드된 영상은 자동으로 `downloads/` 폴더에 최고 화질 MP4 형식으로 저장됩니다.

## 3. 지원 플랫폼
- 인스타그램 (Instagram Reels / 게시물 영상)
- 트위터 / X (Twitter / X 영상 게시물)
