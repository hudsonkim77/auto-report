"""명령줄에서 1~3·6·7단계를 실행하는 스크립트.

CLAUDE.md 개념 5절을 그대로 따른다 — 특히 5-2(계산 엔진=BigQuery, 스테이징
테이블만 갱신, DML 금지)와 5-5(재현성: 계산에 현재 시각을 쓰지 않고, 기간은
업로드 파일에서 판정하거나 사용자가 --month로 직접 지정한다)를 그대로
이어받는다. app.py가 부르는 것과 같은 pipeline 모듈을 그대로 호출하므로
화면과 CLI가 서로 다른 계산·검증 로직을 갖게 되는 일이 없다.

app.py를 여기서 import하지 않는 이유: app.py는 모듈 로드 시점에
st.set_page_config()를 실행하는 Streamlit 스크립트라, Streamlit 런타임 밖에서
import하면 그 부작용이 그대로 터진다. CSV 읽기 같은 짧은 로직은 이 파일에
따로 둔다.

4단계(대시보드)·5단계(사람 확인)는 화면 전용이라 CLI에는 없다. 8단계(발송
확정)는 되돌릴 수 없는 마지막 승인이라 CLI로 만들지 않는다 — 이 스크립트는
7단계(이메일 초안)까지만 만들고, 발송 확정은 항상 streamlit run app.py로
넘긴다.

사용법:
    python run_pipeline.py --file 수업자료/usage_history_2025-01.csv
    python run_pipeline.py --file xxx.csv --approve-extension
    python run_pipeline.py --file xxx.csv --month 2025-01
"""

# 파이썬 3.10 미만에서도 "dict | None" 같은 새 타입 표기를 쓸 수 있게 한다.
# (지금 타입 힌트를 문자열로 미리 평가시켜 버전 호환성을 넓히는 표준 관용구.)
from __future__ import annotations

import argparse  # --file, --month 같은 명령줄 인자를 해석하는 표준 라이브러리
import json  # run_log.json, validation.json 등 결과를 JSON으로 저장/변환
import re  # --month 값이 "YYYY-MM" 형식인지 검사할 정규식
import sys  # 인자 목록(sys.argv), 표준출력 인코딩, 종료 코드(sys.exit) 제어
from datetime import datetime  # 로그 시각, run 폴더명(run_YYYYMMDD_HHMM) 생성
from pathlib import Path  # 모든 파일 경로를 OS 독립적으로 다루기 위해 사용

import pandas as pd  # CSV를 표(DataFrame)로 읽어 이후 판정·계산에 넘긴다

# Windows 콘솔은 기본 인코딩이 cp949라, 로그 문구의 "—" 같은 기호를 만나면
# print()가 그대로 죽는다(실측: UnicodeEncodeError로 스크립트 자체가 죽고
# 그 결과 우연히 exit code 1이 나와 "검증 차단"처럼 보였다 — 실제로는 검증까지
# 가지도 못한 크래시였다). 표준출력을 UTF-8로 강제해 이 스크립트를 실행하는
# 모든 환경에서 같게 동작하게 한다.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 이 파일(run_pipeline.py)이 있는 폴더 = auto-report 프로젝트 루트.
# __file__은 이 스크립트 자신의 경로, .resolve()로 절대경로화, .parent로
# 파일이 아니라 그 파일이 들어있는 "폴더"를 얻는다.
APP_DIR = Path(__file__).resolve().parent
CATALOG_DIR = APP_DIR / "catalog"  # 위키에서 export된 지표/스키마/인사이트 JSON이 있는 폴더

# 이 스크립트를 auto-report 폴더 밖에서 실행해도(예: 다른 작업 폴더에서
# `python C:\...\auto-report\run_pipeline.py`처럼) "pipeline" 패키지를 찾을 수
# 있도록, 프로젝트 루트를 파이썬이 모듈을 찾는 경로 목록(sys.path) 맨 앞에
# 넣어둔다. 아래 pipeline.* import보다 반드시 먼저 실행돼야 한다.
sys.path.insert(0, str(APP_DIR))

# app.py가 화면에서 부르는 것과 똑같은 pipeline 모듈들을 그대로 가져온다.
# CLI만을 위한 계산/검증 로직을 따로 만들지 않는다 — 그러면 화면과 CLI가
# 서로 다른 답을 낼 위험이 생긴다.
from pipeline import profile as profiler       # 2단계: 테이블 판정 + 지표 판정 (BigQuery 호출 없음)
from pipeline import calculate as calculator   # 2단계: BigQuery 스테이징 적재 + 지표 계산
from pipeline import compare as comparator     # 3단계 준비: 전월 값 계산 + 당월/전월 비교
from pipeline import validate as validator     # 3단계: 기계적 검증(유효구간/표본/정합성/전월비교/합계)
from pipeline import report as reporter        # 6단계: 마크다운 리포트 + PDF 생성
from pipeline import email_draft               # 7단계: 이메일 초안(제목·본문·첨부목록) 생성

# report.py가 항상 자동으로 채우는 장 번호(1·3·4·7·8) — 이번 실행 내용과
# 무관하게 report.py의 "구조" 자체가 고정한 값이라 여기서도 상수로 고정한다.
AUTO_GENERATED_CHAPTERS = ["1", "3", "4", "7", "8"]

# --month 인자가 "2025-01"처럼 4자리-2자리 형식인지 검사하는 정규식.
# ^...$로 문자열 전체가 이 형식과 정확히 일치해야만 통과시킨다.
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _log(stage: str, summary: str) -> None:
    """"[시각] 단계명: 요약" 한 줄을 표준출력에 찍는다.

    모든 단계가 이 함수 하나로 로그를 남기게 해서, 로그 줄의 형식(시각 표기
    방식 등)을 한 곳만 고치면 전체가 같이 바뀐다."""
    ts = datetime.now().isoformat(timespec="seconds")  # 예: 2026-08-22T16:20:24
    print(f"[{ts}] {stage}: {summary}")


def _read_csv_with_fallback(path: Path) -> pd.DataFrame:
    """CSV를 읽는다. 엑셀에서 저장한 한글 CSV는 보통 BOM이 붙은 utf-8-sig다.
    그게 아니면(옛 시스템에서 만든 파일 등) cp949(한글 Windows 기본 인코딩)로
    한 번 더 시도한다. 둘 다 실패하면 예외가 호출자에게 그대로 올라간다."""
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="cp949")


def _load_catalogs():
    """카탈로그 3종(지표·스키마·인사이트)을 읽는다.

    metrics_catalog.json·schema_catalog.json 둘 중 하나라도 없으면 이 함수는
    None을 반환하고, 종료 코드를 정하는 건 호출자(main)에게 맡긴다 — 이
    함수 안에서 곧바로 sys.exit()하면, 나중에 이 함수를 다른 스크립트(테스트
    등)에서 재사용할 때 항상 프로세스가 죽어버리는 부작용이 생긴다.
    insights_catalog.json은 없어도 되는 선택 카탈로그라 없으면 빈 dict({})로
    대체한다(report.py의 5장 인용이 "관련 분석 없음"으로 처리할 뿐, 실행이
    막히지는 않는다)."""
    metrics_path = CATALOG_DIR / "metrics_catalog.json"
    schema_path = CATALOG_DIR / "schema_catalog.json"
    insights_path = CATALOG_DIR / "insights_catalog.json"
    if not (metrics_path.exists() and schema_path.exists()):
        return None
    metrics_catalog = json.loads(metrics_path.read_text(encoding="utf-8"))
    schema_catalog = json.loads(schema_path.read_text(encoding="utf-8"))
    insights_catalog = json.loads(insights_path.read_text(encoding="utf-8")) if insights_path.exists() else {}
    return metrics_catalog, schema_catalog, insights_catalog


def parse_args(argv=None) -> argparse.Namespace:
    """명령줄 인자를 정의하고 해석한다. argv=None이면 실제 sys.argv[1:]를
    쓰고, 테스트에서는 리스트를 직접 넘겨 argparse를 우회할 수 있다."""
    parser = argparse.ArgumentParser(
        description="auto-report 1~7단계를 명령줄에서 실행한다. 8단계(발송 확정)는 화면에서만 한다.",
        epilog=(
            "예:\n"
            "  python run_pipeline.py --file 수업자료/usage_history_2025-01.csv\n"
            "  python run_pipeline.py --file xxx.csv --approve-extension\n"
            "  python run_pipeline.py --file xxx.csv --month 2025-01"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,  # epilog의 줄바꿈을 그대로 살린다
    )
    # 필수 인자: 업로드할 CSV 파일의 경로. 이게 없으면 argparse가 자동으로
    # 사용법을 출력하고 종료 코드 2로 끝낸다(우리 코드가 개입하기 전에 멈춤).
    parser.add_argument("--file", required=True, help="업로드할 CSV 경로 (필수)")

    # 선택 인자: 계산 대상 기간을 사람이 직접 지정. 생략하면 파일 안의
    # 날짜/기간 컬럼에서 자동으로 판정한다(CLAUDE.md 5-5 "재현성" — 기간을
    # 현재 시각이 아니라 데이터나 사용자 지정값에서 가져온다).
    parser.add_argument("--month", default=None, help="YYYY-MM. 생략하면 파일에서 기간을 자동 판정한다.")

    # 플래그(값 없이 있으면 True): 지표 정의서의 유효구간을 벗어난 기간도
    # 강제로 계산할지 여부. 이게 없으면 확장이 필요한 지표가 하나라도 있을 때
    # 그 자리에서 멈춘다(화면의 게이트1 체크박스와 같은 역할).
    parser.add_argument(
        "--approve-extension", action="store_true",
        help="유효구간 확장을 승인한다. 없으면 확장이 필요할 때 그 자리에서 중단한다.",
    )

    # 선택 인자: 실행 결과(run_* 폴더)를 어디에 만들지. 기본은 auto-report/outputs.
    parser.add_argument("--output-dir", default=None, help="run_* 폴더를 만들 위치. 기본값: auto-report/outputs")

    # 플래그: PDF 생성을 건너뛸지. 마크다운 리포트는 항상 만들고, PDF만
    # 선택적으로 뺄 수 있게 한다(폰트 렌더링이 느리거나 PDF가 필요 없을 때).
    parser.add_argument(
        "--skip-pdf", action="store_true",
        help="PDF 생성을 생략한다(마크다운 리포트만 만든다).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    """전체 파이프라인의 진입점. 반환값이 곧 프로세스 종료 코드다
    (0=정상, 1=검증 차단, 2=그 외 오류) — 맨 아래 `sys.exit(main())`에서
    이 반환값을 그대로 OS에 전달한다."""
    args = parse_args(argv)

    # --- 0. 입력값 자체의 형식 검사 ---
    # 실제 계산을 시작하기 전에, 사람이 잘못 입력했을 가능성이 가장 큰
    # --month부터 먼저 검사한다. 형식이 틀리면 이후 코드(년/월로 쪼개는 부분)
    # 에서 알아보기 어려운 에러가 나기 전에 여기서 바로 잡는다.
    if args.month and not MONTH_RE.match(args.month):
        _log("입력 확인", f"중단 — --month는 YYYY-MM 형식이어야 합니다: {args.month!r}")
        return 2

    # --file로 받은 경로가 실제로 존재하는 파일인지 확인.
    csv_path = Path(args.file)
    if not csv_path.exists():
        _log("1단계 파일 읽기", f"중단 — 파일을 찾을 수 없습니다: {csv_path}")
        return 2

    # 카탈로그(지표 정의·스키마·인사이트)가 없으면 판정/계산 자체가 불가능하다.
    # export_catalog.py를 먼저 돌려야 한다는 안내를 주고 여기서 멈춘다.
    catalogs = _load_catalogs()
    if catalogs is None:
        _log("0단계 카탈로그", "중단 — catalog/*.json이 없습니다. python catalog/export_catalog.py를 먼저 실행하세요.")
        return 2
    metrics_catalog, schema_catalog, insights_catalog = catalogs

    # --- 1단계: 파일 읽기 ---
    # 업로드된 CSV를 pandas DataFrame으로 읽는다. 인코딩 문제는
    # _read_csv_with_fallback이 내부에서 한 번 더 시도해준다.
    try:
        df = _read_csv_with_fallback(csv_path)
    except Exception as e:  # noqa: BLE001 - 원인을 그대로 보여준다
        _log("1단계 파일 읽기", f"중단 — CSV를 읽을 수 없습니다(utf-8-sig, cp949 둘 다 실패): {e}")
        return 2
    _log("1단계 파일 읽기", f"{csv_path.name} — {len(df):,}행, {len(df.columns)}컬럼")

    # --- 2단계: 판정 (어느 테이블인지 + 어떤 지표를 계산할 수 있는지) ---
    # judge_table: 업로드 파일의 컬럼 구성을 schema_catalog의 각 테이블과
    # 비교해서 가장 비슷한 테이블 하나를 고른다(BigQuery를 부르지 않는
    # 순수 계산). "판정가능"이 False면 어느 테이블인지 확신할 수 없다는
    # 뜻이라, CLAUDE.md 9절("카탈로그에 없는 테이블을 추측해 통과 금지")에
    # 따라 여기서 중단한다.
    table_judgment = profiler.judge_table(df, schema_catalog)
    if not table_judgment["판정가능"]:
        _log(
            "2단계 판정",
            f"중단 — 테이블을 판정할 수 없습니다(최선 후보 {table_judgment['테이블명']}, "
            f"일치율 {table_judgment['일치율']:.0%})",
        )
        return 2
    table_name = table_judgment["테이블명"]
    _log("2단계 판정", f"{table_name} (일치율 {table_judgment['일치율']:.0%})")

    # profile_data: 행수·컬럼수·결측치·기간 컬럼·그레인(중복 여부) 등을
    # 점검한다. table_info(그 테이블의 스키마 정의)를 참고해 컬럼 의미를
    # 더 정확히 해석한다.
    table_info = schema_catalog.get(table_name, {})
    profile_result = profiler.profile_data(df, table_info)

    # 계산 대상 기간을 정한다: --month를 직접 줬으면 그 값을 그대로 쓰고
    # (CLAUDE.md 5-5: 사용자가 지정한 값도 재현성 있는 방법으로 허용),
    # 아니면 파일 안에서 자동으로 찾은 기간 컬럼(예: year_month) 중 첫
    # 번째의 최소~최대 범위를 쓴다. 둘 다 없으면 계산할 기간 자체가
    # 정해지지 않은 것이므로 중단한다.
    if args.month:
        period = {"최소": args.month, "최대": args.month}
    else:
        primary_period = next(iter(profile_result["기간컬럼"].values()), None)
        if primary_period is None:
            _log("2단계 판정", "중단 — 기간을 자동으로 판정할 수 없습니다. --month로 직접 지정하세요.")
            return 2
        period = primary_period
    기간_str = f"{period['최소']} ~ {period['최대']}"  # 로그·리포트에 그대로 쓸 "2024-12 ~ 2024-12" 형태 문자열

    # judge_metrics: 카탈로그의 모든 지표를 훑어서, 이번에 판정된 테이블로
    # 계산 가능한지("계산가능"/"유효구간 확장 필요"/"이 파일과 무관")를
    # 지표별로 판정한다.
    metric_judgments = profiler.judge_metrics(table_name, period, metrics_catalog)

    # 실제로 계산을 시도할 지표(계산가능 + 유효구간 확장 필요)만 골라낸다.
    계산대상 = [m for m in metric_judgments if m["상태"] in ("계산가능", "유효구간 확장 필요")]
    # 그중 정의서의 유효구간을 벗어나 "확장 승인"이 필요한 지표만 따로 추림.
    확장필요_목록 = [m for m in metric_judgments if m["상태"] == "유효구간 확장 필요"]
    # 이 업로드 파일 하나로는 갱신되지 않는 다른 원천 테이블도 함께 쓰는
    # (그래서 "최신 상태를 전부 반영하지는 못하는") 지표 목록.
    부분갱신_목록 = [m for m in metric_judgments if m["부분갱신여부"]]

    # 유효구간 확장이 필요한 지표가 있는데 --approve-extension을 안 줬으면,
    # 화면의 게이트1 체크박스와 똑같은 규칙으로 여기서 멈춘다. 어떤 지표가
    # 왜 막혔는지 전부 나열해서, 사용자가 --approve-extension을 붙여
    # 다시 실행할지 판단할 수 있게 한다.
    if 확장필요_목록 and not args.approve_extension:
        _log("2단계 판정", f"중단 — 유효구간 확장이 필요한 지표 {len(확장필요_목록)}종, --approve-extension 없음")
        for m in 확장필요_목록:
            print(f"  - {m['metric_id']} ({m['지표명']}): {m['이유']}")
        return 2

    target_ids = [m["metric_id"] for m in 계산대상]  # 이제부터 실제 계산에 넘길 metric_id 목록
    _log(
        "2단계 판정",
        f"계산 대상 {len(target_ids)}종 "
        f"(유효구간 확장 {len(확장필요_목록)}종, 부분갱신 {len(부분갱신_목록)}종)",
    )

    # 이 실행 하나를 식별하는 고유 id. 스테이징 테이블명과 run 폴더명에 똑같이
    # 붙여서 서로를 이름만으로 연결할 수 있게 하고, 클라우드에서 여러 실행이
    # 동시에 겹쳐도 서로의 스테이징 테이블을 덮어쓰지 않게 한다.
    run_id = calculator.new_run_id()

    # --- 2단계: 계산 (BigQuery 스테이징 적재 — DDL만, DML 없음) ---
    try:
        # get_client(): BigQuery 클라이언트를 만든다. 로컬에서는 gcloud ADC를
        # 쓰고, Streamlit Cloud 같은 배포 환경에서는 Secrets에 있는 서비스
        # 계정을 대신 쓴다(calculate.py 쪽에서 자동으로 판단).
        client = calculator.get_client()
        # load_staging_table(): 업로드 df를 staging_{table_name}_{run_id}
        # 테이블로 적재한다. CREATE OR REPLACE TABLE(DDL)만 쓰고 INSERT 등
        # DML은 절대 쓰지 않는다 — BigQuery 샌드박스(결제 계정 없는 프로젝트)
        # 에서도 동작해야 하기 때문이다(CLAUDE.md 5-2). run_id를 붙이는 이유는
        # 위 run_id 주석 참고 — 실행마다 새 테이블이라 서로 안 겹친다.
        staging_table = calculator.load_staging_table(df, table_name, client, run_id)
        # compute_target_metrics(): target_ids에 있는 지표들을 정의서의
        # 계산 명세(원천·집계·조인·조건)대로 SQL을 조립해 실제로 계산한다.
        # 지표별로 하드코딩된 SQL은 없다 — 전부 카탈로그 명세에서 조립된다.
        results, sql_log = calculator.compute_target_metrics(
            target_ids, table_name, staging_table, period["최소"], period["최대"],
            metrics_catalog, client, args.approve_extension,
        )
    except calculator.AuthError as e:
        # BigQuery 인증 자체가 안 돼 있으면(로컬 ADC 로그인 안 함 등) 계산을
        # 시도조차 못 한 것이므로, 원인과 해결 명령을 그대로 보여주고 멈춘다.
        _log("2단계 계산", f"중단 — BigQuery 인증 실패: {e}")
        print("아래 명령으로 인증한 뒤 다시 실행하세요: gcloud auth application-default login")
        return 2
    _log("2단계 계산", f"{len(results)}개 지표 계산 완료 (스테이징 테이블: {staging_table})")

    # --- run 폴더 생성 (계산 성공 후에만 — 실패 흔적을 outputs/에 남기지 않는다) ---
    # --output-dir을 줬으면 그 경로를, 아니면 auto-report/outputs를 기본으로 쓴다.
    output_root = Path(args.output_dir) if args.output_dir else (APP_DIR / "outputs")
    run_time = datetime.now()  # 이 실행의 "확정 시각"으로 기록에 함께 쓴다(폴더명은 run_id를 쓴다)
    # 폴더명도 스테이징 테이블과 같은 run_id를 쓴다 — 분 단위 폴더명(예전 방식)은
    # 클라우드에서 여러 실행이 같은 분에 끝나면 서로 폴더를 덮어쓸 수 있었다.
    run_dir = output_root / f"run_{run_id}"  # 예: outputs/run_20260822_164512_a1b2c3
    run_dir.mkdir(parents=True, exist_ok=True)  # 중간 폴더까지 한 번에 생성, 이미 있어도 에러 안 냄
    # 업로드 원본을 그대로 복사해 둔다(CLAUDE.md 5-5 재현성) — 나중에 "이
    # 실행에 정확히 무엇을 넣었는지"를 다시 확인할 수 있게.
    (run_dir / csv_path.name).write_bytes(csv_path.read_bytes())

    # 이 실행에 쓰인 카탈로그가 언제 export됐는지(= 어느 정의서 버전으로
    # 계산했는지) 기록해 둔다. 리포트·이메일에도 그대로 노출된다.
    catalog_generated_at = metrics_catalog.get("_meta", {}).get("생성일시", "알 수 없음")

    # run_log.json 뼈대를 만든다. 앞부분(파일명~확정_시각)은 app.py가 만드는
    # run_log.json과 같은 "평평한" 키들이다 — report.py·email_draft.py가
    # run_context["run_log"]에서 그대로 읽어 쓰므로, 화면이든 CLI든 같은
    # 모양이어야 한다. 뒷부분(0_카탈로그 ~ CLI_실행)은 8단계 전체를 사람이
    # 훑어볼 수 있게 남기는 상세 실행 기록이다.
    run_log = {
        "파일명": csv_path.name,
        "행수": profile_result["행수"],
        "판정_테이블": table_name,
        "기간": 기간_str,
        "카탈로그_생성일시": catalog_generated_at,
        "계산_대상_지표": target_ids,
        "유효구간_확장_승인": bool(확장필요_목록) and args.approve_extension,
        "유효구간_확장_승인_시각": run_time.isoformat() if 확장필요_목록 else None,
        "부분_갱신_지표": [m["metric_id"] for m in 부분갱신_목록],
        "확정_시각": run_time.isoformat(),

        # 0단계: 이번 실행이 어느 카탈로그(지표/테이블/인사이트 개수) 버전을
        # 썼는지. 나중에 "그때 정의서 몇 개짜리로 계산했더라"를 추적하는 용도.
        "0_카탈로그": {
            "생성일시": catalog_generated_at,
            "지표_개수": metrics_catalog.get("_meta", {}).get("항목_개수", "?"),
            "테이블_개수": schema_catalog.get("_meta", {}).get("항목_개수", "?"),
            "인사이트_개수": insights_catalog.get("_meta", {}).get("항목_개수", "?"),
        },
        # 1단계: 투입된 파일 자체의 메타데이터.
        "1_투입": {
            "파일명": csv_path.name,
            "크기_바이트": csv_path.stat().st_size,
            "행수": profile_result["행수"],
            "컬럼_수": profile_result["컬럼수"],
            "업로드_시각": run_time.isoformat(),
        },
        # 2단계(판정 부분): 어느 테이블로 얼마나 정확히 매칭됐는지, 기간·결측·
        # 그레인(중복 행) 점검 결과.
        "2_판정": {
            "테이블": table_name,
            "일치율": table_judgment["일치율"],
            "기간": profile_result["기간컬럼"],
            "결측": profile_result["결측"],
            "그레인_후보": profile_result["그레인_후보"],
        },
        # 2단계(계산 부분): 지표별 실제 계산값·상태, 어느 스테이징 테이블을
        # 썼는지, 계산이 끝난 시각.
        "2_계산": {
            "지표별_값_상태": {r.metric_id: {"값": r.value, "상태": r.status} for r in results},
            "스테이징_테이블명": staging_table,
            "계산_시각": run_time.isoformat(),
        },
        # 게이트1: 화면에서는 사람이 "이 판정으로 계산 진행" 버튼을 누르는
        # 지점. CLI에서는 --approve-extension 플래그가 그 승인을 대신한다.
        "게이트1": {
            "확정_시각": run_time.isoformat(),
            "유효구간_확장_승인_여부": bool(확장필요_목록) and args.approve_extension,
            "승인_방식": "CLI --approve-extension" if args.approve_extension else "해당 없음",
        },
        # 4단계(대시보드)·5단계(사람 확인)는 화면에만 있는 단계라 CLI에는
        # 아예 없다 — "안 한 게 아니라 이 실행 방식에는 원래 없다"는 걸
        # run_log에도 명시적으로 남겨서, 나중에 이 기록만 보고 "왜 확인
        # 기록이 없지?"라고 오해하지 않게 한다.
        "4_대시보드": {"생략": True, "이유": "화면 전용 — CLI에는 없음"},
        "4_5_확인": {"생략": True, "이유": "화면에서 사람이 확인하는 단계 — CLI에는 없음"},
        # CLI를 정확히 어떤 명령으로 불렀는지 자체를 남긴다(게이트1.승인_방식과
        # 내용은 겹치지만, 여기는 "이 실행이 CLI에서 어떻게 시작됐는가"를
        # 통째로 보여주는 자리라 sys.argv 전체를 그대로 적어 둔다).
        "CLI_실행": {
            "명령": " ".join(sys.argv),
            "approve_extension_사용": args.approve_extension,
            "month_직접지정": args.month,
        },
    }
    # 여기까지의 run_log를 파일로 먼저 저장해 둔다 — 이후 단계(검증·리포트·
    # 이메일)가 중간에 실패해도, "여기까지는 성공했다"는 기록이 남는다.
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    calculator.save_metrics_csv(results, run_dir)  # 지표별 계산 결과를 metrics.csv로도 저장
    _log("2단계 계산", f"run 폴더 생성: {run_dir}")

    # --- 3단계: 검증 ---
    # 전월(비교 기준월) 값을 계산해서 당월과 나란히 비교한다.
    prev_period = comparator.previous_month(period["최소"])  # 예: "2024-12" -> "2024-11"
    current_metric_df = comparator.metric_results_to_df(results)  # 이번 실행 결과를 비교용 표 형태로 변환
    prev_df = comparator.calc_previous(target_ids, prev_period, client)  # 전월 값을 같은 방식으로 재계산
    comparison_df = comparator.compare(current_metric_df, prev_df, metrics_catalog)  # 당월 vs 전월 비교표
    comparison_df.to_csv(run_dir / "comparison.csv", index=False, encoding="utf-8-sig")

    # validate_all()에 넘길 지표별 상세 표(metric_id·값·상태·표본수 등)를 만든다.
    metrics_df = pd.DataFrame([{
        "metric_id": r.metric_id, "지표명": r.지표명, "유형": r.유형, "month": r.month,
        "value": r.value, "sample_size": r.sample_size, "min_sample": r.min_sample,
        "status": r.status, "부분갱신": r.부분갱신, "원천": "+".join(r.원천), "error": r.error or "",
    } for r in results])
    table_override = {table_name: staging_table}  # 검증 쿼리도 원본이 아니라 스테이징 테이블을 보게 한다
    uploaded_months = {calculator.parse_year_month(period["최소"])}  # (연, 월) 튜플 — 스테이징 치환이 적용될 달

    # validate_all(): 유효구간 위반·표본부족·파생지표 정합성·전월대비 이상
    # 변동·합계 대조 5종을 기계적으로만 점검한다(혼입변수·역인과·가설검정은
    # 판단이 필요해 자동으로 하지 않고, 그 사실 자체를 결과에 명시한다).
    validation_result = validator.validate_all(
        metrics_df, comparison_df, metrics_catalog, client,
        override=args.approve_extension, table_override=table_override,
        uploaded_months=uploaded_months,
    )
    (run_dir / "validation.json").write_text(
        json.dumps(validation_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    run_log["3_검증"] = validation_result  # 검증 결과 전체를 run_log에도 그대로 합쳐 넣는다
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    _log(
        "3단계 검증",
        f"{validation_result['전체판정']} (차단 {validation_result['차단수']}건, 경고 {validation_result['경고수']}건)",
    )

    # 검증에서 "차단"이 하나라도 나오면 리포트·이메일을 만들지 않고 여기서
    # 멈춘다 — 검증을 통과하지 못한 숫자가 리포트에 실리면 안 된다
    # (CLAUDE.md 9절). 어떤 항목이 왜 차단됐는지 전부 나열하고 종료 코드 1로
    # 끝낸다(0=정상, 1=검증 차단, 2=그 외 오류라는 규칙).
    if validation_result["전체판정"] == "차단":
        print("검증 차단 항목:")
        for f in validation_result["항목별_결과"]:
            if f["판정"] == "차단":
                print(f"  - [{f['검증명']}] {f['대상지표']}: {f['상세']}")
        print()
        print(f"run 폴더: {run_dir}")
        return 1

    # --- 4단계(대시보드) — 화면 전용이라 생략 ---
    # (대시보드는 사람이 눈으로 보는 화면이라 CLI에서 만들 결과물이 없다.
    # 이 자리는 8단계 구조에서 몇 단계가 빠지는지 코드 흐름상 명확히
    # 보여주기 위해 일부러 비워 둔다.)

    # --- 6단계: 리포트 생성 (manual/sections.md 병합 포함) ---
    # report.py·email_draft.py가 공통으로 요구하는 "run_context" 딱 하나를
    # 만들어 두 단계에 그대로 넘긴다 — 화면(app.py)도 같은 모양의 dict를
    # 만들어 넘기므로, 두 모듈은 자기가 CLI에서 불렸는지 화면에서 불렸는지
    # 전혀 알 필요가 없다.
    run_context = {
        "파일명": csv_path.name,
        "판정테이블": table_name,
        "기간": 기간_str,
        "행수": profile_result["행수"],
        "metrics": metrics_df,
        "comparison": comparison_df,
        "validation": validation_result,
        "metrics_catalog": metrics_catalog,
        "schema_catalog": schema_catalog,
        "insights_catalog": insights_catalog,
        "run_log": run_log,
        "run_dir": str(run_dir),
    }

    # build_report(): 8장 마크다운을 만들고, manual/sections.md에 사람이 이미
    # 써둔 2·5·6장 내용을 자동 생성분과 병합하고, 금지 표현(인과 단정·제안·
    # 가치판단)을 자체 검사한 결과까지 함께 돌려준다.
    report_result = reporter.build_report(run_context)
    (run_dir / "report.md").write_text(report_result["report_md"], encoding="utf-8")
    run_log["6_리포트"] = {
        "생성_시각": datetime.now().isoformat(),
        "자동생성_장_목록": AUTO_GENERATED_CHAPTERS,
        "미작성_장_목록": report_result["미작성_장"],  # 아직 사람이 안 쓴 장(보통 2·5·6장 중 일부/전부)
        "금지표현_검사_결과": report_result["금지표현_검사"],
    }
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _log("6단계 리포트", f"생성 완료(report.md). 미작성 장 {len(report_result['미작성_장'])}개")

    pdf_생성됨, pdf_오류 = False, None
    if args.skip_pdf:
        _log("6단계 리포트", "PDF 생성 생략 요청됨(--skip-pdf)")
    else:
        # 마크다운 리포트는 이미 성공했다 — 폰트/fpdf2 문제로 PDF만 실패해도
        # 전체 실행을 중단하지 않는다(마크다운·이메일은 그대로 살린다).
        # build_pdf()는 Noto Sans KR 서브셋 폰트로 텍스트·표만 그린다(차트
        # 이미지는 kaleido가 불안정해서 넣지 않는다 — CLAUDE.md 7절).
        try:
            pdf_bytes = reporter.build_pdf(report_result["report_md"])
            (run_dir / "report.pdf").write_bytes(pdf_bytes)
            pdf_생성됨 = True
            _log("6단계 리포트", "PDF 생성 완료(report.pdf)")
        except Exception as e:  # noqa: BLE001 - PDF만의 실패로 전체를 죽이지 않는다
            pdf_오류 = str(e)
            _log("6단계 리포트", f"PDF 생성 실패(마크다운은 유지) — {pdf_오류}")

    # PDF 생성 성공/실패 여부도 실행 기록에 남긴다 — 이메일 첨부 목록에서
    # report.pdf의 실제 존재 여부·크기를 그대로 보여줄 수 있게 된다.
    run_log["6_리포트"]["PDF_생성됨"] = pdf_생성됨
    run_log["6_리포트"]["PDF_오류"] = pdf_오류
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # --- 7단계: 이메일 초안 생성 ---
    # build_email(): 제목(경고/미작성 장이 있으면 "(확인 필요)"·"(초안)" 자동
    # 부착)·수신자(config.py의 예시값, 실제 주소는 코드에 없음)·본문(html·
    # text 두 버전)·첨부 목록(report.md/pdf, metrics.csv, comparison.csv —
    # 실제 파일 첨부는 8주차, 지금은 목록만)을 만든다. 실제 SMTP 발송은
    # 하지 않는다(CLAUDE.md 5-3).
    email = email_draft.build_email(run_context, report_result["report_md"])
    (run_dir / "email.html").write_text(email["body_html"], encoding="utf-8")
    (run_dir / "email.txt").write_text(email["body_text"], encoding="utf-8")
    (run_dir / "email_meta.json").write_text(
        json.dumps(
            {"subject": email["subject"], "to": email["to"], "from": email["from"],
             "attachments": email["attachments"]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    run_log["7_이메일"] = {
        "제목": email["subject"], "수신자": email["to"],
        "첨부_목록": email["attachments"], "생성_시각": datetime.now().isoformat(),
    }
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _log("7단계 이메일", f"초안 생성 완료. 제목: {email['subject']}")

    # --- 8단계는 하지 않는다 ---
    # 발송 확정은 되돌릴 수 없는 마지막 승인이라, 이 CLI가 대신 누르지 않고
    # 항상 사람이 화면에서 직접 확인 후 눌러야 한다. 여기서는 지금까지 만든
    # 결과를 요약해서 보여주고, 어디로 가서 무엇을 하면 되는지 안내만 한다.
    print()
    print("요약")
    print(f"  계산 지표 수: {len(target_ids)}개")
    print(f"  검증 판정: {validation_result['전체판정']} (차단 {validation_result['차단수']}건, 경고 {validation_result['경고수']}건)")
    print(f"  미작성 장 수: {len(report_result['미작성_장'])}개")
    print(f"  run 폴더: {run_dir}")
    print()
    print("이메일 초안이 준비되었습니다. 발송 확정은 화면에서 진행하세요:")
    print(f"  streamlit run app.py  (run 폴더: {run_dir})")

    return 0  # 여기까지 왔다는 건 1~7단계가 전부 성공했다는 뜻 -> 정상 종료 코드


# 이 파일을 `python run_pipeline.py ...`로 직접 실행했을 때만 아래가 돈다
# (다른 파일이 `import run_pipeline`으로 가져다 쓸 때는 안 돈다).
if __name__ == "__main__":
    # main()의 반환값(0/1/2)을 그대로 프로세스 종료 코드로 넘긴다 — 셸
    # 스크립트나 CI가 이 코드로 성공/차단/오류를 구분할 수 있게 한다.
    sys.exit(main())
