"""지표 계산 모듈 — 5주차 scripts/calc_metrics.py의 구조를 그대로 따른다(CLAUDE.md 5-2).

계승한 것: 정의서의 계산.원천·집계·조인·조건으로 SQL을 조립하는 방식, 기초형/
비율형/파생형/변화율형 4갈래 분기, 재귀로 파생지표의 의존지표를 먼저 계산하는
구조. 지표별 하드코딩 SQL은 만들지 않는다.

달라진 것 두 가지:
  1. 정의를 위키 .md가 아니라 catalog/metrics_catalog.json(이미 로드된 dict)에서
     읽는다 — 앱은 위키를 직접 읽지 않는다(CLAUDE.md 3절).
  2. 업로드 파일은 원본 테이블에 붙이지 않고 스테이징 테이블로 올린 뒤, 판정된
     테이블이 원천으로 쓰이는 자리에서만 스테이징 테이블로 치환한다(table_override).
     다른 원천 테이블(예: customers)은 그대로 실물 테이블을 쓴다 — "부분 갱신"이
     이 치환 하나 안 하는 것으로 자연스럽게 표현된다.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from google.api_core import exceptions as gax_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import bigquery

import config

PLACEHOLDER_PATTERN = re.compile(r"@month(?:_start|_end)?\b")
WIKILINK_PATTERN = re.compile(r"\[\[([A-Za-z0-9_]+)\]\]")

TERMINAL_STATUSES = {"유효구간 밖", "계산오류"}

CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"


def load_metrics_catalog() -> dict:
    """catalog/metrics_catalog.json을 읽는다. compare.py·charts.py 등 지표
    카탈로그가 필요한 다른 모듈이 전부 이 함수 하나를 공유한다 — 카탈로그
    경로를 찾는 로직을 여러 곳에 복제하지 않는다."""
    path = CATALOG_DIR / "metrics_catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))


class AuthError(RuntimeError):
    """BigQuery 인증 실패. 화면에서 gcloud 명령을 안내하고 전체를 중단하는 데 쓴다."""


@dataclass
class MetricResult:
    metric_id: str
    지표명: str
    유형: str
    month: str
    value: Optional[float] = None
    sample_size: Optional[float] = None
    min_sample: Optional[float] = None
    status: str = "OK"  # OK / 구간확장 / 표본부족 / 계산오류 / 유효구간 밖
    error: Optional[str] = None
    부분갱신: bool = False
    원천: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1단계 — 스테이징 적재
# ---------------------------------------------------------------------------

def _cloud_credentials():
    """Streamlit Secrets에 서비스 계정이 있으면 (credentials, project_id)를,
    없으면 (None, None)을 반환한다.

    왜 필요한가: 로컬에서는 `gcloud auth application-default login`으로 ADC를
    쓰지만, Streamlit Community Cloud에는 그 로그인 세션이 없다 — 대신
    Streamlit의 Secrets(로컬은 .streamlit/secrets.toml, 배포본은 앱 설정 화면)에
    서비스 계정 JSON을 넣어두고 그걸로 인증해야 한다.

    streamlit이 없거나 secrets에 그 키가 없으면 조용히 (None, None)을 반환한다
    — 이건 "인증 실패"가 아니라 "이 경로를 쓰지 않는다"는 뜻이라 예외로 다루지
    않는다. 그래서 로컬 CLI(run_pipeline.py)·기존 ADC 흐름은 아무 변화 없이
    그대로 동작한다.
    """
    try:
        import streamlit as st
        if "gcp_service_account" not in st.secrets:
            return None, None
        info = dict(st.secrets["gcp_service_account"])
    except Exception:  # noqa: BLE001 - streamlit 미설치·secrets 파일 없음 등, 전부 "이 경로 없음"
        return None, None

    from google.oauth2 import service_account
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return credentials, info.get("project_id")


def get_client() -> bigquery.Client:
    """BigQuery 클라이언트를 만든다. 인증이 안 돼 있으면 AuthError로 바꿔서
    호출자가 "원인 + gcloud 명령"을 화면에 보여줄 수 있게 한다."""
    try:
        credentials, cloud_project = _cloud_credentials()
        if credentials is not None:
            return bigquery.Client(project=config.BQ_PROJECT or cloud_project, credentials=credentials)
        return bigquery.Client(project=config.BQ_PROJECT) if config.BQ_PROJECT else bigquery.Client()
    except auth_exceptions.DefaultCredentialsError as e:
        raise AuthError(str(e)) from e


def new_run_id() -> str:
    """이 실행 하나를 식별하는 고유 id(시각+짧은 무작위값)를 만든다.

    스테이징 테이블명과 run 폴더명에 똑같이 붙여서, 나중에 "이 run이 어느
    스테이징 테이블을 썼는지"를 이름만 보고 바로 알 수 있게 한다.

    분 단위 시각만 쓰지 않는 이유: 로컬에서 혼자 쓸 때는 안 보이지만,
    Streamlit Cloud처럼 여러 사용자가 동시에 같은 원본 테이블을 대상으로
    실행할 수 있는 환경에서는 같은 분(심지어 같은 초)에 두 실행이 겹칠 수
    있다 — 그러면 옛 설계(테이블명당 스테이징 하나)에서는 나중 실행이 앞
    실행의 스테이징을 덮어써 버린다. 무작위 값을 더해 이 충돌을 없앤다.
    """
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{uuid.uuid4().hex[:6]}"


def load_staging_table(df: pd.DataFrame, table_name: str, client: bigquery.Client, run_id: str) -> str:
    """업로드된 df를 {STAGING_PREFIX}{table_name}_{run_id}로 적재한다.

    왜 적재(LOAD JOB)이지 INSERT(DML)가 아닌가: CLAUDE.md 5-2 — DML은 BigQuery
    샌드박스에서 차단된다. load_table_from_dataframe + WRITE_TRUNCATE는 DDL(테이블
    재생성) 성격이라 결제 계정 없이도 동작한다.

    왜 원본 테이블이 아니라 STAGING_PREFIX를 붙인 별도 테이블인가: 원본 테이블에
    데이터를 붙이면 실행마다 원본이 오염되고 되돌릴 수 없다.

    왜 테이블명에 run_id까지 붙이는가(설계 변경, CLAUDE.md 5-2 갱신 이력 참고):
    처음엔 테이블명당 스테이징 하나(WRITE_TRUNCATE로 매번 교체)였는데, 이건
    "한 번에 한 사람만 쓴다"는 전제에서만 안전하다. 클라우드에 배포되면 여러
    실행이 겹칠 수 있고, 그때 나중 실행이 앞 실행의 스테이징을 덮어써 버리면
    앞 실행이 나중에 "기존 실행 불러오기"로 다시 열렸을 때 잘못된(다른 실행의)
    데이터를 보게 된다 — 실제로 그 위험을 감지하고 바꿨다. run_id를 붙이면
    실행마다 완전히 새 테이블이라 서로 절대 안 겹친다.

    만료 시간(TTL)을 거는 이유: 실행마다 새 테이블이 계속 쌓이면 정리가
    안 된다. config.STAGING_TABLE_TTL_DAYS 뒤에 BigQuery가 스스로 지우게
    한다 — 사람이 따로 정리 스크립트를 돌릴 필요가 없다.
    """
    staging_table = f"{config.STAGING_PREFIX}{table_name}_{run_id}"
    table_id = f"{config.BQ_DATASET}.{staging_table}"

    job_config = bigquery.LoadJobConfig(write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    try:
        job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
        job.result()

        table_ref = client.get_table(table_id)
        table_ref.expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=config.STAGING_TABLE_TTL_DAYS)
        client.update_table(table_ref, ["expires"])
    except auth_exceptions.DefaultCredentialsError as e:
        raise AuthError(str(e)) from e
    except gax_exceptions.Forbidden as e:
        raise AuthError(str(e)) from e

    return staging_table


# ---------------------------------------------------------------------------
# 기간 유틸 (calc_metrics.py와 동일)
# ---------------------------------------------------------------------------

def parse_year_month(s: str):
    y, m = s.strip().split("-")
    return int(y), int(m)


def shift_year_month(year: int, month: int, delta: int):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def months_in_range(start_str: str, end_str: str):
    """업로드 파일의 기간(최소~최대)에 포함된 (year, month) 목록. 한 달짜리
    업로드면 원소 1개다 — 여러 달이 한 번에 올라와도 전부 계산한다."""
    sy, sm = parse_year_month(start_str)
    ey, em = parse_year_month(end_str)
    result = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append((y, m))
        y, m = shift_year_month(y, m, 1)
    return result


def month_bounds(year: int, month: int):
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last_day)


def month_params(year: int, month: int) -> dict:
    start, end = month_bounds(year, month)
    return {"month": f"{year:04d}-{month:02d}", "month_start": start, "month_end": end}


def in_valid_range(year: int, month: int, range_str) -> bool:
    if not range_str or "~" not in str(range_str):
        return True
    start_s, end_s = (p.strip() for p in str(range_str).split("~"))
    sy, sm = parse_year_month(start_s)
    ey, em = parse_year_month(end_s)
    return (sy, sm) <= (year, month) <= (ey, em)


def extract_dep(text) -> Optional[str]:
    m = WIKILINK_PATTERN.search(str(text))
    return m.group(1) if m else None


def is_change_rate(fm: dict) -> bool:
    return "시차" in (fm.get("계산") or {})


def is_base_type(유형: str) -> bool:
    return 유형.startswith("카운트형") or 유형.startswith("금액형")


def is_ratio_type(유형: str) -> bool:
    return 유형.startswith("비율형")


def is_derived_type(유형: str) -> bool:
    return 유형.startswith("파생형")


# ---------------------------------------------------------------------------
# SQL 조립 — table_override로 판정된 테이블만 스테이징으로 치환
# ---------------------------------------------------------------------------

def _tables_in_leg(spec: dict) -> set:
    return {t.strip() for t in str(spec.get("원천", "")).split("+") if t.strip()}


def uses_override(calc: dict, effective_override: dict, is_ratio: bool) -> bool:
    """이 SQL이 실제로 스테이징 테이블을 참조하는지 판정한다.

    effective_override가 비어 있지 않다고 해서 이 지표가 치환됐다는 뜻은 아니다
    — 예: active_customers_contract는 원천이 customers뿐이라 usage_history용
    override가 있어도 실제로는 하나도 안 쓰인다. "이 지표의 원천 테이블 목록"과
    "override의 키"가 실제로 겹치는지 봐야 정확하다(화면에 잘못된 배지를 보여줄
    뻔한 걸 실측으로 확인함).
    """
    if is_ratio:
        tables = _tables_in_leg(calc.get("분자", {})) | _tables_in_leg(calc.get("분모", {}))
    else:
        tables = _tables_in_leg(calc)
    return bool(tables & set(effective_override.keys()))


def build_leg_sql(spec: dict, dataset: str, table_override: dict) -> str:
    tables = [t.strip() for t in str(spec["원천"]).split("+") if t.strip()]
    from_parts = [
        f"`{dataset}.{table_override.get(t, t)}` AS {t}" for t in tables
    ]
    frm = " JOIN ".join(from_parts)
    if len(tables) > 1:
        frm += f" ON {spec['조인']}"
    sql = f"SELECT {spec['집계']} AS value FROM {frm}"
    조건 = spec.get("조건")
    if 조건:
        sql += f" WHERE {조건}"
    return sql


def build_ratio_sql(calc: dict, dataset: str, table_override: dict) -> str:
    n = build_leg_sql(calc["분자"], dataset, table_override)
    d = build_leg_sql(calc["분모"], dataset, table_override)
    return (
        f"WITH n AS ({n}), d AS ({d}) "
        "SELECT (SELECT value FROM n) AS numerator, "
        "(SELECT value FROM d) AS denominator, "
        "SAFE_DIVIDE((SELECT value FROM n), (SELECT value FROM d)) AS value"
    )


def run_query(client: bigquery.Client, sql: str, mparams: dict):
    used = set(PLACEHOLDER_PATTERN.findall(sql))
    query_params = []
    if "@month_start" in used:
        query_params.append(bigquery.ScalarQueryParameter("month_start", "DATE", mparams["month_start"]))
    if "@month_end" in used:
        query_params.append(bigquery.ScalarQueryParameter("month_end", "DATE", mparams["month_end"]))
    if "@month" in used:
        query_params.append(bigquery.ScalarQueryParameter("month", "STRING", mparams["month"]))
    job_config = bigquery.QueryJobConfig(query_parameters=query_params)
    try:
        return list(client.query(sql, job_config=job_config).result())
    except auth_exceptions.DefaultCredentialsError as e:
        raise AuthError(str(e)) from e
    except gax_exceptions.Forbidden as e:
        raise AuthError(str(e)) from e


def safe_divide(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def check_min_sample(fm: dict, sample):
    최소표본 = fm.get("최소표본")
    최소표본_num = 최소표본 if isinstance(최소표본, (int, float)) else None
    if 최소표본_num is not None and sample is not None and sample < 최소표본_num:
        return 최소표본_num, "표본부족"
    return 최소표본_num, "OK"


# ---------------------------------------------------------------------------
# 2단계 — 지표 계산 (재귀)
# ---------------------------------------------------------------------------

def compute_metric(
    metric_id: str,
    year: int,
    month: int,
    metrics_catalog: dict,
    client: bigquery.Client,
    dataset: str,
    table_override: dict,
    uploaded_months: set,
    extend_approved: bool,
    cache: dict,
    sql_log: list,
) -> MetricResult:
    """지표 하나를 계산한다. 파생형/변화율형은 의존지표를 먼저 재귀로 계산한다
    (calc_metrics.py의 compute_metric과 동일한 구조).

    "유효구간 밖" 처리: 게이트에서 승인받지 못했으면(extend_approved=False)
    calc_metrics.py와 똑같이 계산 자체를 하지 않고 즉시 "유효구간 밖"을 반환한다
    (CLAUDE.md 9절: 0으로 처리 금지). 승인받았으면 검사를 건너뛰고 계산하되,
    결과 상태를 "구간확장"으로 표시해 정의서 범위 밖 계산이었다는 걸 숨기지 않는다.

    오류 격리: 이 지표 하나가 실패해도(쿼리 오류·정의서 결함 등) 예외를 밖으로
    던지지 않고 "계산오류" 상태로 담아 반환한다 — 지표 하나의 실패가 나머지
    지표까지 멈추면 안 된다는 요구사항 때문이다. 단, AuthError(인증 실패)는
    전체를 멈춰야 하므로 그대로 다시 던진다.
    """
    key = (metric_id, year, month)
    if key in cache:
        return cache[key]

    fm = metrics_catalog.get(metric_id)
    if fm is None:
        result = MetricResult(metric_id, metric_id, "?", f"{year:04d}-{month:02d}",
                               status="계산오류", error="카탈로그에 없는 metric_id")
        cache[key] = result
        return result

    유형 = fm.get("유형", "")
    지표명 = fm.get("지표명", metric_id)
    month_str = f"{year:04d}-{month:02d}"

    in_range = in_valid_range(year, month, fm.get("유효구간"))
    if not in_range and not extend_approved:
        result = MetricResult(metric_id, 지표명, 유형, month_str, status="유효구간 밖")
        cache[key] = result
        return result

    mparams = month_params(year, month)
    calc = fm.get("계산") or {}

    # table_override(스테이징 치환)는 업로드 파일이 실제로 커버하는 월에만 적용한다.
    # 파생/변화율형이 다른 월(예: 3개월 전)을 참조할 때는 그 월에 스테이징 데이터가
    # 없으므로 실물 테이블을 그대로 써야 한다 — 여기서 안 걸러내면 스테이징 테이블에
    # 없는 월을 조회해 조용히 빈 값(None)을 돌려주는 버그가 생긴다(실측으로 확인함).
    effective_override = table_override if (year, month) in uploaded_months else {}

    try:
        if is_change_rate(fm):
            result = _compute_change_rate(
                metric_id, fm, year, month, metrics_catalog, client, dataset,
                table_override, uploaded_months, extend_approved, cache, sql_log,
            )
        elif is_base_type(유형):
            sql = build_leg_sql(calc, dataset, effective_override)
            sql_log.append({"metric_id": metric_id, "month": month_str, "sql": sql,
                             "스테이징_치환": uses_override(calc, effective_override, is_ratio=False)})
            rows = run_query(client, sql, mparams)
            value = rows[0].value if rows else None
            min_sample, status = check_min_sample(fm, value)
            result = MetricResult(metric_id, 지표명, 유형, month_str, value=value,
                                   sample_size=value, min_sample=min_sample, status=status)
        elif is_ratio_type(유형):
            sql = build_ratio_sql(calc, dataset, effective_override)
            sql_log.append({"metric_id": metric_id, "month": month_str, "sql": sql,
                             "스테이징_치환": uses_override(calc, effective_override, is_ratio=True)})
            rows = run_query(client, sql, mparams)
            row = rows[0] if rows else None
            denominator = row.denominator if row else None
            min_sample, status = check_min_sample(fm, denominator)
            result = MetricResult(metric_id, 지표명, 유형, month_str,
                                   value=row.value if row else None,
                                   sample_size=denominator, min_sample=min_sample, status=status)
        elif is_derived_type(유형):
            num_id = extract_dep(calc.get("분자", ""))
            den_id = extract_dep(calc.get("분모", ""))
            num = compute_metric(num_id, year, month, metrics_catalog, client, dataset,
                                  table_override, uploaded_months, extend_approved, cache, sql_log)
            den = compute_metric(den_id, year, month, metrics_catalog, client, dataset,
                                  table_override, uploaded_months, extend_approved, cache, sql_log)
            if num.status in TERMINAL_STATUSES or den.status in TERMINAL_STATUSES:
                bad = num if num.status in TERMINAL_STATUSES else den
                result = MetricResult(metric_id, 지표명, 유형, month_str, status=bad.status, error=bad.error)
            else:
                value = safe_divide(num.value, den.value)
                min_sample, status = check_min_sample(fm, den.value)
                result = MetricResult(metric_id, 지표명, 유형, month_str, value=value,
                                       sample_size=den.value, min_sample=min_sample, status=status)
        else:
            result = MetricResult(metric_id, 지표명, 유형, month_str, status="계산오류",
                                   error=f"알 수 없는 유형: {유형!r}")
    except AuthError:
        raise
    except Exception as e:  # noqa: BLE001 - 지표 하나의 실패를 격리하는 게 목적
        result = MetricResult(metric_id, 지표명, 유형, month_str, status="계산오류", error=str(e))

    if not in_range and result.status == "OK":
        result.status = "구간확장"

    cache[key] = result
    return result


def _compute_change_rate(metric_id, fm, year, month, metrics_catalog, client, dataset,
                          table_override, uploaded_months, extend_approved, cache, sql_log) -> MetricResult:
    지표명 = fm.get("지표명", metric_id)
    유형 = fm.get("유형", "")
    month_str = f"{year:04d}-{month:02d}"
    calc = fm.get("계산") or {}

    base = extract_dep(calc.get("기준지표", ""))
    lag = calc.get("시차")
    py, pm = shift_year_month(year, month, lag)

    cur = compute_metric(base, year, month, metrics_catalog, client, dataset,
                          table_override, uploaded_months, extend_approved, cache, sql_log)
    prev = compute_metric(base, py, pm, metrics_catalog, client, dataset,
                           table_override, uploaded_months, extend_approved, cache, sql_log)

    if cur.status in TERMINAL_STATUSES or prev.status in TERMINAL_STATUSES:
        bad = cur if cur.status in TERMINAL_STATUSES else prev
        return MetricResult(metric_id, 지표명, 유형, month_str, status=bad.status, error=bad.error)

    delta = (cur.value - prev.value) if (cur.value is not None and prev.value is not None) else None
    if delta is not None and str(calc.get("방향", "증가")).startswith("감소"):
        delta = -delta
    value = safe_divide(delta, prev.value)

    min_sample, status = check_min_sample(fm, None)
    return MetricResult(metric_id, 지표명, 유형, month_str, value=value, min_sample=min_sample, status=status)


# ---------------------------------------------------------------------------
# 전체 실행 — app.py가 부르는 진입점
# ---------------------------------------------------------------------------

def compute_target_metrics(
    target_metric_ids: list,
    table_name: str,
    staging_table: str,
    period_start: str,
    period_end: str,
    metrics_catalog: dict,
    client: bigquery.Client,
    extend_approved: bool,
) -> tuple:
    """스테이징 적재가 끝난 뒤, 대상 지표들을 순서대로 계산한다.

    app.py는 이 함수를 get_client()/load_staging_table() 다음 단계로 따로 불러서
    "스테이징 적재 중"과 "지표 계산 중" 스피너를 각각의 함수 호출에 씌운다.

    Returns:
        (results, sql_log) — results는 target_metric_ids와 같은 순서의
        list[MetricResult]. sql_log는 실제로 실행된 SQL을 순서대로 담은
        list[dict]({"metric_id", "month", "sql", "스테이징_치환"}) — 화면 하단
        expander에서 "스테이징 치환이 진짜 적용됐는지"를 눈으로 확인하는 용도.
        파생/변화율형은 자기 SQL이 없고 의존지표의 SQL이 로그에 대신 남는다.
    """
    table_override = {table_name: staging_table}
    months = months_in_range(period_start, period_end)
    uploaded_months = set(months)  # table_override는 이 월들에만 적용된다
    cache = {}
    results = []
    sql_log = []

    from pipeline.profile import resolve_source_tables  # 순환 임포트 회피용 지연 임포트

    for metric_id in target_metric_ids:
        source_tables = resolve_source_tables(metric_id, metrics_catalog)
        부분갱신 = table_name in source_tables and len(source_tables - {table_name}) > 0

        # 여러 달이 걸쳐 있으면 마지막 달 기준으로 대표값을 보여준다(오늘 실습은
        # 한 달짜리 업로드만 다루므로 실질적으로 months는 원소 1개다).
        result = None
        for year, month in months:
            result = compute_metric(metric_id, year, month, metrics_catalog, client,
                                     config.BQ_DATASET, table_override, uploaded_months,
                                     extend_approved, cache, sql_log)
        result.부분갱신 = 부분갱신
        result.원천 = sorted(source_tables)
        results.append(result)

    return results, sql_log


def calculate_metrics(
    df: pd.DataFrame,
    table_name: str,
    target_metric_ids: list,
    period_start: str,
    period_end: str,
    metrics_catalog: dict,
    schema_catalog: dict,
    extend_approved: bool,
    on_progress=None,
) -> tuple:
    """스테이징 적재 -> 대상 지표 계산까지 한 번에 수행하는 편의 함수(테스트·CLI용).
    app.py는 이 함수 대신 get_client/load_staging_table/compute_target_metrics를
    직접 순서대로 불러서 단계별 스피너를 붙인다."""
    def notify(msg):
        if on_progress:
            on_progress(msg)

    notify("스테이징 적재 중")
    client = get_client()
    staging_table = load_staging_table(df, table_name, client, new_run_id())

    notify("지표 계산 중")
    return compute_target_metrics(
        target_metric_ids, table_name, staging_table, period_start, period_end,
        metrics_catalog, client, extend_approved,
    )


def save_metrics_csv(results: list, run_dir: Path) -> Path:
    """계산 결과를 outputs/run_*/metrics.csv로 저장한다."""
    rows = [{
        "metric_id": r.metric_id,
        "지표명": r.지표명,
        "유형": r.유형,
        "month": r.month,
        "value": r.value,
        "sample_size": r.sample_size,
        "min_sample": r.min_sample,
        "status": r.status,
        "부분갱신": r.부분갱신,
        "원천": "+".join(r.원천),
        "error": r.error or "",
    } for r in results]
    path = Path(run_dir) / "metrics.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path
