"""8장 구조 월간 리포트를 마크다운으로 생성하는 모듈.

CLAUDE.md 1절 금지: "원인 분석·개선 제안 자동 작성"을 이 모듈이 대신하지 않는다.
2·5·6장(배경·원인분석·개선제안)은 판단이 필요해 사람이 쓰는 자리로 비워두고,
3·4·7·8장(사실 서술)만 자동으로 채운다. 1·7·8장 중 지금 구현된 건 1장뿐이고
7·8장은 아직 제목만 둔다(다음 프롬프트).

문장 만들기(값 서식·전월 대비 서술·금지 표현 검사)는 이 모듈이 직접 하지 않고
pipeline/phrasing.py에 전부 맡긴다 — "숫자를 문장으로 바꾸는 규칙"이 report.py와
phrasing.py 두 곳에 따로 생기면 반드시 어긋난다(예: 이전에 이 파일 안에 있던
_fmt/_fmt_delta가 common.py의 서식과 별도로 존재했던 것 — phrasing.py가 생기면서
정리했다). report.py는 어떤 지표를 어떤 순서로, 어느 장에 넣을지만 결정한다.
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import re
import sys
from pathlib import Path

from fpdf import FPDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from common import COLOR_SLATE  # noqa: E402
from pipeline import phrasing  # noqa: E402
from pipeline import manual_sections  # noqa: E402

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
FONT_REGULAR = FONT_DIR / "NotoSansKR-Regular.ttf"
FONT_BOLD = FONT_DIR / "NotoSansKR-Bold.ttf"


# ---------------------------------------------------------------------------
# 공용 유틸
# ---------------------------------------------------------------------------

def _summarize_calc(metric_id: str, metrics_catalog: dict) -> str:
    """지표 하나의 계산 명세를 "분자/분모/기간" 한 줄로 줄인다. 유형 4갈래
    (기초형/비율형/파생형/변화율형)마다 계산 스펙의 모양이 달라 분기한다 —
    calculate.py의 is_base_type 등 판별 로직과 같은 기준을 쓴다."""
    fm = metrics_catalog.get(metric_id, {})
    calc = fm.get("계산") or {}
    유형 = fm.get("유형", "")
    기간_단위 = (fm.get("기간") or {}).get("단위", "월")

    if "시차" in calc:
        기준 = calc.get("기준지표", "")
        시차 = calc.get("시차")
        return f"기준지표 {기준}, {abs(시차)}{기간_단위} 전 대비 변화율, 기간단위 {기간_단위}"

    분자 = calc.get("분자")
    분모 = calc.get("분모")
    if isinstance(분자, dict) or isinstance(분모, dict):
        분자원천 = (분자 or {}).get("원천", "")
        분모원천 = (분모 or {}).get("원천", "")
        return f"분자 원천 {분자원천} / 분모 원천 {분모원천}, 기간단위 {기간_단위}"

    if 유형.startswith("파생형"):
        return f"분자 {분자} / 분모 {분모}, 기간단위 {기간_단위}"

    return f"원천 {calc.get('원천', '')} / 집계 {calc.get('집계', '')}, 기간단위 {기간_단위}"


def _threshold_map(validation: dict) -> dict:
    """검증 결과에서 "전월 대비" 검증이 지표별로 실제 적용한 임계값을 뽑는다.

    validate.py의 _resolve_threshold(정의서 지정 vs 기본값 판단)를 여기서 다시
    구현하지 않는 이유: 그 로직은 이미 check_month_over_month가 한 번 실행해서
    각 finding의 "값" 안에 적용임계값으로 남겨뒀다. 같은 판단을 report.py가
    따로 하면 validate.py가 쓴 임계값과 report.py가 문장에 적는 임계값이
    어긋날 수 있다 — 검증에 쓴 기준과 리포트에 적히는 기준은 항상 같아야 한다.
    """
    result = {}
    for f in (validation or {}).get("항목별_결과", []):
        if f.get("검증명") != "전월 대비":
            continue
        v = f.get("값")
        if isinstance(v, dict) and "적용임계값" in v:
            result[f.get("대상지표")] = v["적용임계값"]
    return result


def _change_magnitude(row) -> float:
    """정렬 전용 — "변동이 크다"의 기준을 상대변화율 절대값으로 통일한다.
    비교 불가/분모 0(상대변화율 없음)은 크기를 매길 근거가 없으므로 맨 뒤로
    보낸다."""
    율 = row.get("상대변화율")
    if 율 is None or (isinstance(율, float) and 율 != 율):
        return -1.0
    return abs(율)


def _top_movers(comparison_df, validation: dict, n: int = 5) -> list:
    """전월 대비 변동이 큰 지표부터 최대 n개의 (metric_id, 문장) 목록을 만든다.
    문장 자체는 phrasing.describe_change가 만들고, 여기서는 "어떤 지표를 어느
    순서로 보여줄지"만 정한다."""
    if comparison_df is None or len(comparison_df) == 0:
        return []

    thresholds = _threshold_map(validation)
    rows = sorted(
        (row for _, row in comparison_df.iterrows()),
        key=_change_magnitude,
        reverse=True,
    )

    movers = []
    for row in rows[:n]:
        threshold = thresholds.get(row["metric_id"], config.MOM_THRESHOLD)
        movers.append((row["metric_id"], phrasing.describe_change(row, threshold)))
    return movers


# ---------------------------------------------------------------------------
# 문서 머리말
# ---------------------------------------------------------------------------

def _period_label(run_log: dict) -> str:
    """run_log의 "기간"(예: "2024-12 ~ 2024-12")에서 대상기간 한 조각만
    뽑는다. 문서 제목에 쓰는 값과 manual_sections.load_manual()에 넘기는
    period가 어긋나면 안 되므로, 이 파싱을 한 곳에만 둔다."""
    기간 = run_log.get("기간", "")
    return 기간.split("~")[0].strip() if 기간 else "?"


def _build_header(run_context: dict) -> str:
    run_log = run_context.get("run_log") or {}
    metrics_catalog = run_context.get("metrics_catalog") or {}

    기간 = run_log.get("기간", "")
    period_label = _period_label(run_log)
    생성일시 = dt.datetime.now().isoformat()
    카탈로그_생성일시 = metrics_catalog.get("_meta", {}).get("생성일시", "알 수 없음")

    return (
        f"# 월간 지표 리포트 — {period_label}\n\n"
        f"- 생성일시: {생성일시}\n"
        f"- 대상 기간: {기간}\n"
        f"- 생성 도구: auto-report\n"
        f"- 카탈로그 버전(생성일시): {카탈로그_생성일시}\n\n"
        f"> 이 문서는 자동 생성되었으며, 2·5·6장은 사람이 작성하는 장입니다."
    )


# ---------------------------------------------------------------------------
# 1. Executive Summary — 자동 3절 + 사람 1절
# ---------------------------------------------------------------------------

def section_1_summary(run_context: dict) -> str:
    """무엇을 계산했는가·무엇이 변했는가·검증 상태 세 절은 3·4·8장에 이미
    있는 사실을 요약해서 앞머리에 다시 보여주는 것뿐이다 — 여기서 새로
    판단하지 않는다. "그래서 무엇이 중요한가"(핵심 시사점)만 사람이 쓴다."""
    run_log = run_context.get("run_log") or {}
    comparison_df = run_context.get("comparison")
    validation = run_context.get("validation") or {}

    lines = ["## 1. Executive Summary", ""]

    lines.append("### 무엇을 계산했는가")
    lines.append("")
    lines.append(f"- 대상 기간: {run_log.get('기간', '—')}")
    lines.append(f"- 계산 지표 개수: {len(run_log.get('계산_대상_지표') or [])}개")
    lines.append("")

    lines.append("### 무엇이 변했는가")
    lines.append("")
    movers = _top_movers(comparison_df, validation)
    if movers:
        for _, sentence in movers:
            lines.append(f"- {sentence}")
    else:
        lines.append("- 전월과 비교할 수 있는 지표가 없습니다.")
    lines.append("")

    lines.append("### 검증 상태")
    lines.append("")
    lines.append(f"- 전체판정: {validation.get('전체판정', '—')}")
    lines.append(f"- 차단 {validation.get('차단수', 0)}건, 경고 {validation.get('경고수', 0)}건")
    lines.append("")

    lines.append("### 핵심 시사점")
    lines.append("")
    lines.append("> 이 소절은 사람이 작성합니다.")
    lines.append(">")
    lines.append("> 힌트: 위 사실 중 이번 리포트에서 가장 눈여겨봐야 할 것을 한두 문장으로 짚습니다.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. 배경·목적 — 사람이 쓴다
# ---------------------------------------------------------------------------

def section_2_background(run_context: dict) -> str:
    return (
        "## 2. 배경·목적\n\n"
        "> 이 장은 사람이 작성합니다.\n"
        ">\n"
        "> 힌트: 이번 실행이 어떤 질문(위키 `01_questions/`)에 답하기 위한 것인지, "
        "왜 지금 이 데이터가 필요했는지 적습니다."
    )


# ---------------------------------------------------------------------------
# 3. 데이터·방법론 — 자동
# ---------------------------------------------------------------------------

def section_3_data_methodology(run_context: dict) -> str:
    """대상 파일·기간·행수, 계산한 지표 목록과 정의 요약, 카탈로그 메타,
    부분 갱신 여부를 사실만 나열한다. 판단·해석 문장을 넣지 않는다."""
    run_log = run_context.get("run_log") or {}
    metrics_df = run_context.get("metrics")
    metrics_catalog = run_context.get("metrics_catalog") or {}

    lines = ["## 3. 데이터·방법론", ""]
    lines.append(f"- 대상 파일명: {run_log.get('파일명', '—')}")
    lines.append(f"- 판정 테이블: {run_log.get('판정_테이블', '—')}")
    lines.append(f"- 기간: {run_log.get('기간', '—')}")
    lines.append(f"- 행수: {run_log.get('행수', '—')}")
    lines.append("")

    meta = metrics_catalog.get("_meta", {})
    lines.append(f"- 카탈로그 생성일시: {meta.get('생성일시', '알 수 없음')}")
    lines.append(f"- 카탈로그 지표 총 개수: {meta.get('항목_개수', '?')}종")
    lines.append("")

    lines.append("### 계산한 지표")
    lines.append("")
    lines.append("| metric_id | 지표명 | 계산 명세 요약 |")
    lines.append("|---|---|---|")

    if metrics_df is not None and len(metrics_df) > 0:
        metric_ids = list(metrics_df["metric_id"])
    else:
        metric_ids = run_log.get("계산_대상_지표", [])

    for metric_id in metric_ids:
        지표명 = metrics_catalog.get(metric_id, {}).get("지표명", metric_id)
        요약 = _summarize_calc(metric_id, metrics_catalog)
        lines.append(f"| {metric_id} | {지표명} | {요약} |")
    lines.append("")

    부분갱신 = run_log.get("부분_갱신_지표") or []
    if 부분갱신:
        판정테이블 = run_log.get("판정_테이블", "")
        other_tables = set()
        if metrics_df is not None:
            for _, row in metrics_df.iterrows():
                if row["metric_id"] in 부분갱신:
                    for t in str(row.get("원천", "")).split("+"):
                        if t and t != 판정테이블:
                            other_tables.add(t)
        names = ", ".join(부분갱신)
        tables = ", ".join(sorted(other_tables)) if other_tables else "확인되지 않음"
        lines.append(f"- 부분 갱신 지표: {names}. {tables} 테이블은 이번 실행에서 갱신되지 않았습니다.")
    else:
        lines.append("- 부분 갱신 지표: 없습니다.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. 현황 — 자동
# ---------------------------------------------------------------------------

def section_4_status(run_context: dict) -> str:
    """지표별 당월·전월·변화·변화율 표. 비율 지표는 %p(변화 칸)와 %(변화율 칸)를
    둘 다 적는다 — 절대 크기와 상대 크기가 다른 이야기라는 걸 6주차 Day3에서
    이미 확인했다(같은 이유로 app.py 대시보드 표와 동일한 구분을 쓴다).

    표 위 요약 문장과 1장의 "무엇이 변했는가"가 같은 _top_movers/describe_change
    결과를 쓴다 — 1장은 전체 요약이고 4장은 그 요약이 어느 표에서 나왔는지 바로
    아래에서 보여주는 자리라, 서로 다른 지표를 뽑거나 다른 문장을 쓰면 안 된다.
    """
    comparison_df = run_context.get("comparison")
    validation = run_context.get("validation") or {}

    lines = ["## 4. 현황", ""]

    if comparison_df is None or len(comparison_df) == 0:
        lines.append("계산된 지표가 없습니다.")
        return "\n".join(lines)

    movers = _top_movers(comparison_df, validation)
    if movers:
        for _, sentence in movers:
            lines.append(f"- {sentence}")
        lines.append("")

    lines.append("| 지표명 | 당월 | 전월 | 변화 | 변화율 |")
    lines.append("|---|---|---|---|---|")

    for _, row in comparison_df.iterrows():
        지표명 = row["지표명"]
        유형 = row["유형"]

        if row["비교상태"] == "비교 불가":
            이유 = row.get("이유", "")
            lines.append(f"| {지표명} | — | — | 비교 불가({이유}) | — |")
            continue

        당월 = phrasing.fmt_value(row["당월"], 유형, 지표명)
        전월 = phrasing.fmt_value(row["전월"], 유형, 지표명)

        pp = row.get("퍼센트포인트변화")
        has_pp = pp is not None and not (isinstance(pp, float) and pp != pp)
        if has_pp:
            변화 = f"{pp:+.1f}%p"
        else:
            절대변화 = row["절대변화"]
            if 절대변화 is None:
                변화 = "—"
            else:
                부호 = "+" if 절대변화 > 0 else "-" if 절대변화 < 0 else ""
                변화 = f"{부호}{phrasing.fmt_value(abs(절대변화), 유형, 지표명)}"

        율 = row["상대변화율"]
        변화율 = f"{율:+.2f}%" if (율 is not None and not (isinstance(율, float) and 율 != 율)) else "—"

        lines.append(f"| {지표명} | {당월} | {전월} | {변화} | {변화율} |")

    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. 원인 분석 — 본문은 사람이 쓴다, 인용만 자동으로 붙인다
# ---------------------------------------------------------------------------

def _exceeded_metric_ids(validation: dict) -> list:
    """"변동이 크다"의 기준을 여기서 새로 정하지 않는다 — validate.py의
    check_month_over_month가 이미 "경고"로 판정한 지표 목록을 그대로 쓴다.
    검증에서 통과라고 한 지표를 리포트에서 "변동이 크다"고 다시 부르면 3단계
    검증과 6단계 리포트의 판정 기준이 어긋난다."""
    return [
        f["대상지표"] for f in (validation or {}).get("항목별_결과", [])
        if f.get("검증명") == "전월 대비" and f.get("판정") == "경고"
    ]


def _related_insights(metric_id: str, metrics_catalog: dict, insights_catalog: dict) -> list:
    """관련 인사이트를 찾는다. 1순위는 정의서 본문의 [[i-XXX]] 링크(사람이 그
    지표를 설명하면서 직접 걸어둔 연결이라 가장 근거가 세다). 그게 하나도
    없을 때만 2순위로 넘어가 지표 tags와 인사이트 tags의 교집합을 본다 —
    태그가 하나도 안 겹치는 인사이트까지 끌어오면 무관한 인용이 섞인다.
    """
    fm = metrics_catalog.get(metric_id, {})
    linked_ids = fm.get("관련인사이트_본문링크") or []
    direct = [(iid, insights_catalog[iid]) for iid in linked_ids if iid in insights_catalog]
    if direct:
        return direct

    metric_tags = set(fm.get("tags") or [])
    if not metric_tags:
        return []

    matched = []
    for insight_id, entry in insights_catalog.items():
        if insight_id == "_meta":
            continue
        if metric_tags & set(entry.get("tags") or []):
            matched.append((insight_id, entry))
    return matched


def _cite_insight(insight_id: str, entry: dict) -> str:
    """인사이트 한 건을 인용 블록으로 만든다. 제목·confidence·시사점 3줄·
    노트명만 옮기고 결론은 쓰지 않는다 — "그래서 원인은 이거다"까지 이 함수가
    쓰면, 5장 본문을 사람 자리로 비워둔 이유(CLAUDE.md 6절: 판단은 사람이
    한다)가 무의미해진다."""
    제목 = entry.get("제목", insight_id)
    confidence = entry.get("confidence", "—")
    시사점 = entry.get("시사점") or ""

    # "**"를 앞쪽만 lstrip하면 "- **문장.** 나머지" 같은 줄에서 여는 마크만
    # 지워지고 닫는 "**"가 문장 중간에 그대로 남는다(실측으로 확인함 — i-008
    # 인용에서 "부족하다.** \"자동이체..." 처럼 깨졌다). 볼드를 살릴 필요가
    # 없는 요약 줄이라 여닫는 마크를 전부 지운다.
    요약줄 = []
    for l in 시사점.splitlines():
        l = l.strip()
        if not l:
            continue
        l = l.lstrip("-").strip().replace("**", "")
        요약줄.append(l)
        if len(요약줄) == 3:
            break

    lines = [f"- **{제목}** (confidence: {confidence}, 노트: `{insight_id}`)"]
    for line in 요약줄:
        lines.append(f"  - {line}")
    return "\n".join(lines)


def section_5_causes(run_context: dict) -> str:
    """5장 본문은 여전히 사람이 쓴다. 그 아래 "참고" 소절에 임계값을 초과한
    지표별로 위키 인사이트를 인용만 해서 붙인다 — 인용까지는 사실(위키에 이런
    분석이 있다)이고, 그 인용으로 원인을 판정하는 건 사람이 할 일이다.

    run_context에 "insights_catalog"가 없으면(아직 그 카탈로그를 안 넘기는
    호출자) 모든 지표를 "관련 분석 없음"으로 처리한다 — 없는 카탈로그를
    있는 것처럼 추측해서 인용을 만들어내지 않는다.
    """
    metrics_catalog = run_context.get("metrics_catalog") or {}
    insights_catalog = run_context.get("insights_catalog") or {}
    validation = run_context.get("validation") or {}

    lines = [
        "## 5. 원인 분석",
        "",
        "> 이 장은 사람이 작성합니다.",
        "> 아래 관련 분석을 근거로 원인을 판정하세요.",
        "",
        "### 참고 — 위키에서 찾은 관련 분석",
        "",
    ]

    metric_ids = _exceeded_metric_ids(validation)
    if not metric_ids:
        lines.append("이번 실행에서 임계값을 초과한 지표가 없어 인용할 대상이 없습니다.")
        return "\n".join(lines)

    for metric_id in metric_ids:
        지표명 = metrics_catalog.get(metric_id, {}).get("지표명", metric_id)
        lines.append(f"**{지표명}**")
        lines.append("")

        related = _related_insights(metric_id, metrics_catalog, insights_catalog)
        if not related:
            lines.append("관련 분석 없음")
        else:
            for insight_id, entry in related:
                lines.append(_cite_insight(insight_id, entry))
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. 개선 제안 — 사람이 쓴다
# ---------------------------------------------------------------------------

def section_6_recommendations(run_context: dict) -> str:
    return (
        "## 6. 개선 제안\n\n"
        "> 이 장은 사람이 작성합니다.\n"
        ">\n"
        "> 힌트: 임팩트·난이도 기준으로 우선순위를 매기고, confidence가 낮은 "
        "근거로 나온 제안은 별도로 표시합니다."
    )


# ---------------------------------------------------------------------------
# 7. 한계 — 자동. 재료가 없는 소절은 만들지 않는다.
# ---------------------------------------------------------------------------

def _limits_valid_range(metrics_df, metrics_catalog) -> str:
    """구간확장 상태로 계산된 지표 — 정의서 범위 밖을 이번 실행에서만 승인해
    계산했다는 사실. "확장은 이번 실행에만..." 문장을 항상 붙일 수 있는 이유는
    calculate.py가 구간확장 상태를 extend_approved=True일 때만 매기기 때문이다
    (calculate.py의 compute_metric 참고) — 매번 다시 확인하지 않아도 되는 코드
    수준의 불변식이다."""
    if metrics_df is None or len(metrics_df) == 0:
        return None
    rows = metrics_df[metrics_df["status"] == "구간확장"]
    if len(rows) == 0:
        return None

    lines = ["### 7-1. 유효구간", ""]
    for _, row in rows.iterrows():
        metric_id = row["metric_id"]
        지표명 = row["지표명"]
        유효구간 = metrics_catalog.get(metric_id, {}).get("유효구간", "—")
        lines.append(
            f"- **{지표명}**: 정의서 유효구간({유효구간})을 넘어선 {row['month']}을 계산했다. "
            f"확장은 이번 실행에만 승인되었고 위키 정의서는 변경되지 않았다."
        )
    lines.append("")
    return "\n".join(lines)


def _limits_partial_refresh(metrics_df, run_log) -> str:
    """부분 갱신 지표 — 갱신 안 된 다른 테이블이 "현재 몇 일자 상태인지"는 이
    run_context 안에 없다(그 값을 알려면 원본 테이블을 다시 조회해야 하는데,
    report.py는 이미 계산된 재료를 조립하는 자리라 여기서 새로 쿼리하지
    않는다). 그래서 예시 문장의 "churn_date 최대 2024-12-28" 같은 구체적 날짜는
    만들어내지 않고, 갱신 여부와 대상 테이블만 사실대로 적는다."""
    부분갱신 = run_log.get("부분_갱신_지표") or []
    if not 부분갱신:
        return None

    판정테이블 = run_log.get("판정_테이블", "")
    기간 = run_log.get("기간", "")

    lines = ["### 7-2. 데이터 갱신 범위", ""]
    for metric_id in 부분갱신:
        지표명 = metric_id
        other_tables = set()
        if metrics_df is not None:
            match = metrics_df[metrics_df["metric_id"] == metric_id]
            if len(match) > 0:
                지표명 = match.iloc[0]["지표명"]
                for t in str(match.iloc[0].get("원천", "")).split("+"):
                    if t and t != 판정테이블:
                        other_tables.add(t)
        tables = ", ".join(sorted(other_tables)) if other_tables else "다른 원천 테이블"
        lines.append(
            f"- **{지표명}**: {판정테이블} 외에 {tables}도 함께 쓴다. "
            f"이번 실행({기간})은 {판정테이블}만 갱신했고, {tables}은 이전 실행 상태 그대로다."
        )
    lines.append("")
    return "\n".join(lines)


def _limits_unautomated_checks(validation: dict) -> str:
    """validate.py의 AUTO_SKIPPED를 그대로 옮긴다 — "무엇을 안 봤는지"를
    report.py가 다시 판단하지 않는다."""
    skipped = (validation or {}).get("자동검증하지_않은_것") or []
    if not skipped:
        return None

    lines = [
        "### 7-3. 수행하지 않은 검증",
        "",
        "이 리포트의 검증은 기계적 점검만 수행했다.",
        "",
    ]
    for s in skipped:
        lines.append(f"- **{s.get('항목', '')}**: {s.get('이유', '')}")
    lines.append("")
    return "\n".join(lines)


def _limits_tentative_thresholds(metric_ids: list, metrics_catalog: dict) -> str:
    """정의서 프론트매터의 임계값_상태가 "잠정"인 계산 대상 지표만 옮긴다.
    본문 설명은 metrics_catalog[mid]["임계값_근거"](export_catalog.py가 "###
    임계값 근거" 절에서 뽑아온 값)를 그대로 인용한다 — 이 함수가 근거를
    요약하거나 재구성하지 않는다."""
    lines = []
    for metric_id in metric_ids:
        fm = metrics_catalog.get(metric_id, {})
        if fm.get("임계값_상태") != "잠정":
            continue
        지표명 = fm.get("지표명", metric_id)
        근거 = fm.get("임계값_근거")
        lines.append(f"- **{지표명}**" + (f": {근거}" if 근거 else ""))

    if not lines:
        return None
    return "\n".join(["### 7-4. 잠정 기준", ""] + lines + [""])


def _limits_per_metric(metric_ids: list, metrics_catalog: dict) -> str:
    """계산된 지표에 한해서만 metrics_catalog의 "답할 수 없는 것" 절을 소절로
    묶는다. 계산 대상이 아니었던 지표(카탈로그엔 있지만 이번 실행이 건드리지
    않은 지표)까지 넣으면 "이번 리포트의 한계"가 아니라 "카탈로그 전체의
    한계"가 되어 범위가 어긋난다."""
    blocks = []
    for metric_id in metric_ids:
        fm = metrics_catalog.get(metric_id, {})
        답할_수_없는_것 = fm.get("답할_수_없는_것")
        if not 답할_수_없는_것:
            continue
        지표명 = fm.get("지표명", metric_id)
        blocks.append(f"**{지표명}**\n\n{답할_수_없는_것}")

    if not blocks:
        return None
    return "### 7-5. 지표별 한계\n\n" + "\n\n".join(blocks) + "\n"


def _limits_low_sample(metrics_df) -> str:
    if metrics_df is None or len(metrics_df) == 0:
        return None
    rows = metrics_df[metrics_df["status"] == "표본부족"]
    if len(rows) == 0:
        return None

    lines = ["### 7-6. 표본 부족 지표", ""]
    for _, row in rows.iterrows():
        lines.append(
            f"- **{row['지표명']}**: 표본 {row['sample_size']:.0f}건으로 "
            f"최소표본({row['min_sample']:.0f}) 미달이다."
        )
    lines.append("")
    return "\n".join(lines)


def section_7_limitations(run_context: dict) -> str:
    """6가지 재료를 각각 조립해 소절로 붙인다. 재료가 없으면(예: 이번 실행에
    구간확장·부분갱신·표본부족 지표가 하나도 없으면) 그 소절 자체를 만들지
    않는다 — 빈 "### 7-1. 유효구간" 제목만 남기면 "확인해봤는데 없었다"와
    "확인 안 했다"가 구분이 안 된다. 문서 구조([구조] 목록의 5개 표제)에는
    없지만 재료 목록 6번(표본 관련)이 따로 있어 7-6으로 이어 붙인다.
    """
    run_log = run_context.get("run_log") or {}
    metrics_df = run_context.get("metrics")
    metrics_catalog = run_context.get("metrics_catalog") or {}
    validation = run_context.get("validation") or {}

    if metrics_df is not None and len(metrics_df) > 0:
        metric_ids = list(metrics_df["metric_id"])
    else:
        metric_ids = run_log.get("계산_대상_지표") or []

    subsections = [
        _limits_valid_range(metrics_df, metrics_catalog),
        _limits_partial_refresh(metrics_df, run_log),
        _limits_unautomated_checks(validation),
        _limits_tentative_thresholds(metric_ids, metrics_catalog),
        _limits_per_metric(metric_ids, metrics_catalog),
        _limits_low_sample(metrics_df),
    ]
    subsections = [s for s in subsections if s]

    if not subsections:
        return "## 7. 한계\n\n이번 실행에서 자동으로 채울 한계 항목이 없습니다."

    return "## 7. 한계\n\n" + "\n\n".join(subsections)


# ---------------------------------------------------------------------------
# 8. 부록 — 다음 프롬프트
# ---------------------------------------------------------------------------

def section_8_appendix(run_context: dict) -> str:
    return "## 8. 부록\n\n(다음 프롬프트에서 구현)"


# ---------------------------------------------------------------------------
# 개발용 자체 검사 — 실제 운영에서는 로그로만
# ---------------------------------------------------------------------------

def _dev_forbidden_notice(findings: list) -> str:
    """phrasing.check_forbidden이 걸어낸 표현을 리포트 맨 아래에 붙인다.

    운영 버전에서 이 블록을 화면에 그대로 남기면 안 되는 이유: "이 리포트에
    금칙어가 있다"는 안내 자체가 완성된 리포트에 남아 있으면 그 리포트를 받는
    사람이 이상하게 여긴다. 실제로는 로그에만 남기고 화면에서는 지워야 한다.
    지금은 학습 단계라 규칙이 실제로 어떤 문장에 걸리는지 눈으로 보이게
    남겨둔다(다음 단계에서 로깅으로 옮길 자리라는 걸 표시해 둔다).
    """
    lines = [
        "---",
        "",
        "> **[개발용, 운영 시 로그로만] 금지 표현 자체 검사에서 발견된 표현**",
        "",
    ]
    for f in findings:
        lines.append(f"- [{f['분류']}] \"{f['표현']}\" (문서 내 위치 {f['위치']})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PDF — CLAUDE.md 7절 하드 제약: fpdf2 + Noto Sans KR 서브셋, 차트 이미지 없음
# (kaleido가 이 환경에서 불안정하다) — 텍스트·표 중심으로만 구성한다.
# ---------------------------------------------------------------------------

_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_SEPARATOR_ROW_RE = re.compile(r"^[\s\-:|]+$")
_BOLD_SPLIT_RE = re.compile(r"\*\*")


def _split_bold(text: str) -> list:
    """"a **b** c" -> [("a ", False), ("b", True), (" c", False)].
    markdown의 굵게 표시(**)를 fpdf2가 이해하는 폰트 스타일 토글로 바꾸는
    자리. 별표 개수가 홀수로 어긋나도(닫는 ** 누락) 마지막 조각은 그냥
    보통 글자로 남기지, 에러를 내지 않는다 — 리포트 문장은 이미 이 모듈이
    스스로 만든 신뢰된 텍스트라 완벽한 마크다운 파서가 필요 없다."""
    parts = []
    for i, chunk in enumerate(_BOLD_SPLIT_RE.split(text)):
        if chunk:
            parts.append((chunk, i % 2 == 1))
    return parts or [("", False)]


def _pdf_write_rich(pdf: FPDF, text: str, size: float = 10, line_h: float = 6) -> None:
    """굵게(**)가 섞인 한 "문단"을 같은 줄에서 이어 쓴다. multi_cell이 아니라
    write()를 쓰는 이유: multi_cell은 호출마다 줄바꿈하지만, 이 함수는 굵은
    조각과 보통 조각을 같은 문단 안에서 자연스럽게 이어 붙여야 한다."""
    for chunk, bold in _split_bold(text):
        pdf.set_font("NotoSansKR", "B" if bold else "", size)
        pdf.write(line_h, chunk)
    pdf.ln(line_h)


def _pdf_render_table(pdf: FPDF, rows: list, size: float = 9) -> None:
    """표 한 덩어리(헤더 행 포함)를 그린다.

    직접 cell()로 고정 높이 칸을 그리던 첫 버전은 "계산 명세 요약"처럼 긴
    문장이 들어오면 줄바꿈을 못 해 페이지 밖으로 흘러넘쳤다(실측으로 확인한
    버그 — 3·4장 표에서 실제로 텍스트가 페이지 경계를 넘어갔다). fpdf2가
    이미 제공하는 Table API로 바꿔서, 셀마다 자동으로 줄바꿈하고 그 행에서
    가장 긴 셀에 맞춰 행 높이를 다 같이 맞추게 한다.
    """
    if not rows:
        return
    pdf.set_font("NotoSansKR", "", size)
    with pdf.table(rows, text_align="LEFT", first_row_as_headings=True, markdown=True):
        pass


def build_pdf(report_md: str) -> bytes:
    """report_md(build_report가 만든 최종 마크다운)를 PDF 바이트로 그린다.

    markdown 전체 문법을 다 지원하지 않는다 — report.py 자신과
    phrasing.py·manual_sections.py가 실제로 만들어내는 모양(#/##/### 제목,
    표, "- " 불릿, "> " 인용, **굵게**, "---" 구분선, 평문단)만 처리한다.
    이 문서를 쓰는 쪽이 이 모듈 자신이라 지원 범위를 스스로 알고 있다.
    """
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(18, 18, 18)
    pdf.add_font("NotoSansKR", "", str(FONT_REGULAR))
    pdf.add_font("NotoSansKR", "B", str(FONT_BOLD))
    pdf.add_page()

    lines = report_md.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if not stripped:
            pdf.ln(3)
            i += 1
            continue

        if stripped == "---":
            pdf.ln(2)
            pdf.set_draw_color(200, 200, 200)
            y = pdf.get_y()
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(4)
            i += 1
            continue

        if stripped.startswith("### "):
            pdf.set_font("NotoSansKR", "B", 12)
            pdf.multi_cell(0, 8, stripped[4:])
            pdf.ln(1)
            i += 1
            continue
        if stripped.startswith("## "):
            pdf.set_font("NotoSansKR", "B", 14)
            pdf.multi_cell(0, 9, stripped[3:])
            pdf.ln(1)
            i += 1
            continue
        if stripped.startswith("# "):
            pdf.set_font("NotoSansKR", "B", 18)
            pdf.multi_cell(0, 10, stripped[2:])
            pdf.ln(2)
            i += 1
            continue

        if stripped.startswith(">"):
            text = stripped.lstrip(">").strip()
            pdf.set_text_color(100, 100, 100)
            pdf.set_x(pdf.l_margin + 4)
            if text:
                _pdf_write_rich(pdf, text, size=10)
            else:
                pdf.ln(3)
            pdf.set_text_color(0, 0, 0)
            i += 1
            continue

        if _TABLE_ROW_RE.match(stripped):
            table_lines = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i].strip()):
                table_lines.append(lines[i].strip())
                i += 1
            rows = []
            for tl in table_lines:
                inner = tl.strip()[1:-1] if tl.strip().endswith("|") else tl.strip()[1:]
                if _SEPARATOR_ROW_RE.match(inner):
                    continue
                rows.append([c.strip() for c in inner.split("|")])
            _pdf_render_table(pdf, rows)
            pdf.ln(2)
            continue

        if stripped.startswith("- "):
            # 불릿 글자 자체의 폰트를 명시적으로 정하지 않으면 직전 줄(특히
            # 제목처럼 크고 굵은 폰트)의 크기를 그대로 물려받는다 — 실측으로
            # 확인함(제목 바로 다음 첫 불릿만 유난히 크게 나왔었다).
            pdf.set_x(pdf.l_margin + 4)
            pdf.set_font("NotoSansKR", "", 10)
            pdf.write(6, "• ")
            _pdf_write_rich(pdf, stripped[2:], size=10)
            i += 1
            continue

        # 평문단
        pdf.set_x(pdf.l_margin)
        _pdf_write_rich(pdf, stripped, size=10)
        i += 1

    return bytes(pdf.output())


def _inline_html(text: str) -> str:
    """**굵게**만 처리해서 안전한 HTML로 바꾼다. build_pdf가 쓰는 _split_bold와
    같은 파싱을 재사용한다 — PDF와 화면 미리보기가 같은 마크다운을 서로 다르게
    읽으면 두 출력이 다른 리포트처럼 보일 수 있다."""
    return "".join(
        f"<strong>{html_lib.escape(chunk)}</strong>" if bold else html_lib.escape(chunk)
        for chunk, bold in _split_bold(text)
    )


_REPORT_HTML_STYLE = f"""<style>
  body {{
    margin: 0; padding: 20px 28px 32px;
    font-family: 'Malgun Gothic', '맑은 고딕', sans-serif;
    color: #0f172a; line-height: 1.65; background: #ffffff;
  }}
  h1 {{ font-size: 21px; margin: 0 0 14px; }}
  h2 {{ font-size: 16px; margin: 26px 0 10px; border-bottom: 2px solid #0f172a; padding-bottom: 6px; }}
  h3 {{ font-size: 13.5px; margin: 16px 0 6px; color: #1e293b; }}
  p {{ margin: 5px 0; font-size: 13px; }}
  ul {{ margin: 6px 0; padding-left: 20px; }}
  li {{ margin: 3px 0; font-size: 13px; }}
  blockquote {{
    margin: 8px 0; padding: 8px 14px; border-left: 3px solid {COLOR_SLATE};
    color: {COLOR_SLATE}; background: #f8fafc; font-size: 12.5px;
  }}
  hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 18px 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 12.5px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 6px 10px; text-align: left; }}
  th {{ background: #f8fafc; font-weight: 700; }}
</style>"""


def build_report_html(report_md: str) -> str:
    """report_md를 화면 미리보기용 HTML로 그린다.

    st.markdown(report_md)를 그대로 쓰지 않는 이유: 리포트는 8장 분량의
    긴 문서라, Streamlit 자체 마크다운 렌더러에 그대로 맡기면 페이지의 다른
    CSS와 섞여 보일 수 있다. 7단계 이메일 미리보기가 이미 같은 문제를
    st.iframe(독립된 프레임)으로 풀어놨다 — 그 형태를 6단계에도 그대로
    가져온다.

    build_pdf와 같은 줄 단위 파싱(#/##/###, >, |표|, "- " 불릿, "---",
    평문단)을 그대로 따른다 — 두 렌더러가 같은 마크다운을 다르게 읽으면
    PDF와 화면 미리보기가 서로 다른 리포트처럼 보일 수 있다.
    """
    lines = report_md.splitlines()
    body = []
    i, n = 0, len(lines)

    while i < n:
        stripped = lines[i].strip()

        if not stripped:
            i += 1
            continue

        if stripped == "---":
            body.append("<hr>")
            i += 1
            continue

        if stripped.startswith("### "):
            body.append(f"<h3>{_inline_html(stripped[4:])}</h3>")
            i += 1
            continue
        if stripped.startswith("## "):
            body.append(f"<h2>{_inline_html(stripped[3:])}</h2>")
            i += 1
            continue
        if stripped.startswith("# "):
            body.append(f"<h1>{_inline_html(stripped[2:])}</h1>")
            i += 1
            continue

        if stripped.startswith(">"):
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                text = lines[i].strip().lstrip(">").strip()
                if text:
                    quote_lines.append(text)
                i += 1
            body.append("<blockquote>" + "<br>".join(_inline_html(t) for t in quote_lines) + "</blockquote>")
            continue

        if _TABLE_ROW_RE.match(stripped):
            table_lines = []
            while i < n and _TABLE_ROW_RE.match(lines[i].strip()):
                table_lines.append(lines[i].strip())
                i += 1
            rows = []
            for tl in table_lines:
                inner = tl[1:-1] if tl.endswith("|") else tl[1:]
                if _SEPARATOR_ROW_RE.match(inner):
                    continue
                rows.append([c.strip() for c in inner.split("|")])
            if rows:
                head, *data_rows = rows
                thead = "".join(f"<th>{_inline_html(c)}</th>" for c in head)
                tbody = "".join(
                    "<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in r) + "</tr>"
                    for r in data_rows
                )
                body.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            continue

        if stripped.startswith("- "):
            items = []
            while i < n and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:])
                i += 1
            body.append("<ul>" + "".join(f"<li>{_inline_html(t)}</li>" for t in items) + "</ul>")
            continue

        # 평문단 — 빈 줄이나 다른 구조(제목·인용·불릿·표·구분선)가 나오기 전까지
        # 한 문단으로 묶는다.
        para = [stripped]
        i += 1
        while i < n and lines[i].strip() and not lines[i].strip().startswith(("#", ">", "- ", "|")) \
                and lines[i].strip() != "---":
            para.append(lines[i].strip())
            i += 1
        body.append(f"<p>{_inline_html(' '.join(para))}</p>")

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{_REPORT_HTML_STYLE}</head><body>{''.join(body)}</body></html>"
    )


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def build_report(run_context: dict) -> dict:
    """8장을 순서대로 합쳐 마크다운을 만들고, manual/sections.md의 사람 작성분을
    병합한 뒤, 병합까지 끝난 최종 문서 전체를 phrasing.check_forbidden으로
    자체 검사한다.

    반환값이 문자열이 아니라 dict인 이유: 화면에 "몇 개 장이 채워졌는지,
    몇 개가 아직 비어 있는지, 기간 불일치·오래됨 경고가 있는지"를 보여줘야
    하는데, 이미 merge_into_report가 판단해 놓은 결과를 문자열에서 다시
    파싱해내게 하면 안 된다(pipeline/manual_sections.py의 merge_into_report와
    같은 이유).

    check_forbidden을 병합 *이후*에 돌리는 이유: 사람이 5장에 "때문"·"필요하다"
    같은 표현을 실제로 쓸 수 있다. 병합 전 골격만 검사하면 사람이 쓴 문장은
    검사망을 피해간다 — 검사는 항상 최종적으로 나갈 문서를 대상으로 해야 한다.

    사람이 쓰는 장(2·5·6)의 안내문·힌트까지 포함해 문서 전체를 검사하는 것도
    같은 이유다(다만 6장 제목처럼 "제안"이라는 낱말이 장 이름 자체에 들어 있는
    경우도 그대로 걸린다 — 이 검사기는 문맥을 모르는 낱말 검색기라는 뜻이고,
    그 한계도 그대로 드러나는 게 맞다).
    """
    parts = [
        _build_header(run_context),
        section_1_summary(run_context),
        section_2_background(run_context),
        section_3_data_methodology(run_context),
        section_4_status(run_context),
        section_5_causes(run_context),
        section_6_recommendations(run_context),
        section_7_limitations(run_context),
        section_8_appendix(run_context),
    ]
    report_md = "\n\n".join(parts)

    run_log = run_context.get("run_log") or {}
    period = _period_label(run_log)
    manual_dict = manual_sections.load_manual(period)
    merged_md, substituted, remaining = manual_sections.merge_into_report(report_md, manual_dict)

    findings = phrasing.check_forbidden(merged_md)
    if findings:
        merged_md += "\n\n" + _dev_forbidden_notice(findings)

    meta = manual_dict.get("_meta") or {}

    return {
        "report_md": merged_md,
        "치환된_장": substituted,
        "미작성_장": remaining,
        "경고": meta.get("경고", []),
        "금지표현_검사": findings,
    }
