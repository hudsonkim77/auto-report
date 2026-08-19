"""업로드 파일을 판정하는 모듈 — 5주차 워크플로우 3·4번(Profile·Confirm)을 담당한다.

CLAUDE.md 2단계("스키마 점검 → 지표 계산")의 앞부분이다. 이 모듈은 "이 파일이
어느 테이블이고, 어느 지표를 계산할 수 있는지"까지만 판정한다. 실제 BigQuery
적재·계산은 다루지 않는다(그건 calculate.py, 다음 프롬프트의 몫).

판정 항목과 워크플로우 대응(실습 D 표 그대로):

    어느 테이블인가         Confirm  — judge_table()
    필수 컬럼이 다 있는가    Confirm  — judge_table()
    행수·결측·중복          Profile  — profile_data()
    기간이 어디인가         Profile  — profile_data()
    그레인이 맞는가         Confirm  — profile_data()
    어느 지표를 계산할 수 있는가  Model 연결 — judge_metrics()

app.py 연결은 다음 프롬프트에서 한다. 여기서는 함수만 만든다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

PERIOD_COLUMN_PATTERNS = [
    re.compile(r"^year_month$"),
    re.compile(r".*_date$"),
    re.compile(r".*_month$"),
]

WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9_]+)\]\]")


# ---------------------------------------------------------------------------
# 함수 1 — judge_table: 어느 테이블인가 (Confirm)
# ---------------------------------------------------------------------------

def judge_table(df, schema_catalog: dict) -> dict:
    """업로드된 df의 컬럼 목록을 schema_catalog의 각 테이블과 대조해 가장 비슷한
    테이블을 찾는다.

    왜 "일치율 = 교집합 / 카탈로그 테이블 컬럼 수"인가:
        분모를 업로드 파일의 컬럼 수로 잡으면, 업로드 파일에 카탈로그가 모르는
        여분 컬럼이 섞여 있을 때(내부 관리용 컬럼 등) 그 사실만으로 일치율이
        떨어져 버린다. 반대로 카탈로그가 요구하는 필수 컬럼이 빠진 건 반드시
        걸려야 하므로, 분모는 "카탈로그 테이블 컬럼 수"로 고정한다.

    왜 컬럼 순서를 안 보는가:
        CLAUDE.md 5-1: "스키마 판정은 컬럼명·타입·필수 컬럼 존재 여부로 한다.
        컬럼 순서는 보지 않는다." CSV를 내보낸 도구에 따라 컬럼 순서는 얼마든지
        달라질 수 있고, 순서를 기준에 넣으면 같은 데이터인데 판정 불가로 떨어지는
        오탐이 생긴다.

    왜 일치율이 가장 높은 테이블 "하나"만 후보로 남기는가:
        여러 테이블이 동시에 임계값을 넘을 수 있다(컬럼 구성이 비슷한 테이블끼리).
        그래도 최종 판정은 하나여야 이후 단계(지표 판정)가 모호해지지 않으므로,
        최댓값 하나만 후보로 삼는다. 동점이면 schema_catalog를 순회하는 순서상
        먼저 나온 쪽이 남는다(실행마다 결과가 바뀌지 않도록 dict 순서에 의존).

    판정 실패(임계값 미달)해도 "가장 가까웠던 후보"는 그대로 반환한다.
    CLAUDE.md 9절 "카탈로그에 없는 테이블을 추측해 통과 금지"는 판정 실패인데도
    통과시키지 말라는 뜻이지, 실패 원인(어디가 얼마나 모자랐는지)을 숨기라는
    뜻이 아니다. `판정가능` 플래그만 보고 호출자가 신뢰 여부를 결정하면 된다.

    Returns:
        {
            "테이블명": str | None,      # 후보가 하나도 없으면 None
            "일치율": float,             # 0.0 ~ 1.0
            "누락_컬럼": list[str],      # 카탈로그엔 있는데 업로드엔 없음
            "추가_컬럼": list[str],      # 업로드엔 있는데 카탈로그엔 없음
            "판정가능": bool,            # 일치율 >= config.MIN_SCHEMA_MATCH
        }
    """
    upload_cols = {str(c).strip() for c in df.columns}

    best_table = None
    best_ratio = -1.0
    best_missing: list[str] = []
    best_extra: list[str] = []

    for table_name, info in schema_catalog.items():
        if table_name == "_meta":
            continue
        catalog_cols = {c["컬럼명"] for c in info.get("컬럼", [])}
        if not catalog_cols:
            continue

        intersection = upload_cols & catalog_cols
        ratio = len(intersection) / len(catalog_cols)

        if ratio > best_ratio:
            best_ratio = ratio
            best_table = table_name
            best_missing = sorted(catalog_cols - upload_cols)
            best_extra = sorted(upload_cols - catalog_cols)

    if best_table is None:
        return {
            "테이블명": None,
            "일치율": 0.0,
            "누락_컬럼": [],
            "추가_컬럼": sorted(upload_cols),
            "판정가능": False,
        }

    return {
        "테이블명": best_table,
        "일치율": round(best_ratio, 4),
        "누락_컬럼": best_missing,
        "추가_컬럼": best_extra,
        "판정가능": best_ratio >= config.MIN_SCHEMA_MATCH,
    }


# ---------------------------------------------------------------------------
# 함수 2 — profile_data: 행수·결측·기간·그레인 (Profile/Confirm)
# ---------------------------------------------------------------------------

def _detect_period_columns(df) -> list[str]:
    """year_month / *_date / *_month 패턴에 맞는 컬럼을 찾는다.
    여러 개일 수 있다(예: customers에는 join_date와 churn_date 둘 다 있다) —
    하나로 단정하지 않고 전부 후보로 남긴다."""
    cols = []
    for col in df.columns:
        name = str(col)
        if any(p.match(name) for p in PERIOD_COLUMN_PATTERNS):
            cols.append(name)
    return cols


def _extract_key_candidates_from_connection(connection_text: str, df_columns) -> list[str]:
    """'## 연결' 절 텍스트에서 백틱으로 감싼 컬럼명을 뽑아, 실제로 업로드 파일에
    있는 것만 그레인 후보 키로 쓴다.

    왜 텍스트에서 추정하는가: 스키마 카탈로그에 "이 테이블의 그레인 키는 X다"라는
    구조화된 필드가 없다. 위키 노트의 '## 연결' 절이 사람이 적어둔 유일한 단서라,
    거기서 실마리를 뽑는다. 다만 이건 텍스트 파싱으로 얻은 "추정"이므로 실제
    유일성 검사(중복 행 카운트)로 반드시 재확인한다 — 추정만 믿고 그레인을
    확정하지 않는다.
    """
    if not connection_text:
        return []
    tokens = re.findall(r"`([A-Za-z0-9_]+)`", connection_text)
    seen = set()
    candidates = []
    for t in tokens:
        if t in df_columns and t not in seen:
            seen.add(t)
            candidates.append(t)
    return candidates


def profile_data(df, table_info: dict) -> dict:
    """행수·컬럼수·결측·기간·그레인 후보를 점검한다.

    왜 결측 개수가 0인 컬럼은 생략하는가:
        컬럼이 8개든 80개든 전부 나열하면 "무엇이 문제인지"가 화면에 묻힌다.
        결측 0은 정상이므로 보고할 이유가 없다 — 이상이 있는 것만 눈에 띄어야
        한다(DESIGN.md 원칙: "이상한 값이 보이면 즉시 고친다"와 같은 맥락).

    왜 그레인을 "후보"로만 표시하고 하나로 단정하지 않는가:
        '## 연결' 텍스트에서 뽑은 키는 추정이고, customer_id + 기간컬럼 조합은
        가장 흔한 경우를 가정한 것뿐이다. 둘 다 후보로 나란히 보여주고, 각각의
        실제 중복 행 수(유일성 검사 결과)를 함께 표시해서 사람이 최종 판단하게
        한다. 스키마 노트가 그레인을 명시적으로 선언하지 않는 한 자동으로
        확정하지 않는다.

    Args:
        table_info: schema_catalog[테이블명] — judge_table()이 고른 테이블의
            카탈로그 엔트리(컬럼 목록·'연결' 텍스트 포함).

    Returns:
        {
            "행수": int,
            "컬럼수": int,
            "결측": {"컬럼명": 개수, ...},              # 0인 컬럼은 없음
            "기간컬럼": {"컬럼명": {"최소": str, "최대": str}, ...},
            "그레인_후보": [
                {"키": [...], "중복행수": int, "유일함": bool, "출처": str},
                ...
            ],
        }
    """
    result = {
        "행수": len(df),
        "컬럼수": len(df.columns),
    }

    # 결측 — 0인 것은 뺀다
    na_counts = df.isna().sum()
    result["결측"] = {
        str(col): int(n) for col, n in na_counts.items() if n > 0
    }

    # 기간 컬럼 — 문자열로 취급해 최소/최대를 구한다(YYYY-MM, YYYY-MM-DD는
    # 문자열 정렬이 곧 시간 순서와 같다).
    period_cols = _detect_period_columns(df)
    period_info = {}
    for col in period_cols:
        values = df[col].dropna().astype(str)
        if len(values) == 0:
            continue
        period_info[col] = {"최소": values.min(), "최대": values.max()}
    result["기간컬럼"] = period_info

    # 그레인 후보
    df_columns = set(str(c) for c in df.columns)
    candidates = []

    connection_text = table_info.get("연결", "") if table_info else ""
    linked_keys = _extract_key_candidates_from_connection(connection_text, df_columns)
    if linked_keys:
        dup = len(df) - len(df[linked_keys].drop_duplicates())
        candidates.append({
            "키": linked_keys,
            "중복행수": int(dup),
            "유일함": dup == 0,
            "출처": "연결 텍스트에서 추정",
        })

    if "customer_id" in df_columns and period_cols:
        combo = ["customer_id"] + period_cols
        # 연결 텍스트 후보와 완전히 같은 조합이면 중복 계산하지 않는다.
        if not any(set(c["키"]) == set(combo) for c in candidates):
            dup = len(df) - len(df[combo].drop_duplicates())
            candidates.append({
                "키": combo,
                "중복행수": int(dup),
                "유일함": dup == 0,
                "출처": "customer_id + 기간컬럼 조합",
            })

    result["그레인_후보"] = candidates
    return result


# ---------------------------------------------------------------------------
# 함수 3 — judge_metrics: 어느 지표를 계산할 수 있는가 (Model 연결)
# ---------------------------------------------------------------------------

def _links_in(value) -> list[str]:
    return WIKILINK_RE.findall(str(value))


def resolve_source_tables(metric_id: str, metrics_catalog: dict, _seen=None) -> set:
    """지표 하나가 최종적으로 의존하는 원천 테이블 집합을 재귀로 구한다.

    왜 재귀인가: 파생형·변화율형은 원천 테이블이 아니라 다른 지표를 참조한다
    (계산.분자/분모/기준지표에 [[metric_id]] 링크). 실제로 어느 테이블에서
    왔는지 알려면 그 링크를 계속 따라가 기초 지표(원천 테이블을 직접 쓰는
    지표)까지 내려가야 한다. calc_metrics.py의 compute_metric()이 같은 이유로
    재귀 구조인 것과 같은 이치다.

    _seen으로 순환을 막는다: 정의서 명세 오류로 A→B→A 순환이 생겨도 무한
    재귀에 빠지지 않고 빈 집합을 반환한다(잘못된 정의서를 이 함수가 죽는
    방식으로 알려주지 않기 위함 — 죽이는 건 calc_metrics.py의 validate_specs
    몫이다).
    """
    if _seen is None:
        _seen = set()
    if metric_id in _seen:
        return set()
    _seen.add(metric_id)

    metric = metrics_catalog.get(metric_id)
    if metric is None:
        return set()

    calc = metric.get("계산") or {}
    유형 = metric.get("유형", "")

    # 변화율형 — 기준지표를 따라간다
    if "시차" in calc:
        tables = set()
        for ref_id in _links_in(calc.get("기준지표", "")):
            tables |= resolve_source_tables(ref_id, metrics_catalog, _seen)
        return tables

    # 비율형 — 분자/분모가 각자 원천 블록(dict)이다
    분자 = calc.get("분자")
    분모 = calc.get("분모")
    if isinstance(분자, dict) or isinstance(분모, dict):
        tables = set()
        for leg in (분자, 분모):
            if isinstance(leg, dict):
                tables |= _tables_from_source_string(leg.get("원천", ""))
        return tables

    # 파생형 — 분자/분모가 다른 metric_id 링크다
    if 유형.startswith("파생형"):
        tables = set()
        for side in ("분자", "분모"):
            for ref_id in _links_in(calc.get(side, "")):
                tables |= resolve_source_tables(ref_id, metrics_catalog, _seen)
        return tables

    # 기초형(카운트형/금액형) — 원천 테이블을 직접 쓴다
    return _tables_from_source_string(calc.get("원천", ""))


def _tables_from_source_string(source: str) -> set:
    """'usage_history + customers' -> {'usage_history', 'customers'}."""
    return {t.strip() for t in str(source).split("+") if t.strip()}


def _parse_year_month(s: str):
    y, m = s.strip().split("-")
    return int(y), int(m)


def _parse_range(range_str):
    if not range_str or "~" not in str(range_str):
        return None
    start_s, end_s = (p.strip() for p in str(range_str).split("~"))
    return _parse_year_month(start_s), _parse_year_month(end_s)


def judge_metrics(table_name: str, period: dict, metrics_catalog: dict) -> list:
    """판정된 테이블 하나를 기준으로, metrics_catalog의 모든 지표에 대해
    "이 업로드 파일로 계산 가능한가"를 판정한다.

    비교 기준이 schema_catalog의 "테이블명"이지 "노트명"이 아닌 이유(개념 4절
    문제 ②): 지표 정의서의 계산.원천은 처음부터 테이블명(`usage_history`)으로
    쓰여 있다. 여기서 노트명(`data_usage_history`)과 비교하면 교집합이 항상
    비어 계산 대상이 0개로 나온다 — 이 사고가 실제로 났던 지점이라 명시적으로
    표시해 둔다.

    왜 원천에 다른 테이블이 더 있으면 "부분 갱신"으로 표시하는가:
        업로드 파일 하나로 갱신되는 테이블은 `table_name` 하나뿐이다. 지표가
        다른 테이블(예: customers)도 함께 쓴다면, 그 테이블은 예전 값 그대로
        계산에 들어간다. 계산 자체는 되지만 "최신 상태를 전부 반영한 값"은
        아니라는 걸 감춰선 안 된다(CLAUDE.md "문제②: 부분 갱신").

    왜 유효구간을 벗어나면 즉시 실패시키지 않고 "유효구간 확장 필요"로
    표시하는가:
        CLAUDE.md "문제①"의 세 가지 선택지 중 유일하게 허용되는 게 "실행 단위
        오버라이드"다. 이 함수는 판정만 하고 승인은 하지 않는다 — 위키
        정의서를 조용히 고치거나 앱이 멋대로 계산하지 않고, 사람이 게이트에서
        보고 승인할 수 있도록 상태만 보고한다.

    Args:
        table_name: judge_table()이 고른 테이블명.
        period: {"최소": "YYYY-MM", "최대": "YYYY-MM"} — profile_data()의
            기간컬럼 중 이 판정에 쓸 기간 컬럼 하나를 호출자가 골라 넘긴다.
        metrics_catalog: export_catalog.py가 만든 전체 지표 카탈로그.

    Returns:
        [
            {
                "metric_id": str,
                "지표명": str,
                "상태": "계산가능" | "유효구간 확장 필요" | "이 파일과 무관",
                "이유": str,
                "부분갱신여부": bool,
                "원천": list[str],
            },
            ...
        ]
    """
    upload_range = (_parse_year_month(period["최소"]), _parse_year_month(period["최대"]))

    results = []
    for metric_id, metric in metrics_catalog.items():
        if metric_id == "_meta":
            continue

        source_tables = resolve_source_tables(metric_id, metrics_catalog)
        지표명 = metric.get("지표명", metric_id)

        if table_name not in source_tables:
            results.append({
                "metric_id": metric_id,
                "지표명": 지표명,
                "상태": "이 파일과 무관",
                "이유": (
                    f"이 지표의 원천({', '.join(sorted(source_tables)) or '알 수 없음'})에 "
                    f"'{table_name}'가 없음"
                ),
                "부분갱신여부": False,
                "원천": sorted(source_tables),
            })
            continue

        other_tables = source_tables - {table_name}
        부분갱신 = len(other_tables) > 0

        valid_range = _parse_range(metric.get("유효구간"))
        if valid_range is None:
            상태 = "계산가능"
            이유 = "원천에 포함됨. 유효구간 선언 없음"
        else:
            (vstart, vend) = valid_range
            (ustart, uend) = upload_range
            if vstart <= ustart and uend <= vend:
                상태 = "계산가능"
                이유 = f"원천에 포함됨. 업로드 기간이 유효구간({metric.get('유효구간')}) 안"
            else:
                상태 = "유효구간 확장 필요"
                이유 = (
                    f"업로드 기간이 유효구간({metric.get('유효구간')})을 벗어남 — "
                    "정의서를 고치지 말고 실행 단위 오버라이드로 승인해야 계산됨"
                )

        if 부분갱신:
            이유 += f" · 부분 갱신(다른 원천 {', '.join(sorted(other_tables))}은 기존 값 그대로 사용)"

        results.append({
            "metric_id": metric_id,
            "지표명": 지표명,
            "상태": 상태,
            "이유": 이유,
            "부분갱신여부": 부분갱신,
            "원천": sorted(source_tables),
        })

    return results
