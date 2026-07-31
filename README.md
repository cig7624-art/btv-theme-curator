# B tv+ AI Theme Curator — 외부 이슈 수집 통합본

## 수집 경로

- **YouTube 반응 30%**: 한국 인기 영상, OTT·방송사 공식 채널 신규 업로드, 12개 리뷰·해석·명장면 검색어
- **극장·박스오피스 25%**: KOBIS 전일 일별 박스오피스 Top 10
- **OTT 랭킹·신작 25%**: Netflix 공식 한국 주간 Top 10 + 주요 OTT 공식 신작·공개 예정 자료
- **온라인 화제·뉴스 20%**: 최근 7일 기사·공식 발표와 동일 작품의 반복 언급 및 타 경로 교차 확인

네이버 데이터랩은 사용하지 않습니다. 온라인 화제성은 별도 검색량 API가 아니라 뉴스 반복 언급량과 YouTube·OTT·박스오피스의 교차 확인으로 판단합니다.

## 핵심 이슈 점수

- 수집 경로: 최대 60점
- 반응 강도: 최대 20점
- 교차 확인: 최대 15점
- 최근성: 최대 5점

같은 작품의 관련 피드가 많을수록 반응 강도 보너스가 붙고, 서로 다른 경로에서 동시에 확인될수록 교차 확인 점수가 높아집니다.

## 필요한 GitHub Secrets

```text
YOUTUBE_API_KEY   필수
KOBIS_API_KEY     극장·박스오피스 사용 시 필요
```

## 적용 파일

```text
app.py
collect_issues.py
requirements.txt
.github/workflows/update_issues.yml
```

파일을 덮어쓴 뒤 GitHub Actions의 `Update Issues`를 수동 실행하면 오늘 수집분이 새 기준으로 교체됩니다.
