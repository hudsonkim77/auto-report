"""전월 대비 비교 모듈. calculate.py의 계산 엔진을 재사용해 전월 값을 구하고,
이미 계산된 당월 값과 나란히 비교한다.

이 모듈은 스테이징을 전혀 모른다 — 전월 데이터는 이미 원본 테이블에 들어있는
확정 데이터이므로, table_override={}로 원본 테이블 그대로 계산한다. 지표별
하드코딩 없이 calculate.py의 compute_metric()을 그대로 재사용한다(5주차부터
이어진 원칙: 새 상황이 생겨도 지표마다 코드를 늘리지 않는다).
"""

from __future__ import annotations

import pandas as pd

import config
from pipeline import calculate as calc


def previous_month(period: str) -> str:
    """"2025-01" -> "2024-12"처럼 한 달 전을 반환한다.

    직접 문자열을 손으로 파싱하지 않고 calculate.py의 parse_year_month/
    shift_year_month를 재사용한다 — 연도 경계(1월 -> 전년 12월) 처리가 이미
    거기서 검증됐으므로 같은 로직을 두 번 만들지 않는다.
    """
    year, month = calc.parse_year_month(period)
    py, pm = calc.shift_year_month(year, month, -1)
    return f"{py:04d}-{pm:02d}"


def calc_previous(metric_ids: list, prev_period: str, client) -> pd.DataFrame:
    """전월 값을 계산한다.

    staging_map(=table_override)을 항상 빈 dict로 넘기는 이유: 전월 데이터는
    이번 업로드와 무관하게 이미 원본 테이블에 존재하는 확정된 값이다. 스테이징
    테이블은 이번에 올라온 신규 기간(예: 2025-01)에만 의미가 있으므로, 전월
    계산에 스테이징을 섞으면 오히려 틀린 값이 나온다.

    extend_approved를 항상 False로 두는 이유: 전월은 이미 지나간, 정의서의
    유효구간 안에 있어야 정상인 달이다. 만약 유효구간 밖이라면(정의서 자체가
    그 달을 커버하지 않는다면) 그건 "확장을 승인해서 억지로 계산할 대상"이
    아니라 있는 그대로 "유효구간 밖"으로 남겨야 한다 — 이번 업로드에 대한
    사용자 승인을 다른 달(전월) 계산에 몰래 재사용하면 안 된다.
    """
    metrics_catalog = calc.load_metrics_catalog()
    year, month = calc.parse_year_month(prev_period)
    cache: dict = {}
    sql_log: list = []

    rows = []
    for metric_id in metric_ids:
        result = calc.compute_metric(
            metric_id, year, month, metrics_catalog, client, config.BQ_DATASET,
            table_override={}, uploaded_months=set(), extend_approved=False,
            cache=cache, sql_log=sql_log,
        )
        rows.append({
            "metric_id": result.metric_id,
            "지표명": result.지표명,
            "유형": result.유형,
            "값": result.value,
            "상태": result.status,
        })

    return pd.DataFrame(rows, columns=["metric_id", "지표명", "유형", "값", "상태"])


def metric_results_to_df(results: list) -> pd.DataFrame:
    """calculate.py의 MetricResult 리스트(당월 계산 결과)를 calc_previous()와
    같은 모양의 DataFrame으로 바꾼다. compare()에 넘길 current_df를 만드는
    용도 — 두 함수가 같은 컬럼 구조를 기대하므로 변환 규칙을 한 곳에 둔다."""
    rows = [{
        "metric_id": r.metric_id,
        "지표명": r.지표명,
        "유형": r.유형,
        "값": r.value,
        "상태": r.status,
    } for r in results]
    return pd.DataFrame(rows, columns=["metric_id", "지표명", "유형", "값", "상태"])


def compare(current_df: pd.DataFrame, previous_df: pd.DataFrame) -> pd.DataFrame:
    """당월/전월 결과를 metric_id로 조인해 변화를 계산한다.

    입력 스키마: 두 DataFrame 모두 calc_previous()/metric_results_to_df()가
    만드는 형태(metric_id, 지표명, 유형, 값, 상태)를 기대한다.

    전월 값을 0으로 채우지 않는 이유: 전월 값이 없다는 것("계산 안 됨"/"유효구간
    밖"/애초에 이 조인에 없음)과 "전월 실제 값이 0이었다"는 서로 다른 사실이다.
    0으로 채우면 상대변화율이 어마어마하게(또는 무한대로) 튀어서 착시를 만든다.
    예: usage_history의 첫 달(2024-01)을 업로드하면 전월(2023-12)은 애초에
    존재하지 않는 기간이라 "비교 불가"가 맞는 답이다.

    분모(전월 값)가 0이면 상대변화율을 None으로 두는 이유: 0으로 나누면
    ZeroDivisionError거나(값이 실제 0), 무한대로 튀거나 하는데 둘 다 의미
    있는 "변화율"이 아니다. 0에서 늘어난 건 상대적으로 몇 %인지 말할 수 없다.

    퍼센트포인트(%p) 변화를 비율형에서 따로 계산하는 이유: 비율형 지표의
    "값"은 0.05(=5%) 같은 소수다. 상대변화율(예: +40%)은 "원래 대비 얼마나
    늘었나"를 말하고, %p변화(예: +2%p)는 "그 비율 자체가 몇 포인트 움직였나"를
    말한다 — 이탈률이 5%→7%면 상대적으로는 40% 늘었지만 체감 규모는 2%p다.
    둘 다 의미가 달라 하나만 계산하면 다른 쪽 해석을 놓친다.
    """
    cur = current_df.rename(columns={"값": "당월"})
    prev = previous_df[["metric_id", "값", "상태"]].rename(
        columns={"값": "전월", "상태": "전월상태"}
    )
    merged = cur.merge(prev, on="metric_id", how="left")

    rows = []
    for _, row in merged.iterrows():
        당월 = row["당월"]
        전월 = row["전월"]
        전월상태 = row.get("전월상태")

        절대변화 = None
        상대변화율 = None
        pp변화 = None
        이유 = None

        전월없음 = pd.isna(전월) or (전월상태 is not None and 전월상태 not in ("OK", "구간확장"))
        당월없음 = pd.isna(당월)

        if 전월없음:
            비교상태 = "비교 불가"
            이유 = f"전월 상태={전월상태}" if 전월상태 not in (None, "OK") else "전월 값 없음"
        elif 당월없음:
            비교상태 = "비교 불가"
            이유 = "당월 값 없음"
        else:
            절대변화 = 당월 - 전월
            상대변화율 = None if 전월 == 0 else (절대변화 / 전월 * 100)
            if str(row["유형"]).startswith("비율형"):
                pp변화 = 절대변화 * 100
            비교상태 = "OK"

        entry = {
            "metric_id": row["metric_id"],
            "지표명": row["지표명"],
            "유형": row["유형"],
            "당월": None if 당월없음 else 당월,
            "전월": None if 전월없음 else 전월,
            "절대변화": 절대변화,
            "상대변화율": 상대변화율,
            "퍼센트포인트변화": pp변화,
            "비교상태": 비교상태,
        }
        if 이유:
            entry["이유"] = 이유
        rows.append(entry)

    columns = ["metric_id", "지표명", "유형", "당월", "전월", "절대변화",
               "상대변화율", "퍼센트포인트변화", "비교상태", "이유"]
    return pd.DataFrame(rows).reindex(columns=columns)
