#!/usr/bin/env python3
"""经香港代理采集 UTC 昨日市场，完成后生成英文海报、数据 JSON 和推文。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polymarket_updown_daily as daily
import polymarket_updown_poster as poster

HK_PROXY = "socks5h://127.0.0.1:20810"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default=HK_PROXY, help=f"香港代理 URL，默认 {HK_PROXY}")
    parser.add_argument("--db", type=Path, default=daily.core.DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=daily.core.ROOT / "outputs/posters")
    args = parser.parse_args(argv)

    # 固定本次日期，采集跨 UTC 午夜时仍为同一天生成海报。
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    print(f"目标市场结束日：{yesterday}（UTC）；采集完成后生成海报和推文。", flush=True)
    try:
        result = daily.main([
            "collect", "--start-date", yesterday.isoformat(), "--end-date", today.isoformat(),
            "--proxy", args.proxy, "--db", str(args.db),
        ])
        if result != 0:
            print(f"采集未成功完成（退出码 {result}），本次不生成海报和推文。", file=sys.stderr)
            return result
        return poster.main([
            "--date", yesterday.isoformat(), "--db", str(args.db),
            "--output-dir", str(args.output_dir),
        ])
    except KeyboardInterrupt:
        print("流程已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
