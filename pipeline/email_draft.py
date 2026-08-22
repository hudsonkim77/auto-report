"""발송 준비된 이메일 초안을 만드는 모듈 — CLAUDE.md 5-3, 9절: 이 앱이 하는
일은 초안 생성과 발송 확정까지다. 실제 SMTP 발송은 8주차 범위이고, 이 모듈은
SMTP를 전혀 모른다. 수신자도 config.EMAIL_TO(예시값)만 쓴다 — 실제 주소를
여기 코드에 적지 않는다.

리포트 전문을 본문에 넣지 않는 이유: 리포트는 8장 전체(3~4천 단어 규모)라
이메일 본문에 그대로 넣으면 읽는 사람이 훑어볼 핵심을 못 찾는다. 본문은
"리포트를 열어봐야 하는지 판단할 수 있는 요약"까지만 하고, 전문은 첨부(지금은
목록만)로 돌린다.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from common import status_badge, COLOR_SLATE  # noqa: E402
from pipeline import phrasing  # noqa: E402
from pipeline import report as reporter  # noqa: E402
from pipeline import manual_sections  # noqa: E402

# 이메일 폭·폰트 — 개념 3절: 웹폰트 없이 시스템 폰트만, 표준 이메일 폭(600px).
EMAIL_WIDTH = 600
EMAIL_FONT = "'Malgun Gothic', '맑은 고딕', sans-serif"

# 실제 첨부(파일을 이메일에 붙이는 것)는 8주차 범위다. 지금은 파일명·경로·
# 크기만 목록으로 알려준다 — 차트 이미지·해석/제안 문장은 첨부 후보에서
# 아예 뺀다(차트는 이메일 클라이언트에서 이미지가 차단되는 경우가 많고
# kaleido도 이 환경에서 불안정하다; 해석·제안은 리포트 5·6장에 있고 아직
# 사람이 안 썼을 수 있어 이메일 첨부 "목록"에 그 자체를 넣을 대상이 아니다).
ATTACHMENT_FILENAMES = ["report.md", "report.pdf", "metrics.csv", "comparison.csv"]

_CHAPTER7_RE = re.compile(r"^## 7\.[^\n]*\n(.*?)(?=^## 8\.|\Z)", re.MULTILINE | re.DOTALL)
_SUBSECTION_RE = re.compile(r"^### (7-\d+\..*)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# 재료 추출 — html/text 렌더러가 공유한다
# ---------------------------------------------------------------------------

def _period_label(run_log: dict) -> str:
    기간 = run_log.get("기간", "")
    return 기간.split("~")[0].strip() if 기간 else "?"


def _recipients() -> list:
    return [addr.strip() for addr in config.EMAIL_TO.split(",") if addr.strip()]


def _build_subject(period: str, validation: dict, report_md: str) -> str:
    """검증 경고는 validation["경고수"]로 판단하고, 미작성 장은 report_md에
    사람 자리표시자(manual_sections.PLACEHOLDER_LINE)가 아직 남아 있는지로
    판단한다 — 둘 다 이미 있는 판정을 다시 계산하지 않고 그대로 읽는다."""
    subject = f"{config.EMAIL_SUBJECT_PREFIX} CS 지표 리포트 {period}"
    if (validation or {}).get("경고수", 0) > 0:
        subject += " (확인 필요)"
    if manual_sections.PLACEHOLDER_LINE in report_md:
        subject += " (초안)"
    return subject


def _core_metrics_rows(comparison_df, limit: int = 6) -> list:
    """"핵심 지표"를 이 모듈이 새로 판단하지 않는다 — app.py의 KPI_METRICS
    처럼 화면 전용으로 큐레이션된 목록을 pipeline 모듈이 끌어오면 지표별
    하드코딩이 두 곳에 생긴다(CLAUDE.md 9절과 같은 이유). 대신 이번 실행이
    실제로 계산·비교한 지표를 앞에서부터 최대 6개 그대로 쓴다 — 이 순서는
    profile.py가 판정한 순서를 그대로 물려받은 것이라 임의가 아니다."""
    if comparison_df is None or len(comparison_df) == 0:
        return []

    rows = []
    for _, row in comparison_df.head(limit).iterrows():
        지표명 = row["지표명"]
        유형 = row["유형"]
        if row["비교상태"] == "비교 불가":
            rows.append({"지표명": 지표명, "당월": "—", "전월": "—", "변화율": "비교 불가"})
            continue

        당월 = phrasing.fmt_value(row["당월"], 유형, 지표명)
        전월 = phrasing.fmt_value(row["전월"], 유형, 지표명)
        율 = row["상대변화율"]
        변화율 = f"{율:+.2f}%" if (율 is not None and not (isinstance(율, float) and 율 != 율)) else "—"
        rows.append({"지표명": 지표명, "당월": 당월, "전월": 전월, "변화율": 변화율})
    return rows


def _exceeded_sentences(comparison_df, validation: dict) -> list:
    """"전월 대비 변동이 큰 지표"의 기준을 report.py 5장이 이미 쓰는 것과
    똑같이 맞춘다 — validate.py가 "경고"로 판정한 지표만, 같은 임계값으로
    문장을 만든다. 이메일과 리포트가 "무엇이 크게 변했다"를 다르게 말하면
    같은 실행을 두고 서로 다른 결론을 준 것처럼 보인다."""
    exceeded_ids = set(reporter._exceeded_metric_ids(validation))
    if not exceeded_ids or comparison_df is None:
        return []

    thresholds = reporter._threshold_map(validation)
    sentences = []
    for _, row in comparison_df.iterrows():
        if row["metric_id"] not in exceeded_ids:
            continue
        threshold = thresholds.get(row["metric_id"], config.MOM_THRESHOLD)
        sentences.append(phrasing.describe_change(row, threshold))
    return sentences


def _limitation_titles(report_md: str) -> list:
    """리포트 7장 본문에서 "### 7-N. 제목" 소절 제목만 뽑는다. 내용은 옮기지
    않는다 — 이메일은 "한계가 있다"는 사실과 어떤 갈래인지만 알리고, 자세한
    내용은 첨부(리포트 본문)를 열어야 보이게 한다."""
    m = _CHAPTER7_RE.search(report_md)
    if not m:
        return []
    return [t.strip() for t in _SUBSECTION_RE.findall(m.group(1))]


def _attachment_list(run_dir: Path) -> list:
    """실제로 파일을 첨부하지 않고 메타데이터만 담는다(8주차 전까지). 파일이
    아직 없으면(예: report.pdf — PDF 생성은 아직 구현 전) size를 0이나
    추측값으로 채우지 않고 None으로 남긴다 — "파일이 없다"와 "크기가 0이다"는
    다른 사실이다(CLAUDE.md 9절과 같은 원칙)."""
    attachments = []
    for filename in ATTACHMENT_FILENAMES:
        path = run_dir / filename
        if path.exists():
            attachments.append({"filename": filename, "path": str(path), "size": path.stat().st_size})
        else:
            attachments.append({"filename": filename, "path": str(path), "size": None})
    return attachments


def _gather(run_context: dict, report_md: str) -> dict:
    run_log = run_context.get("run_log") or {}
    validation = run_context.get("validation") or {}
    comparison_df = run_context.get("comparison")
    metrics_catalog = run_context.get("metrics_catalog") or {}
    run_dir = Path(run_context.get("run_dir") or ".")

    return {
        "기간": _period_label(run_log),
        "생성일시": dt.datetime.now().isoformat(),
        "핵심지표": _core_metrics_rows(comparison_df),
        "변동사항": _exceeded_sentences(comparison_df, validation),
        "전체판정": validation.get("전체판정", "—"),
        "차단수": validation.get("차단수", 0),
        "경고수": validation.get("경고수", 0),
        "한계소절": _limitation_titles(report_md),
        "첨부": _attachment_list(run_dir),
        "카탈로그_생성일시": metrics_catalog.get("_meta", {}).get("생성일시", "알 수 없음"),
    }


def _attachment_note(a: dict) -> str:
    if a["size"] is not None:
        return f"{a['filename']} ({a['size']:,}바이트) — 실제 첨부는 8주차에 구현됩니다"
    return f"{a['filename']} — 파일 없음(아직 생성되지 않음)"


# ---------------------------------------------------------------------------
# 렌더러 — 같은 data를 text/html로 각각 그린다
# ---------------------------------------------------------------------------

def _render_text(data: dict) -> str:
    lines = [
        f"대상 기간: {data['기간']}",
        f"생성일시: {data['생성일시']}",
        "",
        "[핵심 지표]",
    ]
    if data["핵심지표"]:
        for row in data["핵심지표"]:
            lines.append(f"- {row['지표명']}: 당월 {row['당월']} / 전월 {row['전월']} / 변화율 {row['변화율']}")
    else:
        lines.append("- 계산된 지표가 없습니다.")

    lines += ["", "[전월 대비 변동이 큰 지표]"]
    if data["변동사항"]:
        lines += [f"- {s}" for s in data["변동사항"]]
    else:
        lines.append("- 임계값을 초과한 지표가 없습니다.")

    lines += [
        "",
        "[검증 요약]",
        f"- 차단 {data['차단수']}건, 경고 {data['경고수']}건",
        "",
        "[한계 요약]",
    ]
    if data["한계소절"]:
        lines += [f"- {t}" for t in data["한계소절"]]
    else:
        lines.append("- 이번 실행에서 남긴 한계 항목이 없습니다.")

    lines += ["", "[첨부 안내]"]
    lines += [f"- {_attachment_note(a)}" for a in data["첨부"]]

    lines += [
        "",
        "생성 도구: auto-report",
        f"카탈로그 버전(생성일시): {data['카탈로그_생성일시']}",
    ]
    return "\n".join(lines)


def _cell(html_inner: str, **style) -> str:
    """<td style="...">내용</td> 한 칸. 이메일 HTML은 <style> 태그도 클래스도
    믿을 수 없어(클라이언트가 통째로 지우는 경우가 있다, 개념 3절) 칸마다
    인라인 style을 직접 쓴다."""
    base = "font-family:" + EMAIL_FONT + ";"
    style_str = base + "".join(f"{k.replace('_', '-')}:{v};" for k, v in style.items())
    return f'<td style="{style_str}">{html_inner}</td>'


def _row(inner: str) -> str:
    return f"<tr>{inner}</tr>"


def _section_title(text: str) -> str:
    return _row(_cell(
        html.escape(text),
        font_size="15px", font_weight="700", color="#0f172a",
        padding="20px 24px 8px 24px",
    ))


def _html_list(items: list, empty_text: str) -> str:
    """목록 하나를 <table> 행으로 그린다 — <ul>도 이메일 클라이언트에 따라
    들여쓰기가 깨지는 경우가 있어, 표 구조 하나로 통일한다(개념 3절: 레이아웃은
    table로)."""
    if not items:
        rows = _row(_cell(
            status_badge(empty_text, "데이터 없음"),
            padding="4px 24px 16px 24px",
        ))
        return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'

    rows = "".join(
        _row(_cell(
            f"&bull;&nbsp; {html.escape(item)}",
            font_size="13px", color="#334155", padding="4px 24px",
            line_height="1.5",
        ))
        for item in items
    )
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}<tr><td style="padding-bottom:12px;"></td></tr></table>'


def _render_html(data: dict) -> str:
    e = html.escape
    td = _cell

    # --- 상단 헤더: 제목 + 대상 기간, 배경색 있는 헤더 ---
    header = _row(td(
        f"CS 지표 리포트<br>"
        f'<span style="font-size:13px;font-weight:400;">대상 기간 {e(data["기간"])} · 생성 {e(data["생성일시"])}</span>',
        background_color="#0f172a", color="#ffffff",
        font_size="18px", font_weight="700",
        padding="20px 24px",
    ))

    # --- 검증 요약 배지: 화면과 같은 상태 색(common.status_badge 그대로 재사용) ---
    검증배지 = _row(td(
        status_badge(f"검증 결과: {data['전체판정']}", data["전체판정"])
        + "&nbsp;&nbsp;"
        + status_badge(f"차단 {data['차단수']}건", "차단" if data["차단수"] else "통과")
        + "&nbsp;&nbsp;"
        + status_badge(f"경고 {data['경고수']}건", "경고" if data["경고수"] else "통과"),
        padding="16px 24px 4px 24px",
    ))

    # --- 핵심 지표: 테두리 있는 table ---
    if data["핵심지표"]:
        metric_header = _row(
            "".join(
                td(f"<strong>{e(h)}</strong>", border="1px solid #e2e8f0",
                   background_color="#f8fafc", font_size="12px", padding="8px 10px",
                   text_align="left" if h == "지표명" else "right")
                for h in ("지표명", "당월", "전월", "변화율")
            )
        )
        metric_rows = "".join(
            _row(
                td(e(row["지표명"]), border="1px solid #e2e8f0", font_size="13px", padding="8px 10px")
                + td(e(row["당월"]), border="1px solid #e2e8f0", font_size="13px", padding="8px 10px", text_align="right")
                + td(e(row["전월"]), border="1px solid #e2e8f0", font_size="13px", padding="8px 10px", text_align="right")
                + td(e(row["변화율"]), border="1px solid #e2e8f0", font_size="13px", padding="8px 10px", text_align="right")
            )
            for row in data["핵심지표"]
        )
        metrics_table = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'border="0" style="border-collapse:collapse;font-family:{EMAIL_FONT};">'
            f"{metric_header}{metric_rows}</table>"
        )
    else:
        metrics_table = status_badge("계산된 지표가 없습니다", "데이터 없음")
    핵심지표_블록 = _row(td(metrics_table, padding="8px 24px 4px 24px"))

    # --- 변동 큰 지표 / 한계 요약 / 첨부 목록: 표 기반 목록 ---
    변동_블록 = _row(td(
        _html_list(data["변동사항"], "임계값을 초과한 지표가 없습니다"),
        padding="0 12px",
    ))
    한계_블록 = _row(td(
        _html_list(data["한계소절"], "이번 실행에서 남긴 한계 항목이 없습니다"),
        padding="0 12px",
    ))
    첨부_블록 = _row(td(
        _html_list([_attachment_note(a) for a in data["첨부"]], "첨부 대상 파일이 없습니다"),
        padding="0 12px",
    ))

    # --- 하단 푸터: 작은 글씨, slate ---
    footer = _row(td(
        f"생성 도구: auto-report &middot; 카탈로그 버전(생성일시): {e(data['카탈로그_생성일시'])}",
        color=COLOR_SLATE, font_size="11px", padding="16px 24px 20px 24px",
        border_top="1px solid #e2e8f0",
    ))

    body = (
        header
        + 검증배지
        + _section_title("핵심 지표")
        + 핵심지표_블록
        + _section_title("전월 대비 변동이 큰 지표")
        + 변동_블록
        + _section_title("한계 요약")
        + 한계_블록
        + _section_title("첨부 안내")
        + 첨부_블록
        + footer
    )

    # 최대 폭 600px: 바깥 table로 감싸고 안쪽 table에 width를 고정한다
    # (개념 3절 — flexbox/grid 대신 table, max-width는 이메일 표준 폭).
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background-color:#f1f5f9;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        f'<table role="presentation" width="{EMAIL_WIDTH}" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:{EMAIL_WIDTH}px;width:100%;background-color:#ffffff;'
        f'font-family:{EMAIL_FONT};">'
        f"{body}"
        "</table>"
        "</td></tr>"
        "</table>"
    )


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def build_email(run_context: dict, report_md: str) -> dict:
    """발송 준비된 이메일 초안을 만든다. 실제로 보내지 않는다 — SMTP 코드는
    이 파일 어디에도 없다.

    run_context는 report.py의 build_report()와 같은 모양(run_log·validation·
    comparison·metrics_catalog)을 쓰되, 첨부 파일 경로를 만들려면 "run_dir"도
    필요하다 — app.py가 run_context를 만들 때 이 키를 같이 넣어줘야 한다.
    """
    run_log = run_context.get("run_log") or {}
    validation = run_context.get("validation") or {}

    period = _period_label(run_log)
    data = _gather(run_context, report_md)
    subject = _build_subject(period, validation, report_md)

    return {
        "subject": subject,
        "to": _recipients(),
        "from": config.EMAIL_FROM,
        "body_html": _render_html(data),
        "body_text": _render_text(data),
        "attachments": data["첨부"],
    }
