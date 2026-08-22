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

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# Windows 콘솔은 기본 인코딩이 cp949라, 로그 문구의 "—" 같은 기호를 만나면
# print()가 그대로 죽는다(실측: UnicodeEncodeError로 스크립트 자체가 죽고
# 그 결과 우연히 exit code 1이 나와 "검증 차단"처럼 보였다 — 실제로는 검증까지
# 가지도 못한 크래시였다). 표준출력을 UTF-8로 강제해 이 스크립트를 실행하는
# 모든 환경에서 같게 동작하게 한다.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

APP_DIR = Path(__file__).resolve().parent
CATALOG_DIR = APP_DIR / "catalog"
sys.path.insert(0, str(APP_DIR))

from pipeline import profile as profiler  # noqa: E402
from pipeline import calculate as calculator  # noqa: E402
from pipeline import compare as comparator  # noqa: E402
from pipeline import validate as validator  # noqa: E402
from pipeline import report as reporter  # noqa: E402
from pipeline import email_draft  # noqa: E402

AUTO_GENERATED_CHAPTERS = ["1", "3", "4", "7", "8"]  # report.py가 항상 자동으로 채우는 장(구조상 고정)
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _log(stage: str, summary: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] {stage}: {summary}")


def _read_csv_with_fallback(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="cp949")


def _load_catalogs():
    """카탈로그 3종을 읽는다. metrics/schema가 없으면 None — 이 함수 안에서
    곧바로 종료하지 않고 호출자(main)가 종료 코드를 정하게 한다."""
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
    parser = argparse.ArgumentParser(
        description="auto-report 1~7단계를 명령줄에서 실행한다. 8단계(발송 확정)는 화면에서만 한다.",
        epilog=(
            "예:\n"
            "  python run_pipeline.py --file 수업자료/usage_history_2025-01.csv\n"
            "  python run_pipeline.py --file xxx.csv --approve-extension\n"
            "  python run_pipeline.py --file xxx.csv --month 2025-01"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", required=True, help="업로드할 CSV 경로 (필수)")
    parser.add_argument("--month", default=None, help="YYYY-MM. 생략하면 파일에서 기간을 자동 판정한다.")
    parser.add_argument(
        "--approve-extension", action="store_true",
        help="유효구간 확장을 승인한다. 없으면 확장이 필요할 때 그 자리에서 중단한다.",
    )
    parser.add_argument("--output-dir", default=None, help="run_* 폴더를 만들 위치. 기본값: auto-report/outputs")
    parser.add_argument(
        "--skip-pdf", action="store_true",
        help="PDF 생성을 생략한다(마크다운 리포트만 만든다).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.month and not MONTH_RE.match(args.month):
        _log("입력 확인", f"중단 — --month는 YYYY-MM 형식이어야 합니다: {args.month!r}")
        return 2

    csv_path = Path(args.file)
    if not csv_path.exists():
        _log("1단계 파일 읽기", f"중단 — 파일을 찾을 수 없습니다: {csv_path}")
        return 2

    catalogs = _load_catalogs()
    if catalogs is None:
        _log("0단계 카탈로그", "중단 — catalog/*.json이 없습니다. python catalog/export_catalog.py를 먼저 실행하세요.")
        return 2
    metrics_catalog, schema_catalog, insights_catalog = catalogs

    # --- 1단계: 파일 읽기 ---
    try:
        df = _read_csv_with_fallback(csv_path)
    except Exception as e:  # noqa: BLE001 - 원인을 그대로 보여준다
        _log("1단계 파일 읽기", f"중단 — CSV를 읽을 수 없습니다(utf-8-sig, cp949 둘 다 실패): {e}")
        return 2
    _log("1단계 파일 읽기", f"{csv_path.name} — {len(df):,}행, {len(df.columns)}컬럼")

    # --- 2단계: 판정 ---
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

    table_info = schema_catalog.get(table_name, {})
    profile_result = profiler.profile_data(df, table_info)

    if args.month:
        period = {"최소": args.month, "최대": args.month}
    else:
        primary_period = next(iter(profile_result["기간컬럼"].values()), None)
        if primary_period is None:
            _log("2단계 판정", "중단 — 기간을 자동으로 판정할 수 없습니다. --month로 직접 지정하세요.")
            return 2
        period = primary_period
    기간_str = f"{period['최소']} ~ {period['최대']}"

    metric_judgments = profiler.judge_metrics(table_name, period, metrics_catalog)
    계산대상 = [m for m in metric_judgments if m["상태"] in ("계산가능", "유효구간 확장 필요")]
    확장필요_목록 = [m for m in metric_judgments if m["상태"] == "유효구간 확장 필요"]
    부분갱신_목록 = [m for m in metric_judgments if m["부분갱신여부"]]

    if 확장필요_목록 and not args.approve_extension:
        _log("2단계 판정", f"중단 — 유효구간 확장이 필요한 지표 {len(확장필요_목록)}종, --approve-extension 없음")
        for m in 확장필요_목록:
            print(f"  - {m['metric_id']} ({m['지표명']}): {m['이유']}")
        return 2

    target_ids = [m["metric_id"] for m in 계산대상]
    _log(
        "2단계 판정",
        f"계산 대상 {len(target_ids)}종 "
        f"(유효구간 확장 {len(확장필요_목록)}종, 부분갱신 {len(부분갱신_목록)}종)",
    )

    # --- 2단계: 계산 (BigQuery 스테이징 적재 — DDL만, DML 없음) ---
    try:
        client = calculator.get_client()
        staging_table = calculator.load_staging_table(df, table_name, client)
        results, sql_log = calculator.compute_target_metrics(
            target_ids, table_name, staging_table, period["최소"], period["최대"],
            metrics_catalog, client, args.approve_extension,
        )
    except calculator.AuthError as e:
        _log("2단계 계산", f"중단 — BigQuery 인증 실패: {e}")
        print("아래 명령으로 인증한 뒤 다시 실행하세요: gcloud auth application-default login")
        return 2
    _log("2단계 계산", f"{len(results)}개 지표 계산 완료 (스테이징 테이블: {staging_table})")

    # --- run 폴더 생성 (계산 성공 후에만 — 실패 흔적을 outputs/에 남기지 않는다) ---
    output_root = Path(args.output_dir) if args.output_dir else (APP_DIR / "outputs")
    run_time = datetime.now()
    run_dir = output_root / run_time.strftime("run_%Y%m%d_%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / csv_path.name).write_bytes(csv_path.read_bytes())

    catalog_generated_at = metrics_catalog.get("_meta", {}).get("생성일시", "알 수 없음")

    run_log = {
        # app.py의 run_log.json과 같은 평평한 필드 — 같은 run_context 형태를
        # report.py·email_draft.py에 그대로 넘기기 위함이다.
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

        "0_카탈로그": {
            "생성일시": catalog_generated_at,
            "지표_개수": metrics_catalog.get("_meta", {}).get("항목_개수", "?"),
            "테이블_개수": schema_catalog.get("_meta", {}).get("항목_개수", "?"),
            "인사이트_개수": insights_catalog.get("_meta", {}).get("항목_개수", "?"),
        },
        "1_투입": {
            "파일명": csv_path.name,
            "크기_바이트": csv_path.stat().st_size,
            "행수": profile_result["행수"],
            "컬럼_수": profile_result["컬럼수"],
            "업로드_시각": run_time.isoformat(),
        },
        "2_판정": {
            "테이블": table_name,
            "일치율": table_judgment["일치율"],
            "기간": profile_result["기간컬럼"],
            "결측": profile_result["결측"],
            "그레인_후보": profile_result["그레인_후보"],
        },
        "2_계산": {
            "지표별_값_상태": {r.metric_id: {"값": r.value, "상태": r.status} for r in results},
            "스테이징_테이블명": staging_table,
            "계산_시각": run_time.isoformat(),
        },
        "게이트1": {
            "확정_시각": run_time.isoformat(),
            "유효구간_확장_승인_여부": bool(확장필요_목록) and args.approve_extension,
            "승인_방식": "CLI --approve-extension" if args.approve_extension else "해당 없음",
        },
        "4_대시보드": {"생략": True, "이유": "화면 전용 — CLI에는 없음"},
        "4_5_확인": {"생략": True, "이유": "화면에서 사람이 확인하는 단계 — CLI에는 없음"},
        # --approve-extension을 실제로 썼는지는 여기 명시적으로 남긴다
        # (게이트1.승인_방식과 겹치지만, 이 섹션은 "CLI를 어떻게 불렀는가"
        # 자체를 남기는 자리라 실행 명령 전체도 함께 둔다).
        "CLI_실행": {
            "명령": " ".join(sys.argv),
            "approve_extension_사용": args.approve_extension,
            "month_직접지정": args.month,
        },
    }
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    calculator.save_metrics_csv(results, run_dir)
    _log("2단계 계산", f"run 폴더 생성: {run_dir}")

    # --- 3단계: 검증 ---
    prev_period = comparator.previous_month(period["최소"])
    current_metric_df = comparator.metric_results_to_df(results)
    prev_df = comparator.calc_previous(target_ids, prev_period, client)
    comparison_df = comparator.compare(current_metric_df, prev_df, metrics_catalog)
    comparison_df.to_csv(run_dir / "comparison.csv", index=False, encoding="utf-8-sig")

    metrics_df = pd.DataFrame([{
        "metric_id": r.metric_id, "지표명": r.지표명, "유형": r.유형, "month": r.month,
        "value": r.value, "sample_size": r.sample_size, "min_sample": r.min_sample,
        "status": r.status, "부분갱신": r.부분갱신, "원천": "+".join(r.원천), "error": r.error or "",
    } for r in results])
    table_override = {table_name: staging_table}
    uploaded_months = {calculator.parse_year_month(period["최소"])}
    validation_result = validator.validate_all(
        metrics_df, comparison_df, metrics_catalog, client,
        override=args.approve_extension, table_override=table_override,
        uploaded_months=uploaded_months,
    )
    (run_dir / "validation.json").write_text(
        json.dumps(validation_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    run_log["3_검증"] = validation_result
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    _log(
        "3단계 검증",
        f"{validation_result['전체판정']} (차단 {validation_result['차단수']}건, 경고 {validation_result['경고수']}건)",
    )

    if validation_result["전체판정"] == "차단":
        print("검증 차단 항목:")
        for f in validation_result["항목별_결과"]:
            if f["판정"] == "차단":
                print(f"  - [{f['검증명']}] {f['대상지표']}: {f['상세']}")
        print()
        print(f"run 폴더: {run_dir}")
        return 1

    # --- 4단계(대시보드) — 화면 전용이라 생략 ---

    # --- 6단계: 리포트 생성 (manual/sections.md 병합 포함) ---
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

    report_result = reporter.build_report(run_context)
    (run_dir / "report.md").write_text(report_result["report_md"], encoding="utf-8")
    run_log["6_리포트"] = {
        "생성_시각": datetime.now().isoformat(),
        "자동생성_장_목록": AUTO_GENERATED_CHAPTERS,
        "미작성_장_목록": report_result["미작성_장"],
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
        try:
            pdf_bytes = reporter.build_pdf(report_result["report_md"])
            (run_dir / "report.pdf").write_bytes(pdf_bytes)
            pdf_생성됨 = True
            _log("6단계 리포트", "PDF 생성 완료(report.pdf)")
        except Exception as e:  # noqa: BLE001
            pdf_오류 = str(e)
            _log("6단계 리포트", f"PDF 생성 실패(마크다운은 유지) — {pdf_오류}")

    run_log["6_리포트"]["PDF_생성됨"] = pdf_생성됨
    run_log["6_리포트"]["PDF_오류"] = pdf_오류
    (run_dir / "run_log.json").write_text(
        json.dumps(run_log, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # --- 7단계: 이메일 초안 생성 ---
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
    print()
    print("요약")
    print(f"  계산 지표 수: {len(target_ids)}개")
    print(f"  검증 판정: {validation_result['전체판정']} (차단 {validation_result['차단수']}건, 경고 {validation_result['경고수']}건)")
    print(f"  미작성 장 수: {len(report_result['미작성_장'])}개")
    print(f"  run 폴더: {run_dir}")
    print()
    print("이메일 초안이 준비되었습니다. 발송 확정은 화면에서 진행하세요:")
    print(f"  streamlit run app.py  (run 폴더: {run_dir})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
