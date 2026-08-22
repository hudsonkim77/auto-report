r"""auto-report 설정.

이 폴더(auto-report)는 위키 폴더(예: my-wiki-02)와 형제 폴더로 있어야 합니다.
WIKI_PATH는 부모 폴더에서 06_metrics 폴더를 가진 형제 폴더를 자동으로 찾습니다.

자동으로 못 찾으면(None) 아래처럼 직접 지정하세요:
    WIKI_PATH = Path(r"C:\Users\본인계정\...\my-wiki-02")
"""

from pathlib import Path
import warnings

APP_DIR = Path(__file__).resolve().parent


def _find_wiki_path():
    """형제 폴더 중 06_metrics 폴더를 가진 첫 번째 폴더를 위키 경로로 본다.

    OSError를 잡는 이유: 이 탐색은 앱 폴더 "바깥"(부모 폴더)을 들여다본다.
    로컬에선 문제없지만, Streamlit Community Cloud처럼 앱이 샌드박스
    컨테이너에서 도는 환경은 저장소 바깥 디렉터리 나열 자체를 막을 수 있다
    (실측 — 배포본에서 이 줄 때문에 앱이 시작도 못 하고 OSError로 죽었다).
    WIKI_PATH=None은 이미 정상적으로 지원되는 상태다(export_catalog.py만
    쓰고, 배포된 앱 자체는 카탈로그 JSON만 읽는다) — 그래서 탐색이 막히면
    "못 찾았다"로 조용히 넘어가고, 앱 시작을 막지 않는다.
    """
    parent = APP_DIR.parent
    try:
        siblings = sorted(parent.iterdir())
    except OSError:
        return None
    for sibling in siblings:
        try:
            if sibling.is_dir() and sibling != APP_DIR and (sibling / "06_metrics").is_dir():
                return sibling.resolve()
        except OSError:
            continue
    return None


WIKI_PATH = _find_wiki_path()

if WIKI_PATH is None:
    warnings.warn(
        "위키 경로를 자동으로 찾지 못했습니다. config.py의 WIKI_PATH를 직접 지정하세요."
    )

# --- BigQuery ---
BQ_PROJECT = None  # None이면 ADC 기본 프로젝트를 사용한다
BQ_DATASET = "project1_day1"
STAGING_PREFIX = "staging_"

# --- 이메일(초안용, 실제 발송 없음) ---
EMAIL_TO = "team@example.com"
EMAIL_FROM = "auto-report@example.com"
EMAIL_SUBJECT_PREFIX = "[자동 리포트]"

# --- 검증 기준 ---
MIN_SCHEMA_MATCH = 0.8  # 스키마 일치율 최소 기준
MOM_THRESHOLD = 5.0  # 전월 대비 상대변화율(%) 경고 임계값
TREND_MONTHS = 6  # 추이 차트에 보여줄 개월 수
