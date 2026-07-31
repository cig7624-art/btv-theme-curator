# 테마 DB 영구 저장 설정

Streamlit Cloud의 `Settings → Secrets`에 아래 값을 추가합니다.

```toml
GITHUB_WRITE_TOKEN = "github_pat_..."
GITHUB_REPO = "OWNER/REPOSITORY"
GITHUB_BRANCH = "main"
```

- `GITHUB_WRITE_TOKEN`: 해당 저장소의 Contents 읽기/쓰기 권한을 가진 GitHub fine-grained personal access token
- `GITHUB_REPO`: 예) `myname/btv-theme-curator`
- `GITHUB_BRANCH`: 앱이 배포되는 브랜치

CSV가 저장소 루트가 아니라면 다음도 추가합니다.

```toml
THEME_DB_GITHUB_PATH = "data/theme_pool.csv"
THEME_HISTORY_GITHUB_PATH = "data/theme_recommendation_history.csv"
```

앱 시작 시 GitHub의 최신 테마 DB와 추천 이력을 내려받고, 신규 테마 생성이 끝나면 두 파일을 자동 커밋합니다. 영구 저장 설정이 없으면 테마 생성 버튼은 비활성화됩니다.
