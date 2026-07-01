"""一键派生 Polymarket CLOB API 凭证（换私钥即换即用，只打印不落盘）。

私钥来源（按优先级）：
  1) 命令行参数：  python get_api_creds.py --key 0xabc...
  2) 交互输入（默认，最安全，不回显/不进 shell 历史）：
                   python get_api_creds.py
  3) 回退读 .env： python get_api_creds.py --env

行为：用私钥签名调用 create_or_derive_api_creds 派生 L2 凭证，
     把 CLOB_API_KEY / SECRET / PASSPHRASE 打印出来供你手动粘贴。
     —— 纯签名，不发生任何链上交易、不花钱、不改任何文件。

换私钥时：重新跑一次、填新私钥即可，得到对应新钱包的凭证。
"""
from __future__ import annotations

import argparse
import getpass
import sys

from config import CONFIG   # 复用统一的 clob_host

CHAIN_ID_POLYGON = 137


def _read_key(args) -> str:
    """按优先级取私钥：命令行 > .env > 交互输入。"""
    if args.key:
        return args.key.strip()
    if args.env:
        from dotenv import dotenv_values
        return (dotenv_values(".env").get("POLYGON_PRIVATE_KEY") or "").strip()
    # 默认：安全交互输入，不回显、不落 shell 历史。
    return getpass.getpass("请粘贴钱包私钥（输入时不显示）: ").strip()


def _valid(pk: str) -> bool:
    return pk.startswith("0x") and len(pk) == 66


def main() -> int:
    ap = argparse.ArgumentParser(description="派生 Polymarket CLOB API 凭证")
    ap.add_argument("--key", help="直接传入私钥 0x...（注意会留在 shell 历史）")
    ap.add_argument("--env", action="store_true",
                    help="从 .env 的 POLYGON_PRIVATE_KEY 读取")
    args = ap.parse_args()

    pk = _read_key(args)
    if not pk:
        print("✗ 未拿到私钥。用 --key 传入、或 --env 从 .env 读、或直接交互输入。")
        return 1
    if not _valid(pk):
        print(f"✗ 私钥格式可疑（应 0x 开头、共 66 字符），当前长度 {len(pk)}。")
        return 1

    from py_clob_client.client import ClobClient

    tail = pk[-4:]
    print(f"→ 钱包私钥(…{tail}) 连接 {CONFIG.clob_host} (chain {CHAIN_ID_POLYGON})")

    client = ClobClient(CONFIG.clob_host, key=pk, chain_id=CHAIN_ID_POLYGON)
    # 已存在则派生同一套，不存在则创建；确定性、可重复。
    creds = client.create_or_derive_api_creds()

    print("\n✓ 派生成功。对应这三项（自行决定是否粘进 .env）：\n")
    print(f"CLOB_API_KEY={creds.api_key}")
    print(f"CLOB_API_SECRET={creds.api_secret}")
    print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        # 常见失败：钱包从未在链上激活 / 网络不通 / 私钥无效
        print(f"✗ 派生失败：{exc}")
        print("  排查：1) 钱包是否已在 Polygon 上激活（至少一次交互/充值）；"
              "2) 能否访问 clob.polymarket.com；3) 私钥是否正确。")
        sys.exit(1)
