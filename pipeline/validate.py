"""계산 결과를 기계적으로 점검하는 모듈 — CLAUDE.md 6절 "검증 자동화 범위"의
"자동으로 하는 것" 5종을 구현한다.

공통 반환 형태(모든 check_* 함수의 리스트 원소): {검증명, 대상지표, 판정, 상세, 값}
판정은 "통과" / "경고" / "차단" 셋 중 하나다.

이 모듈이 하지 않는 것(자동화 범위 밖, CLAUDE.md 6절): 혼입 변수 층화, 역인과
검토, 가설 검정. 셋 다 업무 지식·판단이 필요해서 기계가 대신할 수 없다.
validate_all()의 반환값에 "자동검증하지_않은_것"으로 항상 명시한다 — 이 목록이
누락되면 "검증 통과"가 실제보다 강한 신뢰를 준다(6절 경고 그대로).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from pipeline import calculate as calc  # noqa: E402

AUTO_SKIPPED = [
    {"항목": "혼입 변수 층화", "이유": "어느 변수가 혼입인지는 업무 지식이 필요하다"},
    {"항목": "역인과 검토", "이유": "판단이 필요하다"},
    {"항목": "가설 검정", "이유": "사전 정의된 가설이 없으면 다중비교 문제를 통제할 수 없다"},
]

DERIVED_RATIO_TOLERANCE = 0.0001  # 상대 오차 0.01%


def _finding(검증명, 대상지표, 판정, 상세, 값=None) -> dict:
    return {"검증명": 검증명, "대상지표": 대상지표, "판정": 판정, "상세": 상세, "값": 값}


# ---------------------------------------------------------------------------
# 1. 유효구간
# ---------------------------------------------------------------------------

def check_valid_range(metrics_df, catalog, override: bool) -> list:
    """status가 "유효구간 밖"이면 차단, "구간확장"이면 경고.

    구간확장을 차단으로 처리하면 안 되는 이유: 사용자가 게이트에서 이미 승인한
    확장이다. 승인된 걸 또 차단하면 게이트에서의 승인이 아무 의미가 없어진다
    — "이번 실행에서만 적용, 다음엔 다시 승인"이라는 설계 자체가 무너진다.
    같은 이유로 진짜 미승인 상태("유효구간 밖")는 반드시 차단해야 한다.
    """
    out_of_range = metrics_df[metrics_df["status"] == "유효구간 밖"]
    extended = metrics_df[metrics_df["status"] == "구간확장"]
    ok_rest = metrics_df[~metrics_df["status"].isin(["유효구간 밖", "구간확장"])]

    findings = []
    if len(out_of_range) > 0:
        names = ", ".join(out_of_range["metric_id"])
        findings.append(_finding(
            "유효구간", f"{len(out_of_range)}종 전체", "차단",
            f"승인 없이 정의서 유효구간을 벗어나 계산되지 않음: {names}",
            list(out_of_range["metric_id"]),
        ))
    if len(extended) > 0:
        names = ", ".join(extended["metric_id"])
        findings.append(_finding(
            "유효구간", f"{len(extended)}종 전체", "경고",
            f"정의서 구간 밖이지만 확장 승인(override={override})으로 계산됨: {names}",
            list(extended["metric_id"]),
        ))
    if len(ok_rest) > 0:
        findings.append(_finding(
            "유효구간", f"{len(ok_rest)}종 전체", "통과",
            "정의서 유효구간 안에서 계산됨",
            list(ok_rest["metric_id"]),
        ))
    return findings


# ---------------------------------------------------------------------------
# 2. 최소표본
# ---------------------------------------------------------------------------

def check_min_sample(metrics_df) -> list:
    """sample_size < min_sample이면 경고. min_sample이 없는(해당 없음) 지표는
    애초에 검사 대상이 아니다 — "검사 안 함"과 "통과"를 구분하기 위해 아예
    목록에서 뺀다(억지로 통과 처리하면 표본 기준이 없던 지표까지 검증된 것처럼
    보인다)."""
    checkable = metrics_df[metrics_df["min_sample"].notna()]
    if len(checkable) == 0:
        return []

    failed = checkable[checkable["sample_size"] < checkable["min_sample"]]
    passed = checkable[checkable["sample_size"] >= checkable["min_sample"]]

    findings = []
    if len(passed) > 0:
        표본_최소 = passed["sample_size"].min()
        표본_최대 = passed["sample_size"].max()
        기준 = passed["min_sample"].max()
        findings.append(_finding(
            "최소표본", f"{len(passed)}종 전체", "통과",
            f"전체 통과 (표본 {표본_최소:.0f}~{표본_최대:.0f}, 기준 {기준:.0f})",
            None,
        ))
    for _, row in failed.iterrows():
        findings.append(_finding(
            "최소표본", row["metric_id"], "경고",
            f"표본 {row['sample_size']:.0f} < 최소표본 {row['min_sample']:.0f}",
            row["sample_size"],
        ))
    return findings


# ---------------------------------------------------------------------------
# 3. 파생 정합성 — 이 검증만 실제로 다시 계산해서 대조한다
# ---------------------------------------------------------------------------

def check_derived_consistency(
    metrics_df, metrics_catalog, client,
    dataset=None, table_override=None, uploaded_months=None, extend_approved=False,
) -> list:
    """파생형 지표의 분자/분모를 calculate.py로 독립적으로 다시 계산해서,
    (분자/분모)가 저장된 값과 실제로 같은지 대조한다.

    "재계산"이 핵심이다 — metrics_df에 이미 있는 값끼리 나눠보는 게 아니라,
    compute_metric()을 새 캐시로 처음부터 다시 실행한다. 그래야 "계산 로직
    자체에 버그가 있어서 저장된 값이 애초에 틀렸다"는 경우까지 잡을 수 있다.
    이게 이 5종 중 유일하게 계산 로직 버그를 잡는 자동 장치인 이유다.

    변화율형(계산.시차가 있는 파생형)은 대상에서 뺀다 — 분자/분모가 아니라
    기준지표+시차 구조라 이 재계산 방식이 애초에 적용되지 않는다.

    의존 지표가 계산 안 됐으면(예: 분모 지표가 이번 계산 대상이 아니었음)
    "검사 불가"로 경고한다 — 차단하지 않는다. 검사를 못 한 것과 검사해서
    틀린 것은 다른 사실이다.
    """
    dataset = dataset or config.BQ_DATASET
    table_override = table_override or {}
    uploaded_months = uploaded_months or set()

    findings = []
    derived_rows = metrics_df[metrics_df["유형"].astype(str).str.startswith("파생형")]

    for _, row in derived_rows.iterrows():
        metric_id = row["metric_id"]
        fm = metrics_catalog.get(metric_id, {})
        calc_spec = fm.get("계산") or {}

        if "시차" in calc_spec:
            continue  # 변화율형 — 이 검증 대상 아님

        num_id = calc.extract_dep(calc_spec.get("분자", ""))
        den_id = calc.extract_dep(calc_spec.get("분모", ""))
        if not num_id or not den_id:
            findings.append(_finding(
                "파생 정합성", metric_id, "경고",
                "정의서에서 분자/분모 metric_id를 찾지 못해 검사 불가", None,
            ))
            continue

        try:
            year, month = calc.parse_year_month(row["month"])
        except Exception:
            findings.append(_finding(
                "파생 정합성", metric_id, "경고", "month 값을 해석할 수 없어 검사 불가", None,
            ))
            continue

        fresh_cache: dict = {}
        fresh_sql_log: list = []
        try:
            num = calc.compute_metric(num_id, year, month, metrics_catalog, client, dataset,
                                       table_override, uploaded_months, extend_approved,
                                       fresh_cache, fresh_sql_log)
            den = calc.compute_metric(den_id, year, month, metrics_catalog, client, dataset,
                                       table_override, uploaded_months, extend_approved,
                                       fresh_cache, fresh_sql_log)
        except calc.AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            findings.append(_finding(
                "파생 정합성", metric_id, "경고", f"재계산 중 오류로 검사 불가: {e}", None,
            ))
            continue

        if num.value is None or den.value is None or den.value == 0:
            findings.append(_finding(
                "파생 정합성", metric_id, "경고",
                f"의존 지표({num_id}={num.value}, {den_id}={den.value})가 계산되지 않아 검사 불가",
                None,
            ))
            continue

        재계산값 = num.value / den.value
        저장값 = row["value"]
        if 저장값 in (None, 0) or (isinstance(저장값, float) and 저장값 != 저장값):
            findings.append(_finding(
                "파생 정합성", metric_id, "경고", "저장된 값이 없어 검사 불가", None,
            ))
            continue

        상대오차 = abs(재계산값 - 저장값) / abs(저장값)
        판정 = "통과" if 상대오차 <= DERIVED_RATIO_TOLERANCE else "차단"
        findings.append(_finding(
            "파생 정합성", metric_id, 판정,
            f"재계산 {재계산값:,.2f} vs 저장값 {저장값:,.2f} (상대오차 {상대오차 * 100:.4f}%)",
            {"재계산값": 재계산값, "저장값": 저장값, "상대오차": 상대오차},
        ))

    return findings


# ---------------------------------------------------------------------------
# 4. 전월 대비 이상 변동
# ---------------------------------------------------------------------------

def _resolve_threshold(metric_id: str, metrics_catalog: dict, default_threshold: float):
    """지표 정의서의 "변동임계값"이 있으면 그 값을, 없으면 기본값을 쓴다.
    (임계값, 출처) 튜플을 반환한다 — 화면에 "정의서 지정"인지 "기본값"인지
    구분해서 보여줘야 하므로 출처도 함께 넘긴다."""
    metric_fm = metrics_catalog.get(metric_id, {})
    지표별_임계값 = metric_fm.get("변동임계값")
    if 지표별_임계값 is not None:
        return float(지표별_임계값), "정의서 지정"
    return default_threshold, "기본값"


def check_month_over_month(comparison_df, metrics_catalog, threshold=None) -> list:
    """상대변화율 절대값이 임계값 이상이면 경고.

    임계값은 지표마다 다를 수 있다 — metrics_catalog[metric_id]["변동임계값"]이
    있으면 그 값을, 없으면 threshold(기본 config.MOM_THRESHOLD=5.0)를 쓴다.
    "몇 %가 이상인가"는 지표 성격에 따라 다르다는 걸 avg_data_usage(계절 변동이
    커서 10.0)와 active_customers_contract(계약 기반이라 1.0)에서 실제로 확인했다
    — 하나의 전역 기준으로는 한쪽은 과다 경고, 다른 쪽은 과소 경고가 난다.

    "비교 불가"는 문제가 아니라 "볼 수 없다"는 사실이라 경고가 아니라 통과로
    남긴다(전월이 원래 없는 첫 달 같은 정상 상황일 수 있다)."""
    default_threshold = config.MOM_THRESHOLD if threshold is None else threshold

    findings = []
    for _, row in comparison_df.iterrows():
        metric_id = row["metric_id"]
        applied_threshold, 출처 = _resolve_threshold(metric_id, metrics_catalog, default_threshold)

        if row["비교상태"] == "비교 불가":
            findings.append(_finding(
                "전월 대비", metric_id, "통과",
                f"비교 불가({row.get('이유', '')}) — 이상 변동 여부를 판단할 수 없어 통과 처리",
                None,
            ))
            continue

        율 = row["상대변화율"]
        if 율 is None or 율 != 율:  # NaN
            findings.append(_finding(
                "전월 대비", metric_id, "통과",
                f"상대변화율 없음 (적용 임계값 {applied_threshold}%, {출처})", None,
            ))
            continue

        if abs(율) >= applied_threshold:
            findings.append(_finding(
                "전월 대비", metric_id, "경고",
                f"{율:+.2f}% (임계값 {applied_threshold}% 초과, {출처})",
                {"상대변화율": 율, "적용임계값": applied_threshold, "임계값_출처": 출처},
            ))
        else:
            findings.append(_finding(
                "전월 대비", metric_id, "통과",
                f"{율:+.2f}% (임계값 {applied_threshold}% 이내, {출처})",
                {"상대변화율": 율, "적용임계값": applied_threshold, "임계값_출처": 출처},
            ))
    return findings


# ---------------------------------------------------------------------------
# 5. 합계 대조 (세그먼트/그룹 지표용)
# ---------------------------------------------------------------------------

def check_totals(metrics_df) -> list:
    """cac_by_channel처럼 채널별로 여러 행을 반환하는 그룹 지표가 있으면,
    그룹 합과 전체 지표 값을 대조한다. 지금 이 앱의 계산 대상엔 그런 지표가
    없으므로(오늘 업로드가 그룹 지표를 건드리지 않음) "해당 없음"으로 통과
    반환한다 — 검사할 게 없는 것과 검사해서 통과한 것을 구분하기 위해 상세에
    이유를 명시한다."""
    if "group" in metrics_df.columns and metrics_df["group"].notna().any():
        # 향후 그룹 지표가 실제로 계산되는 날 채울 자리. 지금은 도달하지 않는다.
        return [_finding("합계 대조", "그룹 지표", "경고", "그룹 지표 대조 로직 미구현", None)]

    return [_finding("합계 대조", "전체", "통과", "해당 없음 (세그먼트 지표 없음)", None)]


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------

def validate_all(
    metrics_df, comparison_df, metrics_catalog, client,
    override: bool = False, dataset=None, table_override=None,
    uploaded_months=None, threshold=None,
) -> dict:
    """5종 검증을 모두 실행하고 하나의 결과로 합친다.

    "자동검증하지_않은_것"을 항상 반환하는 이유: 이 리스트가 빠지면 "검증
    통과"가 혼입·역인과·가설검정까지 다 확인했다는 뜻으로 오해될 수 있다
    (CLAUDE.md 6절). 리포트 한계 절에 그대로 들어갈 문구라 여기서 누락되면
    안 된다.
    """
    findings = []
    findings += check_valid_range(metrics_df, metrics_catalog, override)
    findings += check_min_sample(metrics_df)
    findings += check_derived_consistency(
        metrics_df, metrics_catalog, client, dataset, table_override,
        uploaded_months, override,
    )
    findings += check_month_over_month(comparison_df, metrics_catalog, threshold)
    findings += check_totals(metrics_df)

    차단수 = sum(1 for f in findings if f["판정"] == "차단")
    경고수 = sum(1 for f in findings if f["판정"] == "경고")

    if 차단수 > 0:
        전체판정 = "차단"
    elif 경고수 > 0:
        전체판정 = "경고"
    else:
        전체판정 = "통과"

    return {
        "전체판정": 전체판정,
        "차단수": 차단수,
        "경고수": 경고수,
        "항목별_결과": findings,
        "자동검증하지_않은_것": AUTO_SKIPPED,
    }
