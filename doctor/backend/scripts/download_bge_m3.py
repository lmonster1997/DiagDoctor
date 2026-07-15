"""
一次性脚本：用 ModelScope 下载 bge-m3 到 HuggingFace 缓存目录。
用法: $env:HF_HUB_CACHE="D:\hf_cache"; uv run --with modelscope python scripts/download_bge_m3.py
"""

import os
import shutil
from pathlib import Path

from modelscope import snapshot_download

MODEL = "BAAI/bge-m3"
# 尊重 HF_HUB_CACHE 环境变量，否则用默认路径
HF_BASE = Path(os.environ.get("HF_HUB_CACHE", Path.home() / ".cache" / "huggingface" / "hub"))
# 去掉 /hub 后缀（HF_HUB_CACHE 通常指向 .../huggingface/hub）


def main():
    print(f"从 ModelScope 下载 {MODEL}...")
    print(f"  缓存目录: {HF_BASE}")

    # 下载到临时目录
    local_dir = snapshot_download(MODEL, cache_dir="./_bge_m3_tmp")

    # 确定 HF 缓存目标路径
    target = HF_BASE / f"models--{MODEL.replace('/', '--')}"
    print(f"  → 移动到 HF 缓存: {target}")

    if target.exists():
        print(f"  ⚠️  目标已存在，如需重新下载请先删除: {target}")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(local_dir, str(target))
    shutil.rmtree("./_bge_m3_tmp", ignore_errors=True)

    print(f"  ✅ 完成！模型已就位: {target}")
    print(f"  验证前记得设: $env:HF_HUB_CACHE='{HF_BASE}'")
    print(f"  然后运行: cd doctor/backend && uv run python scripts/verify_am1.py")


if __name__ == "__main__":
    main()
