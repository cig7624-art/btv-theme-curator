# B tv+ AI Theme Curator

Streamlit 기반 외부 이슈 탐색·테마 추천 도구입니다.

## 현재 반영 상태

### 1단계 완료: 외부 데이터 수집 정상화

- YouTube Data API v3로 최근 영상 검색
- `videos.list` 통계로 조회수·좋아요·댓글 저장
- 3분 이하 영상은 `쇼츠/숏폼` 후보로 구분
- 첫 수집일에는 조회수·일평균 확산 속도·댓글 반응으로 이슈 선별
- 다음 수집일부터 전일 대비 조회수·댓글 증가량도 반영
- Google News 기반 결과를 실제 SNS 데이터로 표시하지 않고 `온라인 화제 기사`로 구분
- API 키가 없을 때만 기존 `yt-dlp` 방식으로 보조 실행

### 이후 작업

1. 키노라이츠 기반 전체 콘텐츠 후보 검색
2. 반복 테마 추천 억제 및 추천 이력 반영
3. 확장프로그램이 읽은 현재 웹 UI 데이터를 Streamlit 관제 메뉴로 전달
4. 전체 기능 완료 후 Streamlit UI 재구성

확장프로그램은 현재 **사용자가 링크로 들어간 활성 페이지를 읽는 단계**이며, 자동 순회·완전한 화면 관제 기능은 아직 포함하지 않습니다.

## YouTube API 설정

1. Google Cloud에서 프로젝트를 만들고 **YouTube Data API v3**를 활성화합니다.
2. API 키를 생성합니다.
3. GitHub 저장소에서 `Settings → Secrets and variables → Actions`로 이동합니다.
4. `New repository secret`을 누르고 아래 이름으로 저장합니다.

```text
Name: YOUTUBE_API_KEY
Secret: 발급받은 API 키
```

키 값은 코드나 CSV에 직접 넣지 않습니다.

## 로컬 실행

```bash
pip install -r requirements.txt
```

macOS/Linux:

```bash
export YOUTUBE_API_KEY="발급받은 키"
python collect_issues.py
streamlit run app.py
```

Windows PowerShell:

```powershell
$env:YOUTUBE_API_KEY="발급받은 키"
python collect_issues.py
streamlit run app.py
```

## 주요 파일

- `app.py`: Streamlit 화면
- `collect_issues.py`: 뉴스·YouTube 수집
- `issue_feed.csv`: 외부 이슈
- `youtube_video_watchlist.csv`: 발견 영상 메타데이터
- `youtube_video_stats.csv`: 날짜별 영상 통계
- `theme_pool.csv`: 임시 테마 풀
- `content_db.csv`: 임시 콘텐츠 풀
