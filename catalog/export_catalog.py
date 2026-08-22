"""위키(06_metrics, 02_data)의 정의를 앱이 읽는 JSON 카탈로그로 export한다.

CLAUDE.md 3절 참고: 앱은 위키 폴더를 직접 읽지 않는다. 이 스크립트가 사람이 읽는
위키 노트를 기계가 읽는 스냅샷(metrics_catalog.json, schema_catalog.json)으로
변환하는 유일한 통로다.

실행:
    python catalog/export_catalog.py
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

CATALOG_DIR = Path(__file__).resolve().parent

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
H1_RE = re.compile(r"^#\s+(.*)$")
STRIKETHROUGH_RE = re.compile(r"~~.*~~")
ARROW_OR_REALNAME_RE = re.compile(r"→|실물명")
SEPARATOR_ROW_RE = re.compile(r"^[\s\-:|]+$")
INSIGHT_LINK_RE = re.compile(r"\[\[(i-\d+[^\]|]*)\]\]")


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------

def load_frontmatter_and_body(path: Path):
    """파일을 읽어 (프론트매터 dict, 본문 문자열)을 반환한다.
    프론트매터가 없거나 파싱 실패하면 (None, None)을 반환한다."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None, None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, None
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None, None
    return fm, parts[2]


def extract_insight_links(body: str) -> list:
    """본문에서 [[i-005-요금제지역-이탈률]] 같은 인사이트 위키링크를 찾아
    insight_id(=인사이트 노트 파일명) 목록으로 뽑는다. 같은 노트를 본문
    여러 곳에서 링크하는 경우가 실제로 있어(예: monthly_churn_rate.md가
    i-005를 두 번 언급) 처음 나온 순서를 유지하며 중복을 제거한다."""
    seen = []
    for m in INSIGHT_LINK_RE.finditer(body):
        insight_id = m.group(1).strip()
        if insight_id not in seen:
            seen.append(insight_id)
    return seen


def find_section(body: str, keyword: str):
    """본문에서 제목에 keyword가 포함된 절의 내용을 찾는다.
    같은 레벨(또는 더 얕은 레벨) 다음 제목 전까지를 그 절의 내용으로 본다."""
    lines = body.splitlines()
    headings = []  # (level, title, line_idx)
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            headings.append((len(m.group(1)), m.group(2).strip(), i))

    for idx, (level, title, start) in enumerate(headings):
        if keyword in title:
            end = len(lines)
            for level2, _title2, start2 in headings[idx + 1:]:
                if level2 <= level:
                    end = start2
                    break
            return "\n".join(lines[start + 1:end]).strip()
    return None


# ---------------------------------------------------------------------------
# metrics_catalog.json
# ---------------------------------------------------------------------------

def build_metrics_catalog(wiki_path: Path):
    metrics_dir = wiki_path / "06_metrics"
    catalog = {}
    read_count = 0
    skipped = []

    for path in sorted(metrics_dir.glob("*.md")):
        if path.name.startswith("_") or path.name == "README.md":
            continue

        fm, body = load_frontmatter_and_body(path)
        if fm is None:
            warnings.warn(f"프론트매터 없음/파싱 실패, 건너뜀: {path.name}")
            skipped.append((path.name, "프론트매터 없음/파싱 실패"))
            continue

        metric_id = fm.get("metric_id")
        if not metric_id:
            warnings.warn(f"metric_id 없음, 건너뜀: {path.name}")
            skipped.append((path.name, "metric_id 없음"))
            continue

        entry = dict(fm)
        entry["답할_수_없는_것"] = find_section(body, "답할 수 없")
        # 임계값_상태(잠정/확정)는 프론트매터 필드고, 그 사유(사람이 읽는 긴 설명)는
        # 본문에 둔다 — "답할_수_없는_것"과 같은 이유로, 짧은 사실은 프론트매터,
        # 서술은 본문에 두는 이 위키의 관례를 그대로 따른다.
        entry["임계값_근거"] = find_section(body, "임계값 근거")
        # tags는 이미 fm(프론트매터) 안에 있으면 dict(fm)에 그대로 들어온다 —
        # 별도 로직으로 다시 채우지 않는다. 없는 노트는 그냥 없는 대로 둔다
        # (없다고 빈 리스트를 만들어 넣으면 "원래 없다"와 "있는데 비었다"가
        # 구분이 안 된다).
        entry["관련인사이트_본문링크"] = extract_insight_links(body)
        catalog[metric_id] = entry
        read_count += 1

    return catalog, read_count, skipped


# ---------------------------------------------------------------------------
# insights_catalog.json
# ---------------------------------------------------------------------------

def extract_title(body: str, fallback: str) -> str:
    """본문 첫 H1(# 제목)을 노트 제목으로 쓰고, 없으면 파일명을 쓴다.
    04_insights 노트들은 실제로는 프론트매터 다음에 바로 "## 근거"로
    시작해서(H1이 없다) 대부분 파일명으로 떨어진다 — 그래도 나중에 누가
    H1을 넣는 관례로 바꾸면 그쪽을 우선하도록 순서를 정해 둔다."""
    for line in body.splitlines():
        m = H1_RE.match(line.strip())
        if m:
            return m.group(1).strip()
    return fallback


def build_insights_catalog(wiki_path: Path):
    insights_dir = wiki_path / "04_insights"
    catalog = {}
    read_count = 0
    skipped = []

    for path in sorted(insights_dir.glob("*.md")):
        if path.name.startswith("_") or path.name == "README.md":
            continue

        fm, body = load_frontmatter_and_body(path)
        if fm is None:
            warnings.warn(f"프론트매터 없음/파싱 실패, 건너뜀: {path.name}")
            skipped.append((path.name, "프론트매터 없음/파싱 실패"))
            continue

        insight_id = path.stem

        시사점 = find_section(body, "시사점")
        if 시사점 is None:
            시사점 = find_section(body, "해석")

        entry = dict(fm)
        entry["제목"] = extract_title(body, insight_id)
        entry["시사점"] = 시사점

        catalog[insight_id] = entry
        read_count += 1

    return catalog, read_count, skipped


# ---------------------------------------------------------------------------
# schema_catalog.json
# ---------------------------------------------------------------------------

def resolve_note_name(fm: dict, path: Path) -> str:
    data_field = fm.get("data")
    if data_field:
        return str(data_field)
    return path.stem


def resolve_table_name(fm: dict, note_name: str):
    """(테이블명, 추정여부) 반환. bq_table 필드가 있으면 그걸 그대로 쓴다(추정 false)."""
    bq_table = fm.get("bq_table")
    if bq_table:
        return str(bq_table), False

    table_name = note_name
    if table_name.startswith("data_"):
        table_name = table_name[len("data_"):]
    if table_name.endswith(".csv"):
        table_name = table_name[:-len(".csv")]
    return table_name, True


def clean_cell(cell: str) -> str:
    return cell.replace("`", "").strip()


def parse_column_table(section_text: str, note_name: str, dirty_notes: set):
    """'## 컬럼' / '## 컬럼 정의 표' 절의 마크다운 표를 파싱한다.
    컬럼명·타입·설명(또는 의미) 3개 필드만 뽑는다."""
    lines = [l for l in section_text.splitlines() if l.strip().startswith("|")]
    if len(lines) < 2:
        return []

    header_cells = [clean_cell(c) for c in lines[0].strip().strip("|").split("|")]

    def find_idx(*candidates):
        for i, h in enumerate(header_cells):
            for cand in candidates:
                if cand in h:
                    return i
        return None

    idx_name = find_idx("컬럼명")
    idx_type = find_idx("타입")
    idx_desc = find_idx("설명", "의미")

    if idx_name is None or idx_type is None:
        warnings.warn(f"컬럼 표 헤더를 인식할 수 없음, 건너뜀: {note_name}")
        dirty_notes.add(note_name)
        return []

    columns = []
    # lines[0] = 헤더, lines[1] = 구분선(---|---)이라고 가정하고 그 뒤부터 데이터 행
    for row_line in lines[1:]:
        if SEPARATOR_ROW_RE.match(row_line.strip().strip("|")):
            continue
        cells = [c.strip() for c in row_line.strip().strip("|").split("|")]
        needed = max(idx_name, idx_type, idx_desc or 0)
        if len(cells) <= needed:
            continue

        raw_name = cells[idx_name]

        if STRIKETHROUGH_RE.search(raw_name):
            warnings.warn(
                f"[{note_name}] 취소선 컬럼 건너뜀(실물에 없을 가능성): {raw_name}"
            )
            dirty_notes.add(note_name)
            continue

        if ARROW_OR_REALNAME_RE.search(raw_name):
            warnings.warn(
                f"[{note_name}] 컬럼명에 화살표/실물명 표기가 섞여 파싱하지 않음: {raw_name}"
            )
            dirty_notes.add(note_name)
            continue

        name = clean_cell(raw_name)
        if not name:
            continue

        columns.append({
            "컬럼명": name,
            "타입": clean_cell(cells[idx_type]),
            "설명": clean_cell(cells[idx_desc]) if idx_desc is not None else "",
        })

    return columns


def build_schema_catalog(wiki_path: Path):
    data_dir = wiki_path / "02_data"
    catalog = {}
    read_count = 0
    skipped = []
    dirty_notes = set()

    for path in sorted(data_dir.glob("*.md")):
        if path.name == "README.md":
            continue

        fm, body = load_frontmatter_and_body(path)
        if fm is None:
            warnings.warn(f"프론트매터 없음/파싱 실패, 건너뜀: {path.name}")
            skipped.append((path.name, "프론트매터 없음/파싱 실패"))
            continue

        note_name = resolve_note_name(fm, path)
        table_name, is_guess = resolve_table_name(fm, note_name)

        col_section = find_section(body, "컬럼 정의 표")
        if col_section is None:
            col_section = find_section(body, "컬럼")

        if col_section is None:
            warnings.warn(f"컬럼 절('## 컬럼' 또는 '## 컬럼 정의 표') 없음, 건너뜀: {path.name}")
            skipped.append((path.name, "컬럼 절 없음"))
            continue

        columns = parse_column_table(col_section, note_name, dirty_notes)
        connection = find_section(body, "연결")

        entry = {
            "노트명": note_name,
            "테이블명": table_name,
            "테이블명_추정": is_guess,
            "컬럼": columns,
        }
        if connection is not None:
            entry["연결"] = connection

        catalog[table_name] = entry
        read_count += 1

    return catalog, read_count, skipped, dirty_notes


# ---------------------------------------------------------------------------
# 저장 + 메인
# ---------------------------------------------------------------------------

def save_json(data: dict, path: Path, wiki_path: Path, count: int):
    output = {
        "_meta": {
            "생성일시": datetime.now().isoformat(),
            "원천_위키_경로": str(wiki_path),
            "항목_개수": count,
        }
    }
    output.update(data)
    # default=str: 04_insights의 date/updated 필드는 YAML이 "2026-08-14"를
    # 문자열이 아니라 datetime.date로 자동 해석한다(06_metrics 프론트매터에는
    # 이런 필드가 없어서 지금까지는 안 걸렸다, 실측으로 확인함). 값을 임의로
    # 재해석하지 않고 원래 표기 그대로(ISO 문자열) 남기는 가장 단순한 방법이라
    # date.__str__()에 맡긴다.
    path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def main() -> int:
    if config.WIKI_PATH is None:
        print("WIKI_PATH가 설정되지 않았습니다. config.py를 직접 고치세요.")
        return 2

    wiki_path = config.WIKI_PATH
    print(f"위키 경로: {wiki_path}")
    print()

    # --- metrics_catalog ---
    metrics, m_read, m_skipped = build_metrics_catalog(wiki_path)
    save_json(metrics, CATALOG_DIR / "metrics_catalog.json", wiki_path, len(metrics))

    print(f"[metrics_catalog] 읽음 {m_read}개 / 건너뜀 {len(m_skipped)}개")
    for name, reason in m_skipped:
        print(f"  - {name}: {reason}")
    print()

    # --- insights_catalog ---
    insights, i_read, i_skipped = build_insights_catalog(wiki_path)
    save_json(insights, CATALOG_DIR / "insights_catalog.json", wiki_path, len(insights))

    print(f"[insights_catalog] 읽음 {i_read}개 / 건너뜀 {len(i_skipped)}개")
    for name, reason in i_skipped:
        print(f"  - {name}: {reason}")
    print()

    tags_없는_지표 = [mid for mid, fm in metrics.items() if not fm.get("tags")]
    if tags_없는_지표:
        print(f"tags 없는 지표({len(tags_없는_지표)}개, 프론트매터에 원래 없어서 못 채움): "
              f"{', '.join(tags_없는_지표)}")
    else:
        print("tags: 모든 지표 프론트매터에 이미 있음(추가 작업 없음)")
    print()

    print("지표별 본문 내 인사이트 링크([[i-XXX]]) 개수:")
    for metric_id, fm in metrics.items():
        links = fm.get("관련인사이트_본문링크", [])
        print(f"  - {metric_id}: {len(links)}개" + (f" ({', '.join(links)})" if links else ""))
    print()

    # --- schema_catalog ---
    schema, s_read, s_skipped, dirty_notes = build_schema_catalog(wiki_path)
    save_json(schema, CATALOG_DIR / "schema_catalog.json", wiki_path, len(schema))

    print(f"[schema_catalog] 읽음 {s_read}개 / 건너뜀 {len(s_skipped)}개")
    for name, reason in s_skipped:
        print(f"  - {name}: {reason}")
    print()

    if dirty_notes:
        print("정리가 필요한 노트 (컬럼 표에 편집 표시가 섞여 있음):")
        for name in sorted(dirty_notes):
            print(f"  - {name}")
        print()

    print(f"요약: metrics {len(metrics)}개 / insights {len(insights)}개 / tables {len(schema)}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
