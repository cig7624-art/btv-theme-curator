# Phase 1 작업 메모

## 변경한 핵심

- `yt-dlp` 중심 유튜브 검색을 YouTube Data API v3 우선 방식으로 교체
- 검색 결과와 통계 조회를 분리하고, 통계는 최대 50개씩 묶어서 호출
- 조회수 절대값뿐 아니라 게시 후 일평균 조회수와 전일 대비 증가량 반영
- 라이브·예정 영상, 비공개·임베드 불가 영상 제외
- Google News에서 찾은 SNS 관련 보도는 `SNS/숏폼`이 아니라 `온라인 화제 기사`로 표시
- GitHub Actions에서 `YOUTUBE_API_KEY` Secret 사용

## 아직 하지 않은 것

- Instagram/TikTok 원천 데이터 직접 수집
- 키노라이츠 전체 콘텐츠 후보 검색
- 테마 추천 다양화
- 확장프로그램과 Streamlit 데이터 전달
- 최종 UI 개편
