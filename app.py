"""auto-report Streamlit 진입점. CLAUDE.md 2절의 8단계 흐름을 화면으로 표현한다.

지금까지 구현 범위: 1단계(파일 투입), 2단계(스키마 점검 → 지표 판정, Profile·Confirm·
Model 연결까지). 실제 지표 "계산"(BigQuery 적재)은 아직 없다 — pipeline/profile.py의
판정 결과만 화면에 보여준다. 3~8단계는 제목과 "대기" 배지만 표시하는 자리표시자다.
"""

import json
import platform
import sys
import time
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path

import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
CATALOG_DIR = APP_DIR / "catalog"

# streamlit run app.py는 스크립트 폴더를 sys.path에 자동으로 넣어주지만,
# 다른 실행 방식(테스트 러너 등)에서는 그렇지 않을 수 있어 명시적으로 넣는다.
sys.path.insert(0, str(APP_DIR))

import config  # noqa: E402
from common import (  # noqa: E402
    status_badge, format_metric_value, format_metric_value_compact,
    format_delta_value, COLOR_SLATE, COLOR_SERIES_A, COLOR_SERIES_B,
)
from pipeline import profile as profiler  # noqa: E402
from pipeline import calculate as calculator  # noqa: E402
from pipeline import compare as comparator  # noqa: E402
from pipeline import validate as validator  # noqa: E402
from pipeline import charts as charter  # noqa: E402
from pipeline import report as reporter  # noqa: E402
from pipeline import manual_sections  # noqa: E402
from pipeline import email_draft  # noqa: E402
import plotly.graph_objects as go  # noqa: E402

# 7~8단계는 아직 실제 구현이 없다. 사이드바에서 "진행중"이 아니라 "준비됨"으로
# 표시하기 위한 구분이다(게이트를 통과했다고 없는 기능이 갑자기 "진행중"이 되면 안 된다).
IMPLEMENTED_STEPS = {1, 2, 3, 4, 5, 6, 7, 8}

STEPS = [
    (1, "데이터 파일 투입"),
    (2, "스키마 점검 → 지표 계산"),
    (3, "검증 실행"),
    (4, "대시보드 렌더링"),
    (5, "내용·검증 결과 확인"),
    (6, "리포트 생성"),
    (7, "이메일 초안 생성"),
    (8, "발송 확정"),
]

st.set_page_config(page_title="월간 리포트 자동화", layout="wide")


# ---------------------------------------------------------------------------
# 세션 상태
# ---------------------------------------------------------------------------

def init_session_state():
    st.session_state.setdefault("step", 1)
    st.session_state.setdefault("df", None)
    st.session_state.setdefault("filename", None)
    st.session_state.setdefault("file_bytes", None)
    st.session_state.setdefault("upload_time", None)
    st.session_state.setdefault("stage_durations", {})
    st.session_state.setdefault("table_judgment", None)
    st.session_state.setdefault("profile_result", None)
    st.session_state.setdefault("metric_judgments", None)
    st.session_state.setdefault("run_dir", None)
    st.session_state.setdefault("extend_approved", False)
    st.session_state.setdefault("metric_results", None)
    st.session_state.setdefault("sql_log", None)
    st.session_state.setdefault("comparison_df", None)
    st.session_state.setdefault("prev_period", None)
    st.session_state.setdefault("staging_table", None)
    st.session_state.setdefault("validation_result", None)
    st.session_state.setdefault("chk_period", False)
    st.session_state.setdefault("chk_warnings", False)
    st.session_state.setdefault("chk_partial", False)
    st.session_state.setdefault("chk_skipped", False)
    st.session_state.setdefault("step5_confirmed_at", None)
    st.session_state.setdefault("report_md", None)
    st.session_state.setdefault("report_path", None)
    st.session_state.setdefault("report_substituted", None)
    st.session_state.setdefault("report_remaining", None)
    st.session_state.setdefault("report_warnings", None)
    st.session_state.setdefault("email_draft", None)
    st.session_state.setdefault("email_paths", None)
    st.session_state.setdefault("approved_at", None)
    st.session_state.setdefault("chk8_period", False)
    st.session_state.setdefault("chk8_recipients", False)
    st.session_state.setdefault("chk8_warnings", False)
    st.session_state.setdefault("chk8_unwritten", False)
    st.session_state.setdefault("chk8_attachments", False)


def reset_downstream_state():
    """새 파일이 올라오면 1단계 이후의 모든 결과를 지운다.
    다음 프롬프트에서 4~8단계 상태(검증 결과·리포트 경로 등)가 추가되면
    여기서 같이 지워야 한다."""
    st.session_state.step = 1
    st.session_state.df = None
    st.session_state.filename = None
    st.session_state.file_bytes = None
    st.session_state.upload_time = None
    st.session_state.stage_durations = {}
    st.session_state.table_judgment = None
    st.session_state.profile_result = None
    st.session_state.metric_judgments = None
    st.session_state.run_dir = None
    st.session_state.extend_approved = False
    st.session_state.metric_results = None
    st.session_state.sql_log = None
    st.session_state.comparison_df = None
    st.session_state.prev_period = None
    st.session_state.staging_table = None
    st.session_state.validation_result = None
    st.session_state.chk_period = False
    st.session_state.chk_warnings = False
    st.session_state.chk_partial = False
    st.session_state.chk_skipped = False
    st.session_state.step5_confirmed_at = None
    st.session_state.report_md = None
    st.session_state.report_path = None
    st.session_state.report_substituted = None
    st.session_state.report_remaining = None
    st.session_state.report_warnings = None
    st.session_state.email_draft = None
    st.session_state.email_paths = None
    st.session_state.approved_at = None
    st.session_state.chk8_period = False
    st.session_state.chk8_recipients = False
    st.session_state.chk8_warnings = False
    st.session_state.chk8_unwritten = False
    st.session_state.chk8_attachments = False


init_session_state()


# ---------------------------------------------------------------------------
# CSV 읽기 — utf-8-sig 우선, 실패하면 cp949
# ---------------------------------------------------------------------------

def read_csv_with_fallback(uploaded_file):
    try:
        return pd.read_csv(uploaded_file, encoding="utf-8-sig")
    except UnicodeDecodeError:
        uploaded_file.seek(0)
        return pd.read_csv(uploaded_file, encoding="cp949")


# ---------------------------------------------------------------------------
# 카탈로그 로딩 (사이드바·2단계 공용)
# ---------------------------------------------------------------------------

def _update_run_log(run_dir: Path, **sections) -> dict:
    """run_log.json을 부분 갱신한다. 8단계 전체가 서로 다른 시점(가끔은 서로
    다른 rerun)에 자기 몫만 쓰기 때문에, 매번 파일을 다시 읽어 주어진 키만
    덮어쓰고 나머지는 그대로 둔다 — 한 단계가 다른 단계가 이미 써둔 값을
    지우는 사고(예: 6단계 기록이 3단계 기록을 날림)를 막는다."""
    path = run_dir / "run_log.json"
    run_log = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    run_log.update(sections)
    path.write_text(json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return run_log


_ENV_PACKAGES = ["streamlit", "pandas", "google-cloud-bigquery", "pyyaml", "plotly", "fpdf2", "pyarrow"]


def _environment_info() -> dict:
    """실행 환경 스냅샷 — 이 실행이 어떤 버전 조합에서 돌았는지 나중에
    추적하기 위함이다(예: 나중에 결과가 재현 안 되면 여기부터 비교한다)."""
    versions = {}
    for pkg in _ENV_PACKAGES:
        try:
            versions[pkg] = importlib_metadata.version(pkg)
        except importlib_metadata.PackageNotFoundError:
            versions[pkg] = "설치 안 됨"
    return {"python_버전": platform.python_version(), "패키지_버전": versions}


def load_insights_catalog():
    """catalog/insights_catalog.json을 읽는다. 없으면 빈 dict — report.py의
    5장 인용 로직은 이게 없어도(전부 "관련 분석 없음"으로) 동작하므로 여기서
    load_catalogs()처럼 (None, None)을 강제해 화면을 막을 필요가 없다."""
    path = CATALOG_DIR / "insights_catalog.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_catalogs():
    """catalog/*.json을 읽는다. 없으면 (None, None)을 반환한다."""
    metrics_path = CATALOG_DIR / "metrics_catalog.json"
    schema_path = CATALOG_DIR / "schema_catalog.json"
    if not (metrics_path.exists() and schema_path.exists()):
        return None, None
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return metrics, schema


# ---------------------------------------------------------------------------
# 기존 실행 불러오기 — run_pipeline.py(CLI)가 미리 만들어둔 run_*을
# 화면으로 이어받아 확인·확정만 하는 흐름
# ---------------------------------------------------------------------------

HUMAN_CHAPTERS = ["2", "5", "6"]


def _list_run_dirs() -> list:
    output_root = APP_DIR / "outputs"
    if not output_root.exists():
        return []
    return sorted(
        (p for p in output_root.iterdir() if p.is_dir() and p.name.startswith("run_")),
        key=lambda p: p.name, reverse=True,
    )


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() == "true"


def _load_existing_run(run_dir: Path):
    """outputs/run_*/ 하나를 골라 그 산출물로 화면 상태를 복원한다.

    run_pipeline.py(CLI)가 1~7단계를 미리 다 만들어두고, 사람은 화면에서
    확인·확정(8단계)만 하는 흐름을 위한 진입점이다. 계산을 다시 하지 않고
    저장된 산출물(run_log.json·metrics.csv·comparison.csv·validation.json·
    report.md·email.html 등)만 읽는다 — BigQuery를 다시 부르지 않는다.

    딱 하나, table_judgment/profile_result/metric_judgments는 저장된 적이
    없어서 다시 계산한다. 이 셋은 df·카탈로그만으로 정해지는 순수 함수라서
    (BigQuery를 안 쓴다) 다시 돌려도 비용이 거의 없고, run_log의 요약본보다
    화면이 원래 기대하는 정확한 모양(예: 전체 지표 판정 표)을 그대로 낸다.

    한계 — staging_table 재사용: BigQuery의 스테이징 테이블은 테이블명당
    하나뿐이라(WRITE_TRUNCATE), 이 run 이후 다른 실행이 있었으면 그 테이블은
    이미 최신 데이터로 덮어써져 있다. 대시보드의 "이번 실행 대상이 아니었던
    지표" 즉석 재계산은 그 최신 스테이징을 다시 조회하므로, 오래된 run을
    불러왔을 때 그 부분만 지금 스테이징 상태를 반영할 수 있다 — report.md·
    email.html처럼 이미 저장된 산출물은 영향받지 않는다.
    """
    run_log_path = run_dir / "run_log.json"
    run_log = json.loads(run_log_path.read_text(encoding="utf-8"))

    reset_downstream_state()
    metrics_catalog, schema_catalog = load_catalogs()

    # --- 1단계: 원본 CSV ---
    filename = run_log.get("파일명")
    csv_path = run_dir / filename if filename else None
    df = None
    if csv_path and csv_path.exists():
        with open(csv_path, "rb") as f:
            try:
                df = read_csv_with_fallback(f)
            except Exception:  # noqa: BLE001 - 복원 실패해도 나머지는 보여준다
                df = None

    st.session_state.df = df
    st.session_state.filename = filename
    st.session_state.file_bytes = csv_path.read_bytes() if csv_path and csv_path.exists() else None
    st.session_state.upload_time = run_log.get("1_투입", {}).get("업로드_시각")

    # --- 2단계: 판정 (재계산 — BigQuery 아님) ---
    table_name = None
    if df is not None and metrics_catalog is not None:
        table_judgment = profiler.judge_table(df, schema_catalog)
        st.session_state.table_judgment = table_judgment
        table_name = table_judgment["테이블명"]
        table_info = schema_catalog.get(table_name, {})
        profile_result = profiler.profile_data(df, table_info)
        st.session_state.profile_result = profile_result
        primary_period = next(iter(profile_result["기간컬럼"].values()), None)
        if primary_period is not None:
            st.session_state.metric_judgments = profiler.judge_metrics(table_name, primary_period, metrics_catalog)

    # --- 2단계: 계산 결과 ---
    metrics_csv = run_dir / "metrics.csv"
    results = []
    if metrics_csv.exists():
        mdf = pd.read_csv(metrics_csv, keep_default_na=False, na_values=[""])
        for _, row in mdf.iterrows():
            results.append(calculator.MetricResult(
                metric_id=row["metric_id"], 지표명=row["지표명"], 유형=row["유형"], month=str(row["month"]),
                value=None if pd.isna(row["value"]) else row["value"],
                sample_size=None if pd.isna(row["sample_size"]) else row["sample_size"],
                min_sample=None if pd.isna(row["min_sample"]) else row["min_sample"],
                status=row["status"],
                error=None if pd.isna(row["error"]) else row["error"],
                부분갱신=_to_bool(row["부분갱신"]),
                원천=[t for t in str(row["원천"]).split("+") if t],
            ))
    st.session_state.metric_results = results
    st.session_state.sql_log = []  # CLI 실행은 SQL 로그를 남기지 않는다

    comparison_csv = run_dir / "comparison.csv"
    st.session_state.comparison_df = pd.read_csv(comparison_csv) if comparison_csv.exists() else None

    validation_path = run_dir / "validation.json"
    st.session_state.validation_result = (
        json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else None
    )

    st.session_state.staging_table = run_log.get("2_계산", {}).get("스테이징_테이블명")
    st.session_state.extend_approved = bool(run_log.get("유효구간_확장_승인"))

    기간 = run_log.get("기간", "")
    기간_최소 = 기간.split("~")[0].strip() if 기간 else None
    st.session_state.prev_period = comparator.previous_month(기간_최소) if 기간_최소 else None

    st.session_state.run_dir = str(run_dir)

    # CLI는 화면 확인(5단계)을 사람 대신 건너뛴다 — run_log가 그 사실을 이미
    # "생략" 사유로 남겼으므로, 여기서는 그걸 근거로 통과시킨다(추측이 아니다).
    if run_log.get("4_5_확인", {}).get("생략") or run_log.get("4_5_확인", {}).get("게이트2_확정_시각"):
        st.session_state.step5_confirmed_at = (
            run_log.get("4_5_확인", {}).get("게이트2_확정_시각") or run_log.get("확정_시각")
        )

    # --- 6단계: 리포트 ---
    report_path = run_dir / "report.md"
    if report_path.exists():
        st.session_state.report_md = report_path.read_text(encoding="utf-8")
        st.session_state.report_path = str(report_path)
        미작성 = run_log.get("6_리포트", {}).get("미작성_장_목록", [])
        st.session_state.report_remaining = 미작성
        st.session_state.report_substituted = [c for c in HUMAN_CHAPTERS if c not in 미작성]
        st.session_state.report_warnings = []  # 대상기간 불일치 등 경고는 생성 시점 값이라 다시 채우지 않는다

    # --- 7단계: 이메일 ---
    email_html_path = run_dir / "email.html"
    email_meta_path = run_dir / "email_meta.json"
    if email_html_path.exists() and email_meta_path.exists():
        meta = json.loads(email_meta_path.read_text(encoding="utf-8"))
        text_path = run_dir / "email.txt"
        st.session_state.email_draft = {
            "subject": meta["subject"], "to": meta["to"], "from": meta["from"],
            "body_html": email_html_path.read_text(encoding="utf-8"),
            "body_text": text_path.read_text(encoding="utf-8") if text_path.exists() else "",
            "attachments": meta["attachments"],
        }
        st.session_state.email_paths = {
            "html": str(email_html_path), "text": str(text_path), "meta": str(email_meta_path),
        }

    # --- 8단계: 이미 확정됐으면 그대로 반영, 아니면 어디까지 왔는지로 진행 표시만 ---
    approved_path = run_dir / "APPROVED"
    if approved_path.exists():
        approved = json.loads(approved_path.read_text(encoding="utf-8"))
        st.session_state.approved_at = approved.get("발송확정_시각")
        st.session_state.step = 9
    elif st.session_state.email_draft:
        st.session_state.step = 8
    elif st.session_state.report_md:
        st.session_state.step = 7
    elif st.session_state.validation_result:
        st.session_state.step = 4
    else:
        st.session_state.step = 2


# ---------------------------------------------------------------------------
# 사이드바
# ---------------------------------------------------------------------------

def render_sidebar():
    with st.sidebar:
        st.header("카탈로그 상태")

        metrics, schema = load_catalogs()

        if metrics is not None and schema is not None:
            n_metrics = metrics.get("_meta", {}).get("항목_개수", "?")
            n_tables = schema.get("_meta", {}).get("항목_개수", "?")
            generated_at = metrics.get("_meta", {}).get("생성일시", "알 수 없음")

            c1, c2 = st.columns(2)
            c1.metric("지표", f"{n_metrics}종")
            c2.metric("테이블", f"{n_tables}종")
            st.caption(f"카탈로그 생성일시: {generated_at}")
        else:
            st.warning("카탈로그가 없습니다. export를 먼저 실행하세요.")
            st.code("python catalog/export_catalog.py", language="bash")

        st.divider()
        st.header("진행 단계")

        current = st.session_state.step
        for num, name in STEPS:
            if num < current:
                status = "완료"
                icon = "✓ "
            elif num == current:
                status = "진행중" if num in IMPLEMENTED_STEPS else "준비됨"
                icon = ""
            else:
                status = "대기"
                icon = ""
            st.markdown(
                status_badge(f"{icon}{num}. {name}", status),
                unsafe_allow_html=True,
            )

        st.divider()
        st.header("기존 실행 불러오기")
        run_dirs = _list_run_dirs()
        if not run_dirs:
            st.caption("outputs/에 run_* 폴더가 없습니다.")
        else:
            labels = []
            for p in run_dirs:
                approved = (p / "APPROVED").exists()
                labels.append(f"{p.name} {'(확정 완료)' if approved else ''}".strip())
            choice = st.selectbox("run 폴더 선택", options=range(len(run_dirs)), format_func=lambda i: labels[i])
            if st.button("불러오기"):
                _load_existing_run(run_dirs[choice])
                st.rerun()


# ---------------------------------------------------------------------------
# 1단계 — 데이터 파일 투입 (오늘 실제로 구현하는 부분)
# ---------------------------------------------------------------------------

def render_step1():
    st.header("1단계 — 데이터 파일 투입")

    uploaded = st.file_uploader("CSV 파일 업로드", type=["csv"])

    if uploaded is None:
        return

    # 새 파일이면(파일명이 바뀌면) 이후 단계 상태를 전부 초기화하고 새로 처리한다.
    if uploaded.name != st.session_state.filename:
        reset_downstream_state()

        t0 = time.time()
        try:
            df = read_csv_with_fallback(uploaded)
        except Exception as e:  # noqa: BLE001 - 사용자에게 원인을 그대로 보여준다
            st.error(f"CSV를 읽을 수 없습니다(utf-8-sig, cp949 둘 다 실패): {e}")
            return
        st.session_state.stage_durations["1_투입"] = round(time.time() - t0, 3)

        st.session_state.df = df
        st.session_state.filename = uploaded.name
        st.session_state.file_bytes = uploaded.getvalue()  # 게이트 확정 시 run 폴더에 원본 복사용
        st.session_state.upload_time = datetime.now().isoformat()
        st.session_state.step = 2  # 1단계 완료 -> 2단계로

    df = st.session_state.df
    filename = st.session_state.filename
    size_kb = uploaded.size / 1024

    st.markdown(status_badge("완료", "완료"), unsafe_allow_html=True)

    st.write(f"**파일명**: {filename}")
    c1, c2, c3 = st.columns(3)
    c1.metric("크기", f"{size_kb:.1f} KB")
    c2.metric("행 수", f"{len(df):,}")
    c3.metric("컬럼 수", len(df.columns))

    st.write("**컬럼 목록**:", ", ".join(str(c) for c in df.columns))

    st.subheader("미리보기 (앞 5행)")
    st.dataframe(df.head(5))


# ---------------------------------------------------------------------------
# 2단계 — 스키마 점검 → 지표 판정 (Profile·Confirm·Model 연결)
# ---------------------------------------------------------------------------

def render_step2():
    st.header("2단계 — 스키마 점검 → 지표 판정")

    if st.session_state.df is None:
        st.markdown(status_badge("대기", "대기"), unsafe_allow_html=True)
        st.caption("1단계에서 파일을 업로드하면 여기서 판정이 시작됩니다.")
        return

    metrics_catalog, schema_catalog = load_catalogs()
    if metrics_catalog is None or schema_catalog is None:
        st.error("카탈로그가 없어 판정할 수 없습니다. export를 먼저 실행하세요.")
        st.code("python catalog/export_catalog.py", language="bash")
        return

    df = st.session_state.df

    # judge_table/profile_data/judge_metrics는 순수 함수라 매번 다시 불러도
    # 비용이 크지 않다. 새 파일이 올라올 때만 다시 계산되도록 세션에 캐싱한다.
    # fresh는 이번 rerun에서 실제로 새로 계산하는지를 표시한다 — 이미 캐싱된
    # 결과를 그냥 다시 읽는 rerun에서까지 "2_판정" 소요시간을 거의 0으로
    # 덮어써서 실제 소요시간을 지우는 일이 없게 한다.
    fresh = st.session_state.table_judgment is None
    t0 = time.time()

    if st.session_state.table_judgment is None:
        st.session_state.table_judgment = profiler.judge_table(df, schema_catalog)

    table_judgment = st.session_state.table_judgment

    # --- 1. 테이블 판정 결과 ---
    st.subheader("1. 테이블 판정 결과")

    if table_judgment["판정가능"]:
        st.markdown(
            status_badge(
                f"{table_judgment['테이블명']} (일치율 {table_judgment['일치율']:.0%})",
                "통과",
            ),
            unsafe_allow_html=True,
        )
    else:
        label = table_judgment["테이블명"] or "판정 불가"
        st.markdown(
            status_badge(f"{label} (일치율 {table_judgment['일치율']:.0%})", "판정불가"),
            unsafe_allow_html=True,
        )
        st.caption("어느 테이블인지 확정할 수 없습니다. 카탈로그에 없는 파일이거나 컬럼이 크게 다릅니다.")

    if table_judgment["누락_컬럼"]:
        st.markdown(status_badge("누락 컬럼", "경고"), unsafe_allow_html=True)
        st.write(", ".join(table_judgment["누락_컬럼"]))

    if table_judgment["추가_컬럼"]:
        st.markdown(status_badge("카탈로그에 없는 추가 컬럼", "정보"), unsafe_allow_html=True)
        st.write(", ".join(table_judgment["추가_컬럼"]))

    if not table_judgment["판정가능"]:
        # 테이블이 확정되지 않으면 Profile·지표 판정 자체가 의미 없다(무엇과 대조할지
        # 모르는 상태). CLAUDE.md 9절: "카탈로그에 없는 테이블을 추측해 통과 금지".
        if fresh:
            st.session_state.stage_durations["2_판정"] = round(time.time() - t0, 3)
        return

    table_name = table_judgment["테이블명"]
    table_info = schema_catalog.get(table_name, {})

    if st.session_state.profile_result is None:
        st.session_state.profile_result = profiler.profile_data(df, table_info)
    profile_result = st.session_state.profile_result

    # --- 2. Profile 결과 표 ---
    st.subheader("2. Profile 결과")

    period_cols = profile_result["기간컬럼"]
    if period_cols:
        기간_요약 = "; ".join(
            f"{col} {info['최소']}~{info['최대']}" for col, info in period_cols.items()
        )
        기간_상태 = "통과"
    else:
        기간_요약 = "기간 컬럼을 찾지 못함"
        기간_상태 = "경고"

    if profile_result["결측"]:
        결측_요약 = ", ".join(f"{col} {n}건" for col, n in profile_result["결측"].items())
        결측_상태 = "경고"
    else:
        결측_요약 = "없음"
        결측_상태 = "통과"

    grain_candidates = profile_result["그레인_후보"]
    if any(c["유일함"] for c in grain_candidates):
        unique_one = next(c for c in grain_candidates if c["유일함"])
        그레인_요약 = f"{' + '.join(unique_one['키'])} 기준 유일함 ({unique_one['출처']})"
        그레인_상태 = "통과"
    elif grain_candidates:
        그레인_요약 = "; ".join(
            f"{' + '.join(c['키'])} 중복 {c['중복행수']}건" for c in grain_candidates
        )
        그레인_상태 = "경고"
    else:
        그레인_요약 = "후보를 찾지 못함"
        그레인_상태 = "경고"

    profile_rows = [
        ("행수", f"{profile_result['행수']:,}", "통과" if profile_result["행수"] > 0 else "경고"),
        ("컬럼 수", str(profile_result["컬럼수"]), "통과" if profile_result["컬럼수"] > 0 else "경고"),
        ("기간", 기간_요약, 기간_상태),
        ("결측", 결측_요약, 결측_상태),
        ("그레인 중복", 그레인_요약, 그레인_상태),
    ]

    for label, value, status in profile_rows:
        c1, c2, c3 = st.columns([1, 3, 1])
        c1.write(f"**{label}**")
        c2.write(value)
        c3.markdown(status_badge(status, status), unsafe_allow_html=True)

    # --- 3. 지표 판정 표 ---
    st.subheader("3. 지표 판정")

    primary_period = next(iter(period_cols.values()), None)
    if st.session_state.metric_judgments is None and primary_period is not None:
        st.session_state.metric_judgments = profiler.judge_metrics(
            table_name, primary_period, metrics_catalog
        )
    metric_judgments = st.session_state.metric_judgments or []

    if fresh:
        st.session_state.stage_durations["2_판정"] = round(time.time() - t0, 3)

    relevant = [m for m in metric_judgments if m["상태"] != "이 파일과 무관"]
    irrelevant = [m for m in metric_judgments if m["상태"] == "이 파일과 무관"]

    def render_metric_row(m):
        c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 4, 2])
        c1.write(m["지표명"])
        c2.write(f"`{m['metric_id']}`")
        c3.markdown(status_badge(m["상태"], m["상태"]), unsafe_allow_html=True)
        c4.write(m["이유"])
        c5.write(", ".join(m["원천"]))

    header_cols = st.columns([2, 2, 2, 4, 2])
    for c, label in zip(header_cols, ["지표명", "metric_id", "상태", "이유", "원천"]):
        c.markdown(f"**{label}**")

    for m in relevant:
        render_metric_row(m)

    if irrelevant:
        with st.expander(f"이 파일과 무관한 지표 {len(irrelevant)}종 (접힘)"):
            for m in irrelevant:
                render_metric_row(m)

    # --- 4. 요약 한 줄 ---
    계산가능 = sum(1 for m in metric_judgments if m["상태"] == "계산가능")
    확장필요 = sum(1 for m in metric_judgments if m["상태"] == "유효구간 확장 필요")
    무관 = len(irrelevant)
    st.markdown(
        f"**요약**: 계산 대상 {계산가능 + 확장필요}종 "
        f"(그중 유효구간 확장 필요 {확장필요}종), 무관 {무관}종"
    )


# ---------------------------------------------------------------------------
# 승인 게이트 — "지표 확인 후 확정" (CLAUDE.md 2단계와 3단계 사이)
# ---------------------------------------------------------------------------

def render_gate():
    """2단계 판정 결과를 사람이 보고 진행/중단을 정하는 첫 게이트.

    게이트는 판정이지 계산이 아니다 — 여기서 뭔가를 새로 계산하지 않고 2단계가
    이미 만들어 둔 st.session_state 결과만 읽어서 요약하고, 확정 시 그 스냅샷을
    outputs/run_*/에 기록한다.
    """
    table_judgment = st.session_state.table_judgment
    if table_judgment is None:
        return  # 2단계가 아직 끝나지 않음(파일 미업로드 등)

    st.divider()
    st.header("승인 게이트 — 지표 확인 후 확정")

    # 이미 확정된 실행이 있으면 요약 대신 확정 상태 + 취소 버튼만 보여준다.
    if st.session_state.run_dir:
        st.markdown(status_badge("확정됨", "완료"), unsafe_allow_html=True)
        st.write(f"확정된 실행 폴더: `{st.session_state.run_dir}`")
        st.caption("이미 만들어진 실행 기록은 취소해도 지워지지 않습니다.")
        if st.button("확정 취소"):
            # run_dir만 지우면 계산 결과·비교·검증·대시보드·5단계가 전부 화면에
            # 그대로 남는다 — 이 게이트가 "확정 전"으로 되돌아갔는데 그 아래는
            # 여전히 "확정된 결과"를 보여주는 모순된 상태가 된다(실제로 클릭해서
            # 확인한 버그). 게이트 확정 시 채워지는 상태를 전부 같이 지운다.
            st.session_state.step = 2
            st.session_state.run_dir = None
            st.session_state.metric_results = None
            st.session_state.sql_log = None
            st.session_state.comparison_df = None
            st.session_state.prev_period = None
            st.session_state.staging_table = None
            st.session_state.validation_result = None
            st.session_state.step5_confirmed_at = None
            st.rerun()
        return

    if not table_judgment["판정가능"]:
        st.markdown(status_badge("판정불가", "판정불가"), unsafe_allow_html=True)
        st.write("사유: 테이블을 판정할 수 없어 확정할 수 없습니다(필수 컬럼 누락 또는 일치율 미달).")
        st.button("이 판정으로 계산 진행", disabled=True)
        return

    profile_result = st.session_state.profile_result
    metric_judgments = st.session_state.metric_judgments or []

    계산대상 = [m for m in metric_judgments if m["상태"] in ("계산가능", "유효구간 확장 필요")]
    확장필요_목록 = [m for m in metric_judgments if m["상태"] == "유효구간 확장 필요"]
    부분갱신_목록 = [m for m in metric_judgments if m["부분갱신여부"]]

    metrics_catalog, _ = load_catalogs()
    catalog_generated_at = metrics_catalog.get("_meta", {}).get("생성일시", "알 수 없음")

    primary_period = next(iter(profile_result["기간컬럼"].values()), None)
    기간_str = f"{primary_period['최소']} ~ {primary_period['최대']}" if primary_period else "알 수 없음"

    # --- 확정 전 요약 박스 ---
    st.subheader("확정 전 요약")
    c1, c2 = st.columns(2)
    with c1:
        st.write(f"**대상 파일명**: {st.session_state.filename}")
        st.write(f"**판정 테이블**: {table_judgment['테이블명']}")
        st.write(f"**기간**: {기간_str}")
        st.write(f"**행수**: {profile_result['행수']:,}")
    with c2:
        st.write(f"**계산 대상 지표**: {len(계산대상)}개")
        st.write(f"**유효구간 확장 필요**: {len(확장필요_목록)}개")
        st.write(f"**부분 갱신**: {len(부분갱신_목록)}개")
        st.write(f"**카탈로그 생성일시**: {catalog_generated_at}")

    # 부분 갱신 경고 — 차단하지 않는다
    if 부분갱신_목록:
        names = ", ".join(m["metric_id"] for m in 부분갱신_목록)
        st.markdown(status_badge("부분 갱신 지표 있음", "경고"), unsafe_allow_html=True)
        st.caption(
            f"{names} — 이 지표들이 함께 쓰는 다른 테이블은 이번에 갱신되지 않습니다. "
            "계산은 진행되지만 최신 상태를 전부 반영한 값은 아닙니다."
        )

    # 유효구간 확장 승인 — 있을 때만 체크박스를 보여주고, 체크해야 버튼이 열린다.
    extend_approved = True
    if 확장필요_목록:
        st.markdown(status_badge("유효구간 확장 필요", "경고"), unsafe_allow_html=True)
        names = ", ".join(m["metric_id"] for m in 확장필요_목록)
        st.caption(f"{names} — 정의서에 선언된 유효구간을 벗어난 기간의 데이터입니다.")

        extend_approved = st.checkbox(
            "유효구간 확장을 승인합니다 (이번 실행에만 적용)",
            key="extend_approved",
        )
        st.caption(
            "체크해도 위키 정의서는 바뀌지 않습니다. 이번 실행에만 적용되며, "
            "이 사실은 리포트에 그대로 명시됩니다. 다음 실행에서는 다시 승인해야 합니다."
        )

    button_disabled = not extend_approved
    clicked = st.button("이 판정으로 계산 진행", disabled=button_disabled)
    if button_disabled:
        st.caption("유효구간 확장을 승인해야 진행할 수 있습니다.")

    if not clicked:
        return

    # --- 계산 먼저 시도한다. 인증 실패면 run 폴더를 만들기 전에 중단한다 ---
    # (실패한 실행을 outputs/에 남기면 나중에 "이게 진짜 확정인지 실패 흔적인지"
    # 헷갈리게 된다 — 성공했을 때만 기록을 만든다.)
    target_ids = [m["metric_id"] for m in 계산대상]
    df = st.session_state.df
    table_name = table_judgment["테이블명"]

    # 이 실행 하나를 식별하는 고유 id — 스테이징 테이블명과 run 폴더명에 똑같이
    # 붙인다. 계산 시도 "전에" 만드는 이유: 실패해도 흔적을 안 남긴다는 원칙과
    # 별개로, 스테이징 테이블 이름 자체가 이 id를 필요로 하기 때문이다.
    run_id = calculator.new_run_id()

    t0 = time.time()
    try:
        with st.spinner("스테이징 적재 중"):
            client = calculator.get_client()
            staging_table = calculator.load_staging_table(df, table_name, client, run_id)
        with st.spinner("지표 계산 중"):
            results, sql_log = calculator.compute_target_metrics(
                target_ids, table_name, staging_table,
                primary_period["최소"], primary_period["최대"],
                metrics_catalog, client, extend_approved,
            )
    except calculator.AuthError as e:
        st.error("BigQuery 인증에 실패해 계산을 중단했습니다.")
        st.write(f"원인: {e}")
        st.write("아래 명령으로 인증한 뒤 다시 확정 버튼을 눌러주세요.")
        st.code("gcloud auth application-default login", language="bash")
        return
    계산_소요 = round(time.time() - t0, 3)

    # --- 확정 처리(계산 성공 후에만) ---
    run_time = datetime.now()
    # 폴더명도 스테이징 테이블과 같은 run_id를 쓴다 — 분 단위 폴더명(예전 방식)은
    # 클라우드에서 여러 사용자가 같은 분에 확정하면 서로 폴더를 덮어쓸 수 있다.
    run_dir = APP_DIR / "outputs" / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 업로드 원본을 그대로 복사한다(CLAUDE.md 5-5 재현성): 나중에 이 실행을 그대로
    # 재현하려면 "그때 정확히 무엇을 넣었는지"가 파일로 남아 있어야 한다.
    if st.session_state.file_bytes is not None:
        (run_dir / st.session_state.filename).write_bytes(st.session_state.file_bytes)

    insights_catalog = load_insights_catalog()

    run_log = {
        # --- 기존 평평한 필드: report.py·email_draft.py가 run_context를
        # 만들 때 그대로 읽는다. 아래 "N_단계" 구조와 내용은 겹치지만, 그
        # 모듈들이 다시 고치지 않아도 되게 그대로 둔다.
        "파일명": st.session_state.filename,
        "행수": profile_result["행수"],
        "판정_테이블": table_judgment["테이블명"],
        "기간": 기간_str,
        "카탈로그_생성일시": catalog_generated_at,
        "계산_대상_지표": target_ids,
        "유효구간_확장_승인": bool(확장필요_목록) and extend_approved,
        "유효구간_확장_승인_시각": run_time.isoformat() if 확장필요_목록 else None,
        "부분_갱신_지표": [m["metric_id"] for m in 부분갱신_목록],
        "확정_시각": run_time.isoformat(),

        # --- 8단계 전체를 사람이 훑어볼 수 있는 실행 기록 ---
        "0_카탈로그": {
            "생성일시": catalog_generated_at,
            "지표_개수": metrics_catalog.get("_meta", {}).get("항목_개수", "?"),
            "테이블_개수": (load_catalogs()[1] or {}).get("_meta", {}).get("항목_개수", "?"),
            "인사이트_개수": insights_catalog.get("_meta", {}).get("항목_개수", "?"),
        },
        "1_투입": {
            "파일명": st.session_state.filename,
            "크기_바이트": len(st.session_state.file_bytes) if st.session_state.file_bytes else None,
            "행수": profile_result["행수"],
            "컬럼_수": profile_result["컬럼수"],
            "업로드_시각": st.session_state.upload_time,
        },
        "2_판정": {
            "테이블": table_judgment["테이블명"],
            "일치율": table_judgment["일치율"],
            "기간": profile_result["기간컬럼"],
            "결측": profile_result["결측"],
            "그레인_후보": profile_result["그레인_후보"],
        },
        "2_계산": {
            "지표별_값_상태": {r.metric_id: {"값": r.value, "상태": r.status} for r in results},
            "스테이징_테이블명": staging_table,
            "계산_시각": run_time.isoformat(),
        },
        "게이트1": {
            "확정_시각": run_time.isoformat(),
            "유효구간_확장_승인_여부": bool(확장필요_목록) and extend_approved,
        },
        "실행환경": _environment_info(),
    }

    st.session_state.stage_durations["2_계산"] = 계산_소요
    run_log["소요시간"] = dict(st.session_state.stage_durations)

    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    calculator.save_metrics_csv(results, run_dir)

    # --- 전월 대비 (계산이 끝나면 자동으로 이어서 실행) ---
    prev_period = comparator.previous_month(primary_period["최소"])
    with st.spinner(f"전월({prev_period}) 계산 중"):
        current_metric_df = comparator.metric_results_to_df(results)
        prev_df = comparator.calc_previous(target_ids, prev_period, client)
        comparison_df = comparator.compare(current_metric_df, prev_df, metrics_catalog)
    comparison_df.to_csv(run_dir / "comparison.csv", index=False, encoding="utf-8-sig")

    # 이력 테이블 적재는 부가 기록이라, 실패해도(예: 권한 문제) 오늘 확정
    # 자체를 막지는 않는다 — 이미 run_dir·csv로 로컬 기록은 남아 있다.
    try:
        calculator.save_run_history(comparison_df, run_id, table_name, primary_period["최소"], client)
    except calculator.AuthError as e:
        st.warning(f"실행 이력을 BigQuery에 남기지 못했습니다(계산 결과는 정상): {e}")

    # --- 검증 (전월 대비까지 끝나면 자동으로 이어서 실행) ---
    metrics_df = pd.DataFrame([{
        "metric_id": r.metric_id, "지표명": r.지표명, "유형": r.유형, "month": r.month,
        "value": r.value, "sample_size": r.sample_size, "min_sample": r.min_sample,
        "status": r.status, "부분갱신": r.부분갱신, "원천": "+".join(r.원천), "error": r.error or "",
    } for r in results])
    table_override = {table_name: staging_table}
    uploaded_months = {calculator.parse_year_month(primary_period["최소"])}
    t0 = time.time()
    with st.spinner("검증 실행 중"):
        validation_result = validator.validate_all(
            metrics_df, comparison_df, metrics_catalog, client,
            override=extend_approved, table_override=table_override,
            uploaded_months=uploaded_months,
        )
    st.session_state.stage_durations["3_검증"] = round(time.time() - t0, 3)
    (run_dir / "validation.json").write_text(
        json.dumps(validation_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _update_run_log(run_dir, **{"3_검증": validation_result, "소요시간": dict(st.session_state.stage_durations)})

    st.session_state.metric_results = results
    st.session_state.sql_log = sql_log
    st.session_state.comparison_df = comparison_df
    st.session_state.prev_period = prev_period
    st.session_state.staging_table = staging_table
    st.session_state.validation_result = validation_result
    st.session_state.run_dir = str(run_dir)
    st.session_state.step = 4  # 3단계(검증) 완료 -> 4단계로. 4단계는 상단만 구현됨
    st.rerun()


# ---------------------------------------------------------------------------
# 계산 결과 — 2단계 후반 (게이트 확정 직후 자동 실행된 결과)
# ---------------------------------------------------------------------------

def render_calculation_results():
    results = st.session_state.metric_results
    if not results:
        return

    metrics_catalog, _ = load_catalogs()

    st.divider()
    st.header("계산 결과")

    성공 = [r for r in results if r.status not in ("계산오류", "유효구간 밖")]
    구간확장 = [r for r in results if r.status == "구간확장"]
    오류 = [r for r in results if r.status == "계산오류"]

    c1, c2, c3 = st.columns(3)
    c1.metric("계산 성공", f"{len(성공)}종")
    c2.metric("구간확장", f"{len(구간확장)}종")
    c3.metric("오류", f"{len(오류)}종")

    header_cols = st.columns([3, 2, 2, 2, 3])
    for c, label in zip(header_cols, ["지표명", "값", "표본", "상태", "원천"]):
        c.markdown(f"**{label}**")

    for r in results:
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 3])
        name = r.지표명
        if r.부분갱신:
            name += " 🔹"  # 부분 갱신 표시(작은 마크). 툴팁 대신 캡션으로 아래에 설명
        c1.write(name)
        c2.write(format_metric_value(r.metric_id, r.value, metrics_catalog))
        c3.write(f"{r.sample_size:,.0f}" if r.sample_size is not None else "—")
        c4.markdown(status_badge(r.status, r.status), unsafe_allow_html=True)
        c5.write("+".join(r.원천))
        if r.status == "계산오류" and r.error:
            st.caption(f"　↳ {r.metric_id}: {r.error}")

    if any(r.부분갱신 for r in results):
        st.caption("🔹 부분 갱신 — 이 지표가 함께 쓰는 다른 테이블은 이번에 갱신되지 않았습니다.")

    # --- 생성된 SQL (스테이징 치환이 실제로 적용됐는지 눈으로 확인하는 용도) ---
    sql_log = st.session_state.sql_log or []
    if sql_log:
        staging_hits = sum(1 for entry in sql_log if entry["스테이징_치환"])
        with st.expander(f"생성된 SQL 보기 ({len(sql_log)}건, 스테이징 치환 {staging_hits}건)"):
            for entry in sql_log:
                배지 = status_badge(
                    "스테이징 치환됨" if entry["스테이징_치환"] else "실물 테이블 그대로",
                    "완료" if entry["스테이징_치환"] else "대기",
                )
                st.markdown(
                    f"**{entry['metric_id']}** — {entry['month']}&nbsp;&nbsp;{배지}",
                    unsafe_allow_html=True,
                )
                st.code(entry["sql"], language="sql")


# ---------------------------------------------------------------------------
# 전월 대비 — 2단계 계산 결과 바로 아래
# ---------------------------------------------------------------------------

def render_comparison():
    df = st.session_state.comparison_df
    if df is None:
        return

    metrics_catalog, _ = load_catalogs()

    st.divider()
    st.header(f"전월 대비 ({st.session_state.prev_period} → 이번 기간)")
    st.caption(
        "증가/감소는 방향만 표시합니다. 그게 좋은 신호인지 나쁜 신호인지는 "
        "여기서 판단하지 않습니다 — 해석은 리포트 단계에서 다룹니다."
    )

    header_cols = st.columns([3, 2, 2, 3, 2])
    for c, label in zip(header_cols, ["지표명", "당월", "전월", "변화", "변화율"]):
        c.markdown(f"**{label}**")

    for _, row in df.iterrows():
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 3, 2])
        c1.write(row["지표명"])

        if row["비교상태"] == "비교 불가":
            c2.write("—")
            c3.write("—")
            c4.markdown(status_badge("비교 불가", "데이터 없음"), unsafe_allow_html=True)
            c5.write(row.get("이유") or "")
            continue

        c2.write(format_metric_value(row["metric_id"], row["당월"], metrics_catalog))
        c3.write(format_metric_value(row["metric_id"], row["전월"], metrics_catalog))

        # 변화 칸 — 비율 지표는 %p, 나머지는 절대값 + 방향 화살표
        pp = row.get("퍼센트포인트변화")
        if pp is not None and pd.notna(pp):
            c4.write(f"{pp:+.1f}%p")
        else:
            절대변화 = row["절대변화"]
            화살표 = "▲" if 절대변화 > 0 else ("▼" if 절대변화 < 0 else "→")
            c4.write(f"{화살표} {format_metric_value(row['metric_id'], abs(절대변화), metrics_catalog)}")

        # 변화율 칸 — 부호에 따라 색만 다르게. 지표 성격(증가가 좋은지 나쁜지)은
        # 여기서 절대 판단하지 않는다. emerald/rose는 "방향 표시"일 뿐이다.
        율 = row["상대변화율"]
        if 율 is None or pd.isna(율):
            c5.write("—")
        elif 율 > 0:
            c5.markdown(status_badge(f"+{율:.1f}%", "증가"), unsafe_allow_html=True)
        elif 율 < 0:
            c5.markdown(status_badge(f"{율:.1f}%", "감소"), unsafe_allow_html=True)
        else:
            c5.markdown(status_badge("0.0%", "변동없음"), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 3단계 — 검증 실행
# ---------------------------------------------------------------------------

def _render_validation_findings(result):
    """검증 결과 표 + "자동 검증하지 않은 항목" 박스. 3단계와 4단계(대시보드
    하단 expander)가 이 렌더링을 공유한다 — 데이터는 하나(validation_result)인데
    화면이 두 곳에 있는 이유는 아래 render_validation()/render_dashboard_top()의
    주석 참고."""
    st.markdown(
        status_badge(
            f"{result['전체판정']}  (차단 {result['차단수']} / 경고 {result['경고수']})",
            result["전체판정"],
        ),
        unsafe_allow_html=True,
    )

    findings = result["항목별_결과"]
    flagged = [f for f in findings if f["판정"] != "통과"]
    passed = [f for f in findings if f["판정"] == "통과"]

    if flagged:
        header_cols = st.columns([2, 3, 1, 6])
        for c, label in zip(header_cols, ["검증", "대상", "판정", "상세"]):
            c.markdown(f"**{label}**")
        for f in flagged:
            c1, c2, c3, c4 = st.columns([2, 3, 1, 6])
            c1.write(f["검증명"])
            c2.write(f["대상지표"])
            c3.markdown(status_badge(f["판정"], f["판정"]), unsafe_allow_html=True)
            c4.write(f["상세"])

    if passed:
        with st.expander(f"통과 항목 {len(passed)}건 (접힘)"):
            for f in passed:
                st.write(f"- **{f['검증명']}** / {f['대상지표']}: {f['상세']}")

    st.subheader("자동 검증하지 않은 항목")
    items_html = "".join(
        f"<li><strong>{s['항목']}</strong> — {s['이유']}</li>"
        for s in result["자동검증하지_않은_것"]
    )
    st.markdown(
        f'<div style="background-color:{COLOR_SLATE}15;border:1px solid {COLOR_SLATE};'
        f'border-radius:8px;padding:12px 16px;color:{COLOR_SLATE};">'
        f"<strong>이 검증은 수행되지 않았습니다</strong><ul>{items_html}</ul></div>",
        unsafe_allow_html=True,
    )
    st.caption("이 항목들은 판단이 필요해 자동화하지 않았습니다. 리포트 한계 절에 명시됩니다.")


def render_validation():
    """3단계 화면 — 검증 전용. "문제를 찾는 곳"이다. 확정 직후 사람이 여기서
    차단/경고를 먼저 확인한다."""
    result = st.session_state.validation_result
    if result is None:
        return

    st.divider()
    st.header("3단계 — 검증 실행")
    _render_validation_findings(result)


# ---------------------------------------------------------------------------
# 4단계 — 대시보드 (상단: 한눈에 + 핵심 지표 카드 4개)
# ---------------------------------------------------------------------------

KPI_METRICS = ["billed_revenue", "active_customers_contract", "arpu", "avg_data_usage"]


def _get_current_value(metric_id, metrics_catalog, client):
    for r in (st.session_state.metric_results or []):
        if r.metric_id == metric_id:
            return r.value
    # 이번 실행의 계산 대상이 아니었던 지표(예: active_customers_contract) —
    # 같은 조건(스테이징·승인)으로 즉석에서 다시 계산한다.
    table_judgment = st.session_state.table_judgment
    table_name = table_judgment["테이블명"]
    staging_table = st.session_state.staging_table
    table_override = {table_name: staging_table} if staging_table else {}
    primary_period = next(iter(st.session_state.profile_result["기간컬럼"].values()))
    year, month = calculator.parse_year_month(primary_period["최소"])
    result = calculator.compute_metric(
        metric_id, year, month, metrics_catalog, client, config.BQ_DATASET,
        table_override, {(year, month)}, st.session_state.extend_approved, {}, [],
    )
    return result.value


def _get_previous_value(metric_id, client):
    cdf = st.session_state.comparison_df
    if cdf is not None:
        match = cdf[cdf["metric_id"] == metric_id]
        if len(match) > 0 and match.iloc[0]["비교상태"] == "OK":
            return match.iloc[0]["전월"]
    prev_df = comparator.calc_previous([metric_id], st.session_state.prev_period, client)
    row = prev_df.iloc[0]
    return row["값"] if row["상태"] in ("OK", "구간확장") else None


def render_dashboard_top():
    result = st.session_state.validation_result
    if result is None:
        return

    st.divider()
    st.header("4단계 — 대시보드")

    if result["전체판정"] == "차단":
        # CLAUDE.md 9절: 차단을 우회하는 버튼을 만들지 않는다. 이유를 보여주는
        # 것까지가 이 화면의 역할이고, 그다음은 위키/데이터를 고쳐서 다시
        # 확정하는 것뿐이다.
        st.markdown(status_badge("차단됨 — 대시보드를 표시할 수 없습니다", "차단"), unsafe_allow_html=True)
        blocking = [f for f in result["항목별_결과"] if f["판정"] == "차단"]
        for f in blocking:
            st.write(f"- **{f['검증명']}** / {f['대상지표']}: {f['상세']}")
        return

    metrics_catalog, _ = load_catalogs()
    client = calculator.get_client()

    # --- 한눈에 ---
    st.markdown(
        status_badge(
            f"{result['전체판정']} — 차단 {result['차단수']} / 경고 {result['경고수']}",
            result["전체판정"],
        ),
        unsafe_allow_html=True,
    )

    이상신호 = [f for f in result["항목별_결과"] if f["검증명"] == "전월 대비" and f["판정"] == "경고"]
    if 이상신호:
        st.write("이상 신호:")
        for f in 이상신호:
            수치 = f["상세"].split(" ")[0]
            st.write(f"- {f['대상지표']} {수치}")

    primary_period = next(iter(st.session_state.profile_result["기간컬럼"].values()))
    catalog_generated_at = metrics_catalog.get("_meta", {}).get("생성일시", "알 수 없음")
    st.caption(
        f"대상 기간: {primary_period['최소']} ~ {primary_period['최대']}  ·  "
        f"카탈로그 생성일시: {catalog_generated_at}"
    )

    # --- 핵심 지표 카드 4개 ---
    cols = st.columns(4)
    for col, metric_id in zip(cols, KPI_METRICS):
        지표명 = metrics_catalog.get(metric_id, {}).get("지표명", metric_id)
        cur = _get_current_value(metric_id, metrics_catalog, client)
        prev = _get_previous_value(metric_id, client)
        delta = None if (cur is None or prev is None) else cur - prev

        # 해석 문장 없이 값만 보여준다 — "좋다/나쁘다"는 여기서 판단하지 않는다.
        col.metric(
            지표명,
            format_metric_value_compact(metric_id, cur, metrics_catalog),
            format_delta_value(metric_id, delta, metrics_catalog),
            delta_color="normal",
        )

        existing = next((r for r in (st.session_state.metric_results or []) if r.metric_id == metric_id), None)
        if existing and existing.부분갱신:
            col.caption("🔹 부분 갱신 — 함께 쓰는 다른 테이블은 이번에 갱신되지 않음")

    # --- 전체 지표 표 (핵심 카드 포함) ---
    st.subheader("전체 지표")
    _render_full_metrics_table(metrics_catalog)

    # --- 차트 영역 ---
    st.subheader("추이")
    _render_trend_charts(client)

    # --- 이번 실행에서 계산하지 않은 지표 ---
    irrelevant = [m for m in (st.session_state.metric_judgments or []) if m["상태"] == "이 파일과 무관"]
    if irrelevant:
        with st.expander(f"이번 실행에서 계산하지 않은 지표 {len(irrelevant)}종"):
            for m in irrelevant:
                st.write(f"- **{m['metric_id']}** ({m['지표명']}) — {m['이유']}")

    # --- 부분 갱신 안내 ---
    partial = [r for r in (st.session_state.metric_results or []) if r.부분갱신]
    if partial:
        table_name = st.session_state.table_judgment["테이블명"]
        names = ", ".join(sorted({r.metric_id for r in partial}))
        other_tables = sorted({t for r in partial for t in r.원천 if t != table_name})
        st.markdown(
            f'<div style="background-color:{COLOR_SLATE}15;border:1px solid {COLOR_SLATE};'
            f'border-radius:8px;padding:10px 14px;color:{COLOR_SLATE};">'
            f"<strong>{names}</strong>는 {', '.join(other_tables)} 테이블을 함께 사용합니다. "
            f"업로드된 것은 {table_name}뿐이므로 {', '.join(other_tables)}는 이전 상태를 반영합니다."
            f"</div>",
            unsafe_allow_html=True,
        )

    # --- 검증 상세 (3단계 결과를 여기서도 확인) ---
    # 3단계와 중복이 아닌 이유: 3단계는 "문제를 찾는" 검증 전용 화면이고, 여기(4단계)는
    # "전체를 확인하는" 결과 화면이다. 게이트 2("내용·검증 결과 확인")에서 사람이
    # 판단할 때 대시보드 하나만 보고도 검증 상태까지 같이 보여야 하므로, 데이터는
    # 하나(validation_result)를 재사용하되 진입점을 두 곳에 둔다.
    with st.expander("검증 상세"):
        _render_validation_findings(result)


CHART_METRIC_IDS = [
    "billed_revenue", "billed_revenue_active",
    "avg_data_usage", "low_usage_customer_rate",
    "active_customers_contract", "active_users_product",
]


@st.cache_data(show_spinner=False)
def _cached_trend(metric_ids, end_period, months, staging_map_items, extend_approved, _client):
    """build_trend()를 st.cache_data로 감싼다.

    on_progress 콜백을 여기서 받지 않는 이유(실측으로 잡은 버그): 처음 시도는
    콜백이 st.progress 위젯을 매 단계 갱신하게 했는데, **캐시 히트가 나는 두
    번째 실행부터 Streamlit이 "레이아웃 블록 재생" 에러로 죽었다**
    (`CacheReplayClosureError`). st.cache_data는 캐시 히트 시 "그때 그 UI
    호출들을 다시 재생"하려고 하는데, 콜백이 건드리는 progress_bar는 이 함수
    바깥(호출하는 쪽)에서 만들어진 위젯이라 재생 시점에 그 자리가 없다.
    그래서 이 함수 안에서는 어떤 Streamlit UI 요소도 건드리지 않는다 — 진행
    표시는 호출하는 쪽에서 st.spinner로 감싸는 것으로 대체했다(세부 %는
    캐시와 공존할 수 없어 포기했다).

    _client는 이름 앞에 밑줄을 붙여 해시 대상에서 뺀다 — BigQuery 클라이언트는
    원래 해시할 수 없는 객체다.
    """
    staging_map = dict(staging_map_items)
    return charter.build_trend(
        list(metric_ids), end_period, months, staging_map, _client,
        extend_approved=extend_approved, on_progress=None,
    )


def _range_with_margin(values, margin_ratio=0.1):
    clean = [v for v in values if v is not None and v == v]  # NaN 제외
    if not clean:
        return None
    lo, hi = min(clean), max(clean)
    if lo == hi:
        pad = abs(lo) * margin_ratio or 1
        return [lo - pad, hi + pad]
    pad = (hi - lo) * margin_ratio
    return [lo - pad, hi + pad]


def _add_series(fig, trend_df, metric_id, 지표명, color, end_period, yaxis="y"):
    sub = trend_df[trend_df["metric_id"] == metric_id].sort_values("month")
    x = sub["month"].tolist()
    y = sub["value"].tolist()  # 유효구간 밖은 이미 None -> Plotly가 그 지점에서 선을 끊는다
    sizes = [14 if m == end_period else 7 for m in x]
    fig.add_trace(go.Scatter(
        x=x, y=y, name=지표명, mode="lines+markers",
        line=dict(color=color, width=2),
        marker=dict(size=sizes, color=color),
        connectgaps=False,  # 결측을 0으로 잇지 않는다 — 반드시 선이 끊겨야 한다
        yaxis=yaxis,
    ))
    return y


def _render_trend_charts(client):
    """차트 3종. D-2 세 규칙: ① 추세선 없음(6개월로 계절성 판별 불가) ② 유효구간
    밖은 None+connectgaps=False로 결측 처리(0으로 채우지 않음) ③ 색은 DESIGN.md
    Tailwind 500 팔레트(COLOR_SERIES_A/B) — 상태색(emerald/rose/amber)은 "판단"을
    담고 있어 순수 데이터 계열에 쓰면 안 된다."""
    table_judgment = st.session_state.table_judgment
    table_name = table_judgment["테이블명"]
    staging_table = st.session_state.staging_table
    staging_map = {table_name: staging_table} if staging_table else {}
    end_period = next(iter(st.session_state.profile_result["기간컬럼"].values()))["최소"]
    extend_approved = st.session_state.extend_approved
    metrics_catalog, _ = load_catalogs()

    # 캐시 히트면 즉시 끝나고, 캐시 미스면 실제로 BigQuery를 여러 번 호출하느라
    # 오래 걸린다 — 세부 진행률(%)은 캐시와 같이 못 쓰므로(위 _cached_trend
    # 문서 참고) 스피너로만 표시한다.
    with st.spinner("추이 계산 중"):
        trend_df = _cached_trend(
            tuple(CHART_METRIC_IDS), end_period, config.TREND_MONTHS,
            tuple(sorted(staging_map.items())), extend_approved,
            client,
        )

    caption_common = f"{end_period}은 업로드 파일로 계산, 그 이전은 기존 테이블"

    # --- 차트 1: 매출 추이 (단일 축, 원) ---
    st.markdown("**매출 추이**")
    fig1 = go.Figure()
    y1 = _add_series(fig1, trend_df, "billed_revenue", "청구 매출", COLOR_SERIES_A, end_period)
    y2 = _add_series(fig1, trend_df, "billed_revenue_active", "활성 계약 청구 매출", COLOR_SERIES_B, end_period)
    y_range = _range_with_margin(y1 + y2)
    if y_range:
        fig1.update_yaxes(range=y_range, title="원")
    fig1.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
    st.plotly_chart(fig1, width='stretch')
    st.caption(
        f"{caption_common}. y축 범위 {y_range[0]:,.0f}~{y_range[1]:,.0f}원으로 명시(0부터 시작 안 함)."
        if y_range else caption_common
    )

    # --- 차트 2: 사용량 · 저사용 비율 (이중 축) ---
    st.markdown("**사용량 · 저사용 고객 비율 (이중 축)**")
    fig2 = go.Figure()
    y_gb = _add_series(fig2, trend_df, "avg_data_usage", "평균 데이터 사용량(GB)",
                        COLOR_SERIES_A, end_period, yaxis="y")
    y_pct_raw = trend_df[trend_df["metric_id"] == "low_usage_customer_rate"].sort_values("month")["value"]
    y_pct = [None if v is None or v != v else v * 100 for v in y_pct_raw]
    x_pct = trend_df[trend_df["metric_id"] == "low_usage_customer_rate"].sort_values("month")["month"].tolist()
    sizes_pct = [14 if m == end_period else 7 for m in x_pct]
    fig2.add_trace(go.Scatter(
        x=x_pct, y=y_pct, name="저사용 고객 비율(%)", mode="lines+markers",
        line=dict(color=COLOR_SERIES_B, width=2),
        marker=dict(size=sizes_pct, color=COLOR_SERIES_B),
        connectgaps=False, yaxis="y2",
    ))
    gb_range = _range_with_margin(y_gb)
    pct_range = _range_with_margin(y_pct)
    fig2.update_layout(
        height=320, margin=dict(l=10, r=10, t=10, b=10), showlegend=True,
        yaxis=dict(title="GB", range=gb_range),
        yaxis2=dict(title="%", overlaying="y", side="right", range=pct_range),
    )
    st.plotly_chart(fig2, width='stretch')
    st.caption(
        f"좌축 GB / 우축 % — 축이 다르므로 두 선의 교차점은 의미가 없습니다. {caption_common}"
    )

    # --- 차트 3: 활성 고객 추이 (단일 축, 명) ---
    st.markdown("**활성 고객 추이 (계약 기준 vs 사용 기준)**")
    fig3 = go.Figure()
    y3 = _add_series(fig3, trend_df, "active_customers_contract", "계약 기준 활성 고객",
                      COLOR_SERIES_A, end_period)
    y4 = _add_series(fig3, trend_df, "active_users_product", "사용 기준 활성 사용자",
                      COLOR_SERIES_B, end_period)
    y_range3 = _range_with_margin(y3 + y4)
    if y_range3:
        fig3.update_yaxes(range=y_range3, title="명")
    fig3.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
    st.plotly_chart(fig3, width='stretch')
    st.caption(
        f"계약 기준(customers.churn_date)과 사용 기준(로그인 여부)은 정의가 달라 값이 갈립니다. {caption_common}"
    )


def _render_full_metrics_table(metrics_catalog):
    """지표명/당월/전월/변화/변화율/표본/상태 7열 표. 행 배경색(변화율 임계값
    초과 시 amber)이 필요해서 st.columns 대신 커스텀 HTML 표로 그린다 —
    DESIGN.md에 이미 있는 방식과 같다(st.dataframe은 셀 단위 CSS를 못 먹인다)."""
    cdf = st.session_state.comparison_df
    if cdf is None:
        return

    results_by_id = {r.metric_id: r for r in (st.session_state.metric_results or [])}
    threshold = config.MOM_THRESHOLD

    header = "".join(
        f"<th style='text-align:left;padding:6px 10px;border-bottom:1px solid #ddd;'>{h}</th>"
        for h in ["지표명", "당월", "전월", "변화", "변화율", "표본", "상태"]
    )

    rows_html = []
    for _, row in cdf.iterrows():
        metric_id = row["metric_id"]
        mr = results_by_id.get(metric_id)

        지표명 = row["지표명"]
        if mr and mr.부분갱신:
            other = ", ".join(t for t in mr.원천 if t != st.session_state.table_judgment["테이블명"])
            지표명 += (
                f' <span title="이 지표는 {other} 테이블도 함께 씁니다. '
                f'이번 업로드에는 포함되지 않아 이전 상태를 반영합니다." '
                f'style="cursor:help;">🔹</span>'
            )

        표본 = f"{mr.sample_size:,.0f}" if (mr and mr.sample_size is not None) else "—"
        상태 = mr.status if mr else "—"
        상태_배지 = status_badge(상태, 상태)

        강조 = False
        if row["비교상태"] == "비교 불가":
            당월_s = 전월_s = 변화_s = "—"
            변화율_s = status_badge("비교 불가", "데이터 없음")
        else:
            당월_s = format_metric_value(metric_id, row["당월"], metrics_catalog)
            전월_s = format_metric_value(metric_id, row["전월"], metrics_catalog)

            pp = row.get("퍼센트포인트변화")
            if pp is not None and pd.notna(pp):
                변화_s = f"{pp:+.1f}%p"
            else:
                절대 = row["절대변화"]
                화살표 = "▲" if 절대 > 0 else ("▼" if 절대 < 0 else "→")
                변화_s = f"{화살표} {format_metric_value(metric_id, abs(절대), metrics_catalog)}"

            율 = row["상대변화율"]
            if 율 is None or pd.isna(율):
                변화율_s = "—"
            else:
                라벨 = "증가" if 율 > 0 else ("감소" if 율 < 0 else "변동없음")
                변화율_s = status_badge(f"{율:+.1f}%", 라벨)
                강조 = abs(율) >= threshold

        bg = "background-color:#f59e0b1a;" if 강조 else ""
        cells = [지표명, 당월_s, 전월_s, 변화_s, 변화율_s, 표본, 상태_배지]
        rows_html.append(
            f"<tr style='{bg}'>"
            + "".join(f"<td style='padding:6px 10px;border-bottom:1px solid #eee;'>{c}</td>" for c in cells)
            + "</tr>"
        )

    table_html = (
        "<table style='width:100%;border-collapse:collapse;font-size:0.9em;'>"
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 5단계 — 내용·검증 결과 확인 (두 번째 게이트)
# ---------------------------------------------------------------------------

CHECKLIST_ITEMS = [
    "계산 대상 기간이 의도한 기간인가",
    "경고 항목을 확인했는가",
    "부분 갱신 지표의 한계를 이해했는가",
    "자동 검증하지 않은 항목을 인지했는가",
]


def render_step5():
    """5단계 — 사람 확인 체크리스트 + 두 번째 게이트.

    3단계(검증)는 기계가 문제를 찾는 단계였다. 5단계는 그 결과를 사람이 실제로
    봤는지 확인하는 단계다 — 체크박스 자체가 뭔가를 다시 검증하지는 않는다.
    이미 3·4단계에서 나온 사실(경고 건수, 부분 갱신 지표, 자동검증 제외 목록)을
    사람이 인지했다는 걸 스스로 표시하는 게이트다.
    """
    result = st.session_state.validation_result
    if result is None:
        return

    st.divider()
    st.header("5단계 — 내용·검증 결과 확인")

    if result["전체판정"] == "차단":
        st.markdown(status_badge("차단됨 — 5단계로 진행할 수 없습니다", "차단"), unsafe_allow_html=True)
        st.write("3단계 검증에서 차단 사유가 해결되기 전까지 다음 단계로 진행할 수 없습니다.")
        return

    if st.session_state.step5_confirmed_at:
        st.markdown(status_badge("확인 완료", "완료"), unsafe_allow_html=True)
        st.write(f"확인 완료 시각: {st.session_state.step5_confirmed_at}")
        st.caption("이미 만들어진 run_log 기록은 취소해도 지워지지 않습니다.")
        if st.button("확인 취소"):
            st.session_state.step5_confirmed_at = None
            st.session_state.step = 5
            st.rerun()
        return

    primary_period = next(iter(st.session_state.profile_result["기간컬럼"].values()))
    기간_str = f"{primary_period['최소']} ~ {primary_period['최대']}"

    warn_findings = [f for f in result["항목별_결과"] if f["판정"] == "경고" and f["검증명"] == "전월 대비"]
    partial = [r for r in (st.session_state.metric_results or []) if r.부분갱신]
    table_name = st.session_state.table_judgment["테이블명"]
    other_tables = sorted({t for r in partial for t in r.원천 if t != table_name})

    st.write("아래 4가지를 실제로 확인한 뒤 체크해주세요.")

    c1a, c1b = st.columns([1, 20])
    c1a.checkbox(" ", key="chk_period", label_visibility="collapsed")
    with c1b:
        st.write(f"**계산 대상 기간이 의도한 기간인가** ({기간_str})")

    c2a, c2b = st.columns([1, 20])
    c2a.checkbox(" ", key="chk_warnings", label_visibility="collapsed")
    with c2b:
        st.write(f"**경고 항목을 확인했는가** ({len(warn_findings)}건)")
        if warn_findings:
            with st.expander("경고 내역 보기"):
                for f in warn_findings:
                    st.write(f"- **{f['대상지표']}**: {f['상세']}")

    c3a, c3b = st.columns([1, 20])
    c3a.checkbox(" ", key="chk_partial", label_visibility="collapsed")
    with c3b:
        if partial:
            names = ", ".join(sorted({r.metric_id for r in partial}))
            st.write(f"**부분 갱신 지표의 한계를 이해했는가** ({names}는 {', '.join(other_tables)} 이전 상태)")
        else:
            st.write("**부분 갱신 지표의 한계를 이해했는가** (이번 실행에는 부분 갱신 지표 없음)")

    c4a, c4b = st.columns([1, 20])
    c4a.checkbox(" ", key="chk_skipped", label_visibility="collapsed")
    with c4b:
        st.write("**자동 검증하지 않은 항목을 인지했는가**")
        with st.expander("자동 검증하지 않은 항목 보기"):
            for s in result["자동검증하지_않은_것"]:
                st.write(f"- **{s['항목']}** — {s['이유']}")

    all_checked = all([
        st.session_state.chk_period, st.session_state.chk_warnings,
        st.session_state.chk_partial, st.session_state.chk_skipped,
    ])

    clicked = st.button("확인 완료, 리포트 생성 단계로", disabled=not all_checked)
    if not all_checked:
        st.caption("4가지를 모두 체크해야 다음 단계로 진행할 수 있습니다.")

    if not clicked:
        return

    run_time = datetime.now()
    run_dir = Path(st.session_state.run_dir)
    run_log_path = run_dir / "run_log.json"
    run_log = json.loads(run_log_path.read_text(encoding="utf-8")) if run_log_path.exists() else {}

    run_log["확인_완료_시각"] = run_time.isoformat()
    run_log["확인한_체크항목"] = CHECKLIST_ITEMS
    run_log["검증_요약"] = {
        "전체판정": result["전체판정"],
        "차단수": result["차단수"],
        "경고수": result["경고수"],
    }
    run_log["4_5_확인"] = {
        "게이트2_확정_시각": run_time.isoformat(),
        "확인한_체크항목": CHECKLIST_ITEMS,
    }
    run_log_path.write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    st.session_state.step5_confirmed_at = run_time.isoformat()
    st.session_state.step = 6  # 6단계는 아직 미구현 -> 사이드바에는 "준비됨"으로 표시됨
    st.rerun()


# ---------------------------------------------------------------------------
# 6단계 — 리포트 생성 (게이트 2 통과 후)
# ---------------------------------------------------------------------------

CHAPTER_NAMES = {"1": "Executive Summary", "2": "배경·목적", "3": "데이터·방법론",
                  "4": "현황", "5": "원인 분석", "6": "개선 제안", "7": "한계", "8": "부록"}


def _build_run_context(run_dir: Path) -> dict:
    """report.py·email_draft.py가 공유하는 run_context를 만든다. 두 모듈이
    같은 계산 결과·검증 결과를 봐야 이메일이 리포트와 다른 숫자를 말하는
    일이 없다 — 이 딱 하나의 dict를 두 곳에 그대로 넘긴다."""
    metrics_catalog, schema_catalog = load_catalogs()
    insights_catalog = load_insights_catalog()

    run_log = json.loads((run_dir / "run_log.json").read_text(encoding="utf-8"))
    metrics_df = pd.read_csv(run_dir / "metrics.csv")

    return {
        "파일명": run_log.get("파일명"),
        "판정테이블": run_log.get("판정_테이블"),
        "기간": run_log.get("기간"),
        "행수": run_log.get("행수"),
        "metrics": metrics_df,
        "comparison": st.session_state.comparison_df,
        "validation": st.session_state.validation_result,
        "metrics_catalog": metrics_catalog,
        "schema_catalog": schema_catalog,
        "insights_catalog": insights_catalog,
        "run_log": run_log,
        "run_dir": str(run_dir),
    }


AUTO_GENERATED_CHAPTERS = ["1", "3", "4", "7", "8"]  # report.py가 항상 자동으로 채우는 장(구조상 고정)


def _generate_report(run_dir: Path):
    """report.py를 호출해 리포트를 만들고 파일로 저장한 뒤 session_state에
    반영한다. "리포트 생성" 버튼과 "사람 작성분 저장" 버튼(저장 후 자동
    재생성) 둘 다 같은 생성 로직을 타야 하므로 한 곳에 모아둔다 — 따로
    두면 둘 중 하나만 고치고 다른 쪽을 잊는 일이 생긴다."""
    run_context = _build_run_context(run_dir)

    t0 = time.time()
    with st.spinner("리포트 생성 중"):
        result = reporter.build_report(run_context)
    소요 = round(time.time() - t0, 3)

    report_path = run_dir / "report.md"
    report_path.write_text(result["report_md"], encoding="utf-8")

    st.session_state.report_md = result["report_md"]
    st.session_state.report_path = str(report_path)
    st.session_state.report_substituted = result["치환된_장"]
    st.session_state.report_remaining = result["미작성_장"]
    st.session_state.report_warnings = result["경고"]

    # PDF는 마크다운이 이미 성공한 뒤의 "덤"이다 — 폰트 파일이 없거나 fpdf2가
    # 없어도 마크다운 리포트 자체는 그대로 살려야 한다(CLAUDE.md 7절 하드
    # 제약을 지키다 실패해도 6단계 전체를 막을 이유는 아니다).
    pdf_error = None
    with st.spinner("PDF 생성 중"):
        try:
            pdf_bytes = reporter.build_pdf(result["report_md"])
            (run_dir / "report.pdf").write_bytes(pdf_bytes)
        except Exception as e:  # noqa: BLE001
            pdf_error = str(e)
    if pdf_error:
        st.warning(f"PDF 생성에 실패해 마크다운만 저장했습니다: {pdf_error}")

    st.session_state.stage_durations["6_리포트"] = 소요
    _update_run_log(run_dir, **{
        "6_리포트": {
            "생성_시각": datetime.now().isoformat(),
            "자동생성_장_목록": AUTO_GENERATED_CHAPTERS,
            "미작성_장_목록": result["미작성_장"],
            "금지표현_검사_결과": result["금지표현_검사"],
            "PDF_생성됨": pdf_error is None,
            "PDF_오류": pdf_error,
        },
        "소요시간": dict(st.session_state.stage_durations),
    })


def render_step6():
    """6단계 — pipeline/report.py로 마크다운 리포트를 만들어 outputs/run_*/report.md에
    저장하고 화면에 그대로 렌더링한다. PDF·다운로드 버튼은 다음 프롬프트 범위다.

    게이트 2(5단계)를 통과했는지는 step5_confirmed_at만 보고 판단한다 — 5단계
    자체가 이미 "차단이면 체크리스트를 보여주지 않는다"로 막아뒀으므로, 여기서
    같은 조건을 또 검사하지 않는다(우회 버튼을 만들지 않는다는 원칙과 같은
    이유로, 이 단계에 별도의 차단 로직을 두면 5단계와 판정이 어긋날 수 있다).
    """
    if not st.session_state.step5_confirmed_at:
        return

    st.divider()
    st.header("6단계 — 리포트 생성")

    run_dir = Path(st.session_state.run_dir)
    run_log = json.loads((run_dir / "run_log.json").read_text(encoding="utf-8"))
    period = run_log.get("기간", "").split("~")[0].strip()

    if st.button("리포트 생성"):
        _generate_report(run_dir)
        st.rerun()

    if st.session_state.report_md:
        st.markdown(status_badge("생성 완료", "완료"), unsafe_allow_html=True)
        st.caption(f"저장 위치: {st.session_state.report_path}")

        substituted = st.session_state.report_substituted or []
        remaining = st.session_state.report_remaining or []
        warnings = st.session_state.report_warnings or []

        if substituted:
            names = ", ".join(f"{n}장({CHAPTER_NAMES.get(n, n)})" for n in substituted)
            st.markdown(status_badge(f"병합된 장: {names}", "통과"), unsafe_allow_html=True)
        if remaining:
            names = ", ".join(f"{n}장({CHAPTER_NAMES.get(n, n)})" for n in remaining)
            st.markdown(status_badge(f"미작성 장: {names}", "경고"), unsafe_allow_html=True)
        if warnings:
            for w in warnings:
                st.markdown(status_badge(w, "경고"), unsafe_allow_html=True)

        # 리포트 본문 미리보기 — "생성 완료" 배지만 보여주고 실제 내용은
        # 안 그리는 회귀가 있었다(병합/미작성 장 배지를 추가하면서 이 줄이
        # 빠졌다, 실측으로 확인함). 그때 만든 마크다운을 화면에 그대로 낸다.
        st.divider()
        st.markdown(st.session_state.report_md)

    with st.expander("사람 작성분 편집"):
        st.caption(f"manual/sections.md · 대상기간 {period}")
        editor_key = "manual_editor_text"
        if editor_key not in st.session_state:
            st.session_state[editor_key] = manual_sections.read_raw(period)

        st.text_area("2·5·6장 원문", key=editor_key, height=320)

        if st.button("저장하고 리포트 다시 생성"):
            manual_sections.save_raw(st.session_state[editor_key], period)
            del st.session_state[editor_key]
            if st.session_state.report_md:
                _generate_report(run_dir)
            st.rerun()


# ---------------------------------------------------------------------------
# 7단계 — 이메일 초안 생성 (리포트 생성 후)
# ---------------------------------------------------------------------------

def render_step7():
    """7단계 — pipeline/email_draft.py로 발송 준비 초안(제목·수신·발신·본문
    html/text·첨부 목록)을 만들어 outputs/run_*/email.html·email.txt·
    email_meta.json으로 저장하고 화면에 미리보기한다. 실제 발송은 없다
    (CLAUDE.md 5-3 — SMTP는 8주차).

    6단계(리포트)가 끝났는지는 report_md 존재로만 판단한다 — 5단계 게이트를
    다시 검사하지 않는 것과 같은 이유로, 이 단계 전용 차단 로직을 새로 만들면
    6단계 판정과 어긋날 수 있다.
    """
    if not st.session_state.report_md:
        return

    st.divider()
    st.header("7단계 — 이메일 초안 생성")

    remaining = st.session_state.report_remaining or []
    if remaining:
        st.markdown(
            status_badge(f"리포트 {len(remaining)}개 장이 미작성 상태입니다. 제목에 (초안)이 붙습니다.", "경고"),
            unsafe_allow_html=True,
        )

    경고수 = (st.session_state.validation_result or {}).get("경고수", 0)
    if 경고수:
        st.markdown(status_badge(f"검증 경고 {경고수}건", "경고"), unsafe_allow_html=True)

    run_dir = Path(st.session_state.run_dir)

    if st.button("이메일 초안 생성"):
        run_context = _build_run_context(run_dir)
        t0 = time.time()
        with st.spinner("이메일 초안 생성 중"):
            email = email_draft.build_email(run_context, st.session_state.report_md)
        소요 = round(time.time() - t0, 3)
        생성_시각 = datetime.now().isoformat()

        html_path = run_dir / "email.html"
        text_path = run_dir / "email.txt"
        meta_path = run_dir / "email_meta.json"

        html_path.write_text(email["body_html"], encoding="utf-8")
        text_path.write_text(email["body_text"], encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {"subject": email["subject"], "to": email["to"], "from": email["from"],
                 "attachments": email["attachments"]},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )

        st.session_state.email_draft = email
        st.session_state.email_paths = {
            "html": str(html_path), "text": str(text_path), "meta": str(meta_path),
        }
        st.session_state.stage_durations["7_이메일"] = 소요
        _update_run_log(run_dir, **{
            "7_이메일": {
                "제목": email["subject"], "수신자": email["to"],
                "첨부_목록": email["attachments"], "생성_시각": 생성_시각,
            },
            "소요시간": dict(st.session_state.stage_durations),
        })
        st.rerun()

    email = st.session_state.email_draft
    if not email:
        return

    st.markdown(status_badge("초안 생성 완료", "완료"), unsafe_allow_html=True)
    st.caption(f"저장 위치: {st.session_state.email_paths['html']}")

    st.write(f"**제목**: {email['subject']}")
    st.write(f"**수신**: {', '.join(email['to'])}")
    st.write(f"**발신**: {email['from']}")

    st.subheader("본문 미리보기")
    st.iframe(email["body_html"], height=600)

    with st.expander("텍스트 버전 보기"):
        st.text(email["body_text"])

    st.subheader("첨부 목록")
    c1, c2, c3 = st.columns([3, 2, 2])
    c1.write("**파일명**")
    c2.write("**크기**")
    c3.write("**존재 여부**")
    for a in email["attachments"]:
        c1, c2, c3 = st.columns([3, 2, 2])
        c1.write(a["filename"])
        if a["size"] is not None:
            c2.write(f"{a['size']:,}바이트")
            c3.markdown(status_badge("존재", "완료"), unsafe_allow_html=True)
        else:
            c2.write("—")
            c3.markdown(status_badge("없음", "차단"), unsafe_allow_html=True)

    st.download_button(
        "이메일 HTML 다운로드",
        data=email["body_html"],
        file_name="email.html",
        mime="text/html",
    )


# ---------------------------------------------------------------------------
# 8단계 — 발송 확정 (되돌릴 수 없는 마지막 게이트)
# ---------------------------------------------------------------------------

def _render_step8_done():
    run_dir = Path(st.session_state.run_dir)
    st.markdown(status_badge("확정 완료", "완료"), unsafe_allow_html=True)
    st.write(f"확정 시각: {st.session_state.approved_at}")
    st.write("저장 위치:")
    st.write(f"- {run_dir / 'run_log.json'}")
    st.write(f"- {run_dir / 'APPROVED'}")
    st.write(f"- {run_dir / 'email_final.html'}")

    if st.button("새 실행 시작"):
        reset_downstream_state()
        st.rerun()


def render_step8():
    """8단계 — 발송 확정. CLAUDE.md 1절 "사용자는 넣기 한 번, 승인 세 번만
    한다"의 마지막 승인이고, 유일하게 되돌릴 수 없는 지점이다 — 그래서
    2·5단계 게이트보다 확인 항목이 많다(5개).

    7단계(이메일 초안)가 끝났는지는 email_draft 존재로 판단한다. 검증 차단은
    이론상 5단계에서 이미 막혀 여기까지 오지 않지만, "되돌릴 수 없는 마지막
    지점"이라는 이 단계의 성격상 명시적으로 요청받은 대로 한 번 더 확인하고
    확정 버튼 자체를 만들지 않는다 — 다른 단계에서 "이미 위에서 막았으니
    다시 검사하지 않는다"고 판단한 것과는 다르게, 여기는 사용자가 이 검사를
    분명히 원했다.
    """
    if not st.session_state.email_draft:
        return

    st.divider()
    st.header("8단계 — 발송 확정")

    if st.session_state.approved_at:
        _render_step8_done()
        return

    validation = st.session_state.validation_result or {}
    if validation.get("전체판정") == "차단":
        st.markdown(status_badge("차단됨 — 발송을 확정할 수 없습니다", "차단"), unsafe_allow_html=True)
        for f in validation.get("항목별_결과", []):
            if f.get("판정") == "차단":
                st.write(f"- **{f['대상지표']}**: {f['상세']}")
        return

    run_dir = Path(st.session_state.run_dir)
    run_log = json.loads((run_dir / "run_log.json").read_text(encoding="utf-8"))
    period = run_log.get("기간", "").split("~")[0].strip()
    email = st.session_state.email_draft
    remaining = st.session_state.report_remaining or []
    경고수 = validation.get("경고수", 0)

    st.write("아래 5가지를 실제로 확인한 뒤 체크해주세요.")
    checklist = {}

    checklist["기간"] = f"대상 기간이 맞다 ({period})"
    c1a, c1b = st.columns([1, 20])
    c1a.checkbox(" ", key="chk8_period", label_visibility="collapsed")
    c1b.write(f"**{checklist['기간']}**")

    checklist["수신자"] = f"수신자가 맞다 ({', '.join(email['to'])})"
    c2a, c2b = st.columns([1, 20])
    c2a.checkbox(" ", key="chk8_recipients", label_visibility="collapsed")
    c2b.write(f"**{checklist['수신자']}**")

    checklist["경고"] = f"검증 경고 {경고수}건을 확인했다"
    c3a, c3b = st.columns([1, 20])
    c3a.checkbox(" ", key="chk8_warnings", label_visibility="collapsed")
    c3b.write(f"**{checklist['경고']}**")

    if remaining:
        names = "·".join(CHAPTER_NAMES.get(n, n) for n in remaining)
        nums = "·".join(f"{n}장" for n in remaining)
        checklist["미작성"] = f"{nums}({names})이 비어 있는 상태로 발송하는 것을 확인했습니다"
    else:
        checklist["미작성"] = "미작성 장이 없음을 확인했다"
    c4a, c4b = st.columns([1, 20])
    c4a.checkbox(" ", key="chk8_unwritten", label_visibility="collapsed")
    with c4b:
        st.write(f"**{checklist['미작성']}**")
        if remaining:
            st.caption("초안 공유가 목적일 수 있으므로 차단하지 않습니다. 대신 제목에 (초안)이 표시됩니다.")

    checklist["첨부"] = "첨부 파일 목록을 확인했다"
    c5a, c5b = st.columns([1, 20])
    c5a.checkbox(" ", key="chk8_attachments", label_visibility="collapsed")
    with c5b:
        st.write(f"**{checklist['첨부']}**")
        for a in email["attachments"]:
            상태 = "존재" if a["size"] is not None else "없음"
            st.caption(f"- {a['filename']} ({상태})")

    all_checked = all([
        st.session_state.chk8_period, st.session_state.chk8_recipients,
        st.session_state.chk8_warnings, st.session_state.chk8_unwritten,
        st.session_state.chk8_attachments,
    ])

    st.divider()
    st.markdown(status_badge("되돌릴 수 없음", "차단"), unsafe_allow_html=True)
    st.warning(
        "확정하면 발송 준비 최종본이 저장됩니다. 이 앱은 실제로 메일을 "
        "보내지 않습니다(8주차에 구현)."
    )

    clicked = st.button("발송 확정", type="primary", disabled=not all_checked)
    if not all_checked:
        st.caption("5가지를 모두 체크해야 확정할 수 있습니다.")

    if not clicked:
        return

    run_time = datetime.now()
    (run_dir / "email_final.html").write_text(email["body_html"], encoding="utf-8")

    확정정보 = {
        "발송확정_시각": run_time.isoformat(),
        "확인한_체크항목": list(checklist.values()),
        "제목": email["subject"],
        "수신자": email["to"],
        "미작성_장_목록": remaining,
        "검증_경고수": 경고수,
        "첨부_목록": email["attachments"],
    }

    (run_dir / "APPROVED").write_text(
        json.dumps(확정정보, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    run_log.update(확정정보)
    run_log["8_확정"] = {
        "발송확정_시각": 확정정보["발송확정_시각"],
        "확인한_체크항목": 확정정보["확인한_체크항목"],
    }
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    st.session_state.approved_at = run_time.isoformat()
    st.session_state.step = 9  # STEPS는 8까지뿐 — 사이드바에 8단계까지 전부 "완료"로 표시하려는 것
    st.rerun()


# ---------------------------------------------------------------------------
# 실행 기록 보기 — run_log.json을 사람이 읽는 표로
# ---------------------------------------------------------------------------

def _log_display(v):
    """표에 넣기 전 값 하나를 항상 문자열로 만든다. run_log.json의 "값" 같은
    칸은 실행마다 int·str·None·list가 뒤섞여 들어올 수 있는데, 섞인 채로
    DataFrame에 넣으면 pyarrow가 컬럼 타입을 하나로 못 정해 통째로 죽는다
    (실측: "Expected bytes, got a 'int' object"로 렌더링 자체가 깨졌다).
    표시가 목적이라 타입을 살릴 이유가 없어, 전부 문자열로 통일한다."""
    if v is None:
        return "—"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _render_log_value(value):
    """run_log.json의 값 하나를 종류에 맞게 표/목록/텍스트로 그린다.
    run_log가 "N_단계" 별로 서로 다른 모양(스칼라 모음, 리스트, dict의 dict)을
    섞어 담고 있어서, 구조를 미리 정해두지 않고 값의 실제 타입으로 분기한다
    — 8단계 전체를 한 함수가 다 알고 있어야 하는 하드코딩을 피한다."""
    if isinstance(value, dict):
        if not value:
            st.caption("(없음)")
            return
        scalar_rows = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                with st.expander(str(k)):
                    _render_log_value(v)
            else:
                scalar_rows.append({"항목": k, "값": _log_display(v)})
        if scalar_rows:
            st.dataframe(pd.DataFrame(scalar_rows), hide_index=True, width="stretch")
    elif isinstance(value, list):
        if not value:
            st.caption("(없음)")
        elif all(isinstance(x, dict) for x in value):
            rows = [{k: _log_display(v) for k, v in row.items()} for row in value]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            for x in value:
                st.write(f"- {_log_display(x)}")
    else:
        st.write(_log_display(value))


def render_run_log_viewer():
    """run_log.json 전체를 화면에서 훑어볼 수 있게 한다. 파일을 매번 다시
    읽는다 — session_state에 캐싱하면 이 실행 도중 계속 갱신되는 run_log.json의
    최신 상태를 놓친다."""
    if not st.session_state.run_dir:
        return

    run_log_path = Path(st.session_state.run_dir) / "run_log.json"
    if not run_log_path.exists():
        return

    st.divider()
    with st.expander("실행 기록 보기 (run_log.json)"):
        st.caption(f"경로: {run_log_path}")
        run_log = json.loads(run_log_path.read_text(encoding="utf-8"))
        for section, value in run_log.items():
            st.subheader(section)
            _render_log_value(value)


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main():
    st.title("월간 리포트 자동화")

    render_sidebar()
    render_step1()
    render_step2()
    render_gate()
    render_calculation_results()
    render_comparison()
    render_validation()
    render_dashboard_top()
    render_step5()
    render_step6()
    render_step7()
    render_step8()
    render_run_log_viewer()


if __name__ == "__main__":
    main()
