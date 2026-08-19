"""추이 데이터 준비 모듈. 대시보드 차트 영역에 쓸 월별 시계열을 만든다.

calculate.py의 compute_metric()을 그대로 재사용한다 — 지표별로 새 계산 로직을
만들지 않는다(5주차부터 이어진 원칙).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from pipeline import calculate as calc  # noqa: E402

import pandas as pd


def build_trend(metric_ids, end_period, months, staging_map, client,
                 extend_approved=False, on_progress=None) -> pd.DataFrame:
    """end_period부터 거꾸로 months개월치 추이를 계산한다.

    당월(end_period)에만 staging_map을 적용하는 이유: 스테이징 테이블에는 이번에
    올라온 한 달치 데이터만 있다. 그 이전 달까지 스테이징으로 조회하면 거기 없는
    데이터를 찾다가 조용히 빈 값이 나온다 — calculate.py의 table_override를
    "업로드 기간에 속한 월에만" 적용하도록 고쳤던 것과 원인이 같다(실측으로 이미
    한 번 잡은 버그). 그래서 당월 이전 달은 항상 빈 dict(원본 테이블)로 계산한다.

    유효구간 밖인 달을 0이 아니라 None으로 두는 이유: 이 값은 그대로 차트에
    들어간다. 0으로 채우면 꺾은선이 급락한 것처럼 보여 "그 달에 실제로 무너졌다"는
    착시를 만든다. None(NaN)으로 두면 차트 라이브러리가 그 구간의 선을 끊어
    그리므로 "값이 없다"와 "값이 0이다"가 시각적으로도 구분된다.

    extend_approved는 당월(end_period)에만 적용한다 — 게이트에서 받은 승인은
    "이번 실행"의 계산 대상 기간에 한정된 것이지, 차트가 보여주는 과거 달까지
    소급 적용할 근거가 없다. 과거 달은 항상 정의서의 유효구간을 그대로 따른다.

    캐시: (metric_id, year, month) 조합은 함수 호출 1번 안에서 한 번만 계산한다.
    여러 지표가 같은 의존 지표·같은 달을 참조해도(예: 파생지표 두 개가 같은
    기초지표를 씀) 중복 쿼리를 보내지 않는다.
    """
    metrics_catalog = calc.load_metrics_catalog()

    end_year, end_month = calc.parse_year_month(end_period)
    end_key = (end_year, end_month)

    months_list = []
    y, m = end_year, end_month
    for _ in range(months):
        months_list.append((y, m))
        y, m = calc.shift_year_month(y, m, -1)
    months_list.reverse()  # 과거 -> 현재 순으로 정렬해서 반환

    cache: dict = {}
    sql_log: list = []
    rows = []

    total_steps = len(metric_ids) * len(months_list)
    done = 0

    for metric_id in metric_ids:
        for (year, month) in months_list:
            is_current = (year, month) == end_key
            table_override = staging_map if is_current else {}
            uploaded_months = {end_key} if is_current else set()
            approved_for_month = extend_approved if is_current else False

            result = calc.compute_metric(
                metric_id, year, month, metrics_catalog, client, config.BQ_DATASET,
                table_override, uploaded_months, approved_for_month, cache, sql_log,
            )

            rows.append({
                "metric_id": metric_id,
                "month": f"{year:04d}-{month:02d}",
                "value": result.value,  # 유효구간 밖이면 None — 0으로 채우지 않는다
                "status": result.status,
            })

            done += 1
            if on_progress:
                on_progress(done, total_steps)

    return pd.DataFrame(rows, columns=["metric_id", "month", "value", "status"])
