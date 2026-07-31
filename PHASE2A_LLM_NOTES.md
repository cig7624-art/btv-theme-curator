# Phase 2A — LLM 신규 테마 생성

## 실제 흐름

1. 최근 핵심 이슈 상위 12개를 LLM에 전달합니다.
2. 첫 번째 호출에서는 기존 테마 DB를 전혀 전달하지 않고 신규 테마 후보를 생성합니다.
3. 두 번째 호출에서만 신규 후보와 기존 DB를 비교해 중복·품질을 검수합니다.
4. 기존 500개는 `LEGACY_UNVERIFIED`로 취급하며, 기존 표현이 명백히 더 좋은 경우에만 활용합니다. 기존 활용이 0개여도 됩니다.
5. 최종 채택된 AI 신규 테마는 `theme_pool.csv`에 자동 추가됩니다.
6. 화면에서는 `[AI 신규 생성]`과 `[기존 DB 활용]`을 구분합니다.
7. 추천 테마 전체의 콘텐츠를 일괄 불러오는 버튼은 다음 단계용으로 화면에 비활성 표시합니다.

## 신규 테마 DB 컬럼

기존 6개 컬럼은 유지하고 아래 컬럼을 자동 추가합니다.

- `source_status`: `LEGACY_UNVERIFIED`, `AI_GENERATED`, `HUMAN_APPROVED`, `USED`
- `created_date`
- `source_issue`
- `creation_angle`
- `approved_status`
- `last_recommended_date`
- `content_search_terms`
- `generation_model`

## 필수 Secret

Streamlit 앱의 **Settings → Secrets**에 등록합니다.

```toml
OPENAI_API_KEY = "sk-..."
OPENAI_MODEL = "gpt-5.6-terra"
```

GitHub Actions Secret이 아니라 **Streamlit 앱 Secret**입니다. 버튼 클릭 시 Streamlit 서버에서 LLM을 호출하기 때문입니다.

## 테마 DB 영구 저장

Streamlit Community Cloud의 로컬 파일 저장은 재배포 시 유지가 보장되지 않습니다. 기본 버전은 로컬 CSV에 합류시키고 업데이트 CSV 다운로드를 제공합니다.

GitHub 자동 커밋까지 사용하려면 다음 Secret을 추가합니다.

```toml
GITHUB_WRITE_TOKEN = "github_pat_..."
GITHUB_REPO = "owner/repository"
GITHUB_BRANCH = "main"
THEME_DB_GITHUB_PATH = "theme_pool.csv"
THEME_HISTORY_GITHUB_PATH = "theme_recommendation_history.csv"
```

`GITHUB_WRITE_TOKEN`은 해당 저장소의 Contents 읽기/쓰기 권한이 있는 fine-grained PAT를 사용합니다.
