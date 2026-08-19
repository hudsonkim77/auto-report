"""공통 색상 상수·유틸. CLAUDE.md 7절(상태 색 대응)·DESIGN.md를 따른다.

이 대응은 화면·리포트·이메일 어디서나 같아야 하므로 여기 한 곳에서만 정의한다.
"""

import re

COLOR_EMERALD = "#10b981"  # 통과 / OK / 완료
COLOR_AMBER = "#f59e0b"    # 경고 / 표본부족 / 진행중
COLOR_ROSE = "#f43f5e"     # 차단 / 검증 실패
COLOR_SLATE = "#64748b"    # 데이터 없음 / 유효구간 밖 / 대기

# 차트 계열선 전용 색상(Tailwind 500, DESIGN.md 1절). 위 4색은 "상태"라는 뜻을
# 이미 갖고 있어서 그대로 차트 선에 쓰면 "이 선이 경고다/통과다"처럼 잘못 읽힐
# 수 있다. 그래서 차트 선은 의미가 없는 별도 색(blue/violet)만 쓴다.
COLOR_SERIES_A = "#3b82f6"  # blue 500 — 각 차트의 첫 번째 계열
COLOR_SERIES_B = "#8b5cf6"  # violet 500 — 각 차트의 두 번째 계열

# 상태 문구 -> 색상. 8단계 진행 상태(완료/진행중/대기)와
# 검증 상태(통과/경고/표본부족/차단/데이터없음)를 한 사전에서 같이 관리한다.
STATUS_COLORS = {
    "완료": COLOR_EMERALD,
    "통과": COLOR_EMERALD,
    "OK": COLOR_EMERALD,
    "계산가능": COLOR_EMERALD,
    "증가": COLOR_EMERALD,
    "감소": COLOR_ROSE,
    "변동없음": COLOR_SLATE,
    "진행중": COLOR_AMBER,
    "준비됨": COLOR_AMBER,
    "경고": COLOR_AMBER,
    "표본부족": COLOR_AMBER,
    "유효구간 확장 필요": COLOR_AMBER,
    "구간확장": COLOR_AMBER,
    "계산오류": COLOR_ROSE,
    "차단": COLOR_ROSE,
    "검증 실패": COLOR_ROSE,
    "판정불가": COLOR_ROSE,
    "계산불가": COLOR_ROSE,
    "대기": COLOR_SLATE,
    "데이터 없음": COLOR_SLATE,
    "유효구간 밖": COLOR_SLATE,
    "이 파일과 무관": COLOR_SLATE,
    "정보": COLOR_SLATE,
}


_WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9_]+)\]\]")


def _display_kind(metric_id: str, metrics_catalog: dict) -> str:
    """지표 하나를 "금액" / "비율" / "카운트" 중 어디로 표시할지 판별한다.

    기초형(카운트형/금액형)·비율형은 유형 필드만 보면 되지만, 파생형은 유형
    하나로 못 정한다 — arpu(파생형)는 원 단위 금액이고 monthly_churn_rate(파생형)는
    비율이다. 분자가 참조하는 기초 지표의 유형까지 한 단계 더 봐서 정한다.
    """
    metric = metrics_catalog.get(metric_id, {})
    유형 = metric.get("유형", "")
    calc = metric.get("계산") or {}

    if 유형.startswith("금액형"):
        return "금액"
    if 유형.startswith("카운트형"):
        return "카운트"
    if 유형.startswith("비율형"):
        return "비율"

    if "시차" in calc:  # 변화율형은 항상 비율(%)
        return "비율"

    if 유형.startswith("파생형"):
        num_text = calc.get("분자", "")
        m = _WIKILINK_RE.search(str(num_text))
        if m:
            num_kind = _display_kind(m.group(1), metrics_catalog)
            if num_kind in ("금액", "카운트"):
                # 분자가 금액이면 결과도 금액(예: arpu), 분자가 카운트면 결과는
                # 카운트/카운트 = 비율(예: monthly_churn_rate).
                return "금액" if num_kind == "금액" else "비율"
        return "비율"

    return "카운트"


def format_metric_value(metric_id: str, value, metrics_catalog: dict) -> str:
    """계산된 값을 화면 표시용 문자열로 바꾼다.

    금액 = 천단위 콤마 + "원", 비율 = 소수 1자리 + "%", 카운트 = 콤마 + "명"/"건".
    "명"과 "건" 중에는 지표명에 고객·사용자 같은 사람 단위 낱말이 있으면 "명"을,
    아니면 "건"을 쓴다 — 완벽하진 않지만 이 위키의 지표 이름 관례에서는 이 정도
    휴리스틱으로 전부 맞는다.
    """
    if value is None:
        return "—"

    metric = metrics_catalog.get(metric_id, {})

    # 표시단위가 명시적으로 선언된 지표는 유형 기반 추론보다 우선한다. avg_data_usage
    # 처럼 계산 파이프라인 제약 때문에 유형이 "금액형"이라고 적혀 있어도 실제 단위가
    # GB인 경우가 있다 — 그걸 코드에서 metric_id로 특별 취급하지 않고, 위키 정의서에
    # 명시적 필드(표시단위)를 두는 쪽으로 해결했다.
    표시단위 = metric.get("표시단위")
    if 표시단위 and 표시단위 not in ("원", "%"):
        return f"{value:,.1f}{표시단위}"

    kind = _display_kind(metric_id, metrics_catalog)
    지표명 = metric.get("지표명", metric_id)

    if kind == "금액":
        return f"{value:,.0f}원"
    if kind == "비율":
        return f"{value * 100:.1f}%"
    # 카운트
    unit = "명" if any(k in 지표명 for k in ("고객", "사용자")) else "건"
    return f"{value:,.0f}{unit}"


def format_metric_value_compact(metric_id: str, value, metrics_catalog: dict) -> str:
    """대시보드 카드처럼 공간이 좁은 곳에 쓰는 축약형. 백만원 이상 금액만
    "27.8백만원"으로 줄이고, 나머지(비율·카운트·GB 등)는 format_metric_value와
    같다 — 큰 금액만 자릿수가 많아서 카드 폭을 넘기기 때문에 이것만 축약한다."""
    if value is None:
        return "—"

    metric = metrics_catalog.get(metric_id, {})
    표시단위 = metric.get("표시단위")
    if 표시단위 and 표시단위 not in ("원", "%"):
        return f"{value:,.1f}{표시단위}"

    kind = _display_kind(metric_id, metrics_catalog)
    if kind == "금액":
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:,.1f}백만원"
        return f"{value:,.0f}원"

    return format_metric_value(metric_id, value, metrics_catalog)


def format_delta_value(metric_id: str, delta, metrics_catalog: dict):
    """전월 대비 변화량을 st.metric의 delta 인자로 쓸 문자열로 만든다.
    부호를 명시적으로 붙인다 — st.metric은 문자열 delta의 부호로 화살표 방향과
    색(delta_color="normal" 기준 증가=초록/감소=빨강)을 정하므로, 부호가 없으면
    항상 증가로만 표시된다."""
    if delta is None:
        return None
    sign = "+" if delta > 0 else ("-" if delta < 0 else "")
    formatted = format_metric_value(metric_id, abs(delta), metrics_catalog)
    return f"{sign}{formatted}" if sign else formatted


def status_badge(label: str, status: str) -> str:
    """상태 배지 HTML을 반환한다. st.markdown(..., unsafe_allow_html=True)로 렌더링한다.

    status는 STATUS_COLORS에 정의된 문구여야 한다. 없는 문구가 들어오면
    slate(데이터 없음/대기와 같은 색)로 표시해 "판단 근거 없음"에 가깝게 취급한다.
    """
    color = STATUS_COLORS.get(status, COLOR_SLATE)
    return (
        f'<span style="'
        f'display:inline-block;'
        f'background-color:{color}22;'
        f'color:{color};'
        f'border:1px solid {color};'
        f'border-radius:999px;'
        f'padding:2px 12px;'
        f'font-size:0.85em;'
        f'font-weight:600;'
        f'margin:2px 0;'
        f'">{label}</span>'
    )
