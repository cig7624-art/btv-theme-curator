# B tv+ AI Theme Curator

Streamlit 기반 외부 이슈 탐색·테마 추천 도구입니다.

## 통합 수집 경로

- **YouTube 반응**: 한국 인기 영상, 주요 OTT·방송사 공식 채널의 최근 업로드, 12개 리뷰·해석·명장면 검색어
- **극장·박스오피스**: KOBIS 일별 순위, 신규 진입, 순위 변화, 일일·누적 관객 수
- **OTT 랭킹·신작**: Netflix 공식 한국 Top 10과 주요 OTT 신작·공개 예정 관련 자료
- **온라인 화제성**: 앞선 경로에서 발견한 작품의 네이버 데이터랩 검색 관심도 변화
- **뉴스·공식자료**: 공개·캐스팅·수상·편성 변화 등 이슈의 원인과 배경

키가 없는 경로는 자동으로 건너뛰고 나머지 경로는 계속 수집합니다.

## 핵심 이슈 점수

- 수집 경로: 60점
- 반응 강도: 20점
- 여러 경로 교차 확인: 15점
- 최근성: 5점

같은 작품이 여러 피드에서 반복 언급되면 반응 강도가 높아지고, 서로 다른 경로에서 함께 발견되면 교차 확인 점수가 추가됩니다.

## UI 변경

- 경로별 가중치에 마우스를 올리면 수집 기준 표시
- 경로 선택 드롭다운의 흰색 글자 문제 수정
- YouTube 썸네일 및 RSS 대표 이미지 표시
- 이미지가 없으면 출처·콘텐츠명을 활용한 기본 비주얼 표시
- 최근 핵심 이슈는 중복 피드를 하나로 묶어 대표 기사만 노출

## GitHub Actions Secrets

저장소의 `Settings → Secrets and variables → Actions`에 필요한 키를 추가합니다.

```text
YOUTUBE_API_KEY       필수: YouTube Data API v3
KOBIS_API_KEY         선택: 영화진흥위원회 KOBIS OpenAPI
NAVER_CLIENT_ID       선택: 네이버 데이터랩 애플리케이션 Client ID
NAVER_CLIENT_SECRET   선택: 네이버 데이터랩 애플리케이션 Client Secret
```

Netflix 공식 Top 10은 별도 키 없이 수집합니다.

## 로컬 실행

```bash
pip install -r requirements.txt
python collect_issues.py
streamlit run app.py
```

## 주요 파일

- `app.py`: Streamlit UI, 핵심 이슈 점수, 중복 이슈 통합
- `collect_issues.py`: 외부 데이터 통합 수집
- `issue_feed.csv`: 수집 결과
- `youtube_video_watchlist.csv`: YouTube 후보 영상 메타데이터
- `youtube_video_stats.csv`: 영상 통계 이력
- `theme_pool.csv`: 임시 테마 풀
- `content_db.csv`: 임시 콘텐츠 풀
