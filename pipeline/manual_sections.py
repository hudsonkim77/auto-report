"""사람이 쓰는 장(2·5·6장)을 관리하는 모듈.

report.py가 만드는 문서는 2·5·6장을 "> 이 장은 사람이 작성합니다." 자리표시자로
비워둔다(CLAUDE.md 1절: 판단이 필요한 문장은 사람이 쓴다). 이 모듈은 그 자리에
실제로 사람이 쓴 글(manual/sections.md)을 찾아서 자리표시자를 대체하는 접합부다.

이 모듈이 절대 하지 않는 것: manual/sections.md의 내용을 요약·재구성·검사하지
않는다. 사람이 쓴 문장은 그대로 옮긴다 — 이 모듈이 문장을 고치기 시작하면
"사람이 썼다"는 전제 자체가 흔들린다.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import yaml

MANUAL_DIR = Path(__file__).resolve().parent.parent / "manual"
MANUAL_PATH = MANUAL_DIR / "sections.md"

PLACEHOLDER_LINE = "> 이 장은 사람이 작성합니다."
CHAPTER_HEADING_RE = re.compile(r"^## (\d+)\.[^\n]*\n", re.MULTILINE)
STALE_DAYS = 60

TEMPLATE = """---
작성일: {작성일}
대상기간: {대상기간}
작성자: (이름)
---

## 2. 배경·목적

<!-- 힌트: 이 리포트를 왜 만드는가, 누가 읽는가, 어떤 결정에 쓰이는가 -->
(내용)

## 5. 원인 분석

<!-- 힌트: 아래 "참고 — 위키에서 찾은 관련 분석"을 근거로 원인을 판정 -->
(내용)

## 6. 개선 제안

<!-- 힌트: 우선순위와 근거. 실행 가능성·비용을 함께 -->
(내용)
"""


def _write_template(period: str) -> None:
    MANUAL_DIR.mkdir(parents=True, exist_ok=True)
    content = TEMPLATE.format(작성일=dt.date.today().isoformat(), 대상기간=period)
    MANUAL_PATH.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# app.py 편집 화면용 — 원문 그대로 읽기/쓰기
# ---------------------------------------------------------------------------

def read_raw(period: str) -> str:
    """편집용 text_area에 보여줄 원문 그대로를 반환한다. 파일이 없으면
    load_manual과 같은 규칙으로 템플릿을 만든 뒤 그 내용을 돌려준다 — 편집
    화면을 열었을 때와 리포트를 생성할 때 "파일이 없으면 어떻게 하는가"가
    서로 다르면 안 된다."""
    if not MANUAL_PATH.exists():
        _write_template(period)
    return MANUAL_PATH.read_text(encoding="utf-8")


def save_raw(text: str, period: str) -> None:
    """text_area에서 편집한 전체 텍스트(프론트매터+본문)를 저장한다.

    프론트매터의 작성일·대상기간을 항상 지금 값으로 강제로 덮어쓰는 이유:
    사용자가 본문만 고치고 이 두 필드는 그대로 두면, load_manual의 신선도
    판정(대상기간 불일치·60일 경과 경고)이 "방금 저장했다"는 사실과 어긋난
    상태로 남는다. 작성자는 사람이 직접 채우는 값이라 손대지 않는다.
    """
    fm, body = _parse_frontmatter_and_body(text)
    if not isinstance(fm, dict):
        fm = {}

    fm["작성일"] = dt.date.today().isoformat()
    fm["대상기간"] = period
    fm.setdefault("작성자", "(이름)")

    frontmatter_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
    content = f"---\n{frontmatter_text}\n---\n{body}"

    MANUAL_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_PATH.write_text(content, encoding="utf-8")


def _parse_frontmatter_and_body(text: str):
    """export_catalog.py의 load_frontmatter_and_body와 같은 모양이지만, 이
    모듈은 06_metrics 정의서가 아니라 manual/sections.md 하나만 다루므로
    catalog 모듈을 끌어오지 않고 여기 따로 둔다 — 서로 다른 문서 종류를
    같은 파서로 묶으면 나중에 한쪽만 바뀌어도 같이 흔들린다."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, text
    return fm, parts[2]


def _parse_chapters(body: str) -> dict:
    """"## N. 제목" 헤딩으로 나뉜 절의 본문을 {"N": 내용} 형태로 뽑는다.
    "(내용)"만 있는(사람이 아직 안 쓴) 절은 빈 것으로 취급해 아예 키에 넣지
    않는다 — merge_into_report가 "내용이 없는 장"과 "빈 문자열이 있는 장"을
    구분할 필요 없이 in 연산 하나로 판단하게 하려는 목적이다."""
    matches = list(CHAPTER_HEADING_RE.finditer(body))
    chapters = {}
    for i, m in enumerate(matches):
        chapter_num = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        # 힌트 주석(<!-- ... -->)은 사람에게 보여줄 안내문이라 실제 리포트에
        # 섞여 들어가면 안 된다 — 항상 제거한다.
        content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL).strip()
        if content and content != "(내용)":
            chapters[chapter_num] = content
    return chapters


def load_manual(period: str) -> dict:
    """manual/sections.md를 읽어 {"장번호": 내용} dict로 반환한다.

    파일이 없으면 템플릿을 만들고 빈 dict({})를 반환한다 — 지금 막 만든
    템플릿에는 사람이 쓴 내용이 없으므로 "내용이 있다"고 보고하면 안 된다.

    경고가 있으면(대상기간 불일치·작성일 오래됨) 반환 dict에 "_meta" 키로
    같이 담는다. 경고가 없으면 "_meta"를 넣지 않는다 — 매번 빈 경고 리스트를
    넣으면 호출자가 항상 "_meta" 존재 여부부터 확인해야 해서, 없을 때는
    아예 키 자체를 생략하는 쪽을 택했다.
    """
    if not MANUAL_PATH.exists():
        _write_template(period)
        return {}

    text = MANUAL_PATH.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter_and_body(text)
    chapters = _parse_chapters(body)

    경고 = []

    대상기간 = fm.get("대상기간")
    if 대상기간 is not None and str(대상기간) != str(period):
        경고.append(f"이전 기간({대상기간}) 내용입니다")

    작성일 = fm.get("작성일")
    if 작성일 is not None:
        if isinstance(작성일, (dt.date, dt.datetime)):
            작성일_date = 작성일 if isinstance(작성일, dt.date) and not isinstance(작성일, dt.datetime) else 작성일.date()
        else:
            try:
                작성일_date = dt.date.fromisoformat(str(작성일))
            except ValueError:
                작성일_date = None
        if 작성일_date is not None:
            지난일수 = (dt.date.today() - 작성일_date).days
            if 지난일수 >= STALE_DAYS:
                경고.append(f"작성일({작성일_date.isoformat()})로부터 {지난일수}일 지났습니다(오래됨 경고, 기준 {STALE_DAYS}일)")

    if 경고:
        chapters["_meta"] = {"대상기간": 대상기간, "작성일": 작성일, "작성자": fm.get("작성자"), "경고": 경고}

    return chapters


# ---------------------------------------------------------------------------
# 리포트에 병합
# ---------------------------------------------------------------------------

def _split_placeholder(chapter_text: str):
    """장 본문에서 "> 이 장은 사람이 작성합니다." 자리표시자 블록(연속된
    "..."로 시작하는 줄)을 찾아 (그 앞, 그 뒤)로 나눈다. 자리표시자가 없으면
    None을 반환한다 — 3·4장처럼 이미 자동 생성된 장이거나, 5장처럼 자리표시자
    뒤에 "### 참고 — 위키에서 찾은 관련 분석" 인용 소절이 남아 있는 장도
    "뒤(tail)" 쪽에 그대로 보존된다."""
    lines = chapter_text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.strip() == PLACEHOLDER_LINE:
            start = i
            break
    if start is None:
        return None

    end = start
    while end < len(lines) and lines[end].startswith(">"):
        end += 1

    head = "".join(lines[:start])
    tail = "".join(lines[end:])
    return head, tail


def merge_into_report(report_md: str, manual_dict: dict):
    """report_md의 사람 자리표시자를 manual_dict 내용으로 치환한다.

    반환값이 문자열 하나가 아니라 (병합된 리포트, 치환된 장 번호 목록, 남은
    장 번호 목록) 튜플인 이유: 어느 장이 실제로 채워졌고 어느 장이 여전히
    빈 채로 남았는지를 호출자(예: app.py 6단계 화면)가 문자열을 다시 파싱해서
    알아내게 하면 이 판단이 두 곳에 생긴다 — 이미 여기서 판단한 결과를 그대로
    같이 돌려주는 쪽이 어긋날 일이 없다.

    내용이 없는 장은 자리표시자를 그대로 남긴다 — 빈 문자열로 지우거나
    "(내용 없음)" 같은 문구로 채우지 않는다. "아직 안 썼다"는 사실 자체가
    다음 사람에게 전달돼야 한다.
    """
    manual_dict = manual_dict or {}
    matches = list(CHAPTER_HEADING_RE.finditer(report_md))
    if not matches:
        return report_md, [], []

    pieces = [report_md[: matches[0].start()]]
    substituted = []
    remaining = []

    for i, m in enumerate(matches):
        chapter_num = m.group(1)
        chap_start = m.start()
        chap_end = matches[i + 1].start() if i + 1 < len(matches) else len(report_md)
        chapter_text = report_md[chap_start:chap_end]

        split = _split_placeholder(chapter_text)
        if split is None:
            pieces.append(chapter_text)
            continue

        head, tail = split
        content = manual_dict.get(chapter_num)
        if content:
            substituted.append(chapter_num)
            pieces.append(head + content.strip() + "\n" + tail)
        else:
            remaining.append(chapter_num)
            pieces.append(chapter_text)

    return "".join(pieces), substituted, remaining
