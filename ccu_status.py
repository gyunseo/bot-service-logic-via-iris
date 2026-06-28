#!/usr/bin/env python3
"""삼성서울병원 심장내과중환자실 실시간 가용병상 조회.

국립중앙의료원 '응급실 실시간 가용병상정보 조회' 오픈API 사용.
필드 매핑은 공식 활용가이드(V4) 기준:
  - hv34  : [중환자실] 심장내과  (현재 가용)
  - hvs15 : [중환자실] 심장내과 _ 기준 (정원/총 병상)

사용법:
  export SERVICE_KEY='발급받은_디코딩키'
  python3 ccu_status.py
"""
import os
import sys
import xml.etree.ElementTree as ET

import requests

API_URL = "http://apis.data.go.kr/B552657/ErmctInfoInqireService/getEmrrmRltmUsefulSckbdInfoInqire"

# 조회 대상
STAGE1 = "서울특별시"
STAGE2 = "강남구"
TARGET_HPID = "A1100010"        # 삼성서울병원
TARGET_NAME = "삼성서울병원"

# 심장내과중환자실 필드 (공식 가이드 V4)
FIELD_AVAIL = "hv34"            # 가용
FIELD_TOTAL = "hvs15"          # 기준(정원)


def fetch_items(service_key: str) -> list[ET.Element]:
    """API 호출 후 <item> 목록 반환."""
    params = {
        "serviceKey": service_key,
        "STAGE1": STAGE1,
        "STAGE2": STAGE2,
        "pageNo": "1",
        "numOfRows": "50",
    }
    resp = requests.get(API_URL, params=params, timeout=10)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)

    # 게이트웨이/서비스 에러 체크 (resultCode 00 이 정상)
    code = root.findtext(".//resultCode")
    msg = root.findtext(".//resultMsg")
    if code is not None and code != "00":
        raise RuntimeError(f"API 오류 [{code}] {msg}")

    return root.findall(".//item")


def get_int(item: ET.Element, tag: str):
    """필드를 정수로. 없거나 비면 None."""
    text = item.findtext(tag)
    if text is None or text.strip() == "":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def fmt_idate(raw: str | None) -> str:
    """hvidate(YYYYMMDDhhmmss)를 읽기 좋게."""
    if not raw or len(raw) < 14:
        return raw or "-"
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"


def build_report(service_key: str) -> str:
    """삼성서울병원 심장내과중환자실 가용병상 현황을 사람이 읽을 문자열로 반환.

    네트워크/응답 오류는 RuntimeError로 올린다. 대상 병원을 못 찾으면 LookupError.
    """
    items = fetch_items(service_key)

    target = next((it for it in items if it.findtext("hpid") == TARGET_HPID), None)
    if target is None:
        raise LookupError(f"{TARGET_NAME}({TARGET_HPID}) 을(를) 응답에서 찾지 못했습니다.")

    avail = get_int(target, FIELD_AVAIL)   # hv34
    total = get_int(target, FIELD_TOTAL)   # hvs15
    updated = fmt_idate(target.findtext("hvidate"))
    tel = target.findtext("dutyTel3") or "-"

    # 상태 판정 (음수 = 정원 초과)
    if avail is None:
        status = "정보없음"
    elif avail > 0:
        status = "가용"
    elif avail == 0:
        status = "만실"
    else:
        status = f"포화(초과 {abs(avail)})"

    total_str = total if total is not None else "?"
    avail_str = avail if avail is not None else "-"

    return (
        f"[{TARGET_NAME}] 심장내과중환자실\n"
        f"  가용/정원 : {avail_str} / {total_str} 병상   → {status}\n"
        f"  갱신시각  : {updated}\n"
        f"  응급실전화 : {tel}"
    )


def main() -> int:
    service_key = os.environ.get("SERVICE_KEY")
    if not service_key:
        print("환경변수 SERVICE_KEY 가 설정되지 않았습니다.", file=sys.stderr)
        print("  export SERVICE_KEY='발급받은_디코딩키'", file=sys.stderr)
        return 1

    try:
        report = build_report(service_key)
    except requests.RequestException as e:
        print(f"네트워크 오류: {e}", file=sys.stderr)
        return 2
    except (ET.ParseError, RuntimeError) as e:
        print(f"응답 처리 오류: {e}", file=sys.stderr)
        return 3
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 4

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
