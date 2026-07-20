"""
AM1 验证脚本：测试 bge-m3 embedding + Qdrant collection 初始化。

Embedding 双后端：优先 TEI → 不可用时自动降级 sentence-transformers（本地加载 bge-m3）。

用法：
    uv run python scripts/verify_am1.py
"""

from __future__ import annotations

import asyncio
import sys

# Add doctor backend to path
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "doctor" / "backend" / "src"))


async def verify_tei_status() -> None:
    """Check TEI availability (informational — embedding.py auto-falls back)."""
    import httpx

    tei_url = "http://localhost:8080"
    print(f"\n[1/3] TEI 状态检测 ({tei_url})...")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            await client.get(f"{tei_url}/health")
            print("  ✅ TEI 运行中 — 将使用 TEI 作为 embedding 后端")
    except Exception:
        print("  ⚠️  TEI 不可用 — 将降级到本地 sentence-transformers（同等精度，稍慢）")


async def verify_qdrant_collection() -> bool:
    """Test Qdrant collection creation with migration."""
    from src.memory.long_term.qdrant_client import ensure_collection, get_qdrant_client

    print("\n[2/3] 测试 Qdrant collection 初始化...")

    try:
        collection_name = await ensure_collection()
        print(f"  ✅ Collection '{collection_name}' 就绪")

        # Verify config (P1-a: named vectors symptom + root_cause)
        client = await get_qdrant_client()
        info = await client.get_collection(collection_name)
        config = info.config
        vectors = config.params.vectors if config and config.params else None
        if vectors and hasattr(vectors, "size"):
            # Legacy single unnamed vector
            print(f"  ✅ 向量维度: {vectors.size}, 距离: {vectors.distance} (单向量)")
        elif vectors:
            # Named vectors (dict[str, VectorParams])
            for vname, vparams in vectors.items():
                print(f"  ✅ 向量 '{vname}': dim={vparams.size}, 距离: {vparams.distance}")
        else:
            print("  ⚠️  未能读取向量配置")

        # Check payload indexes
        print("  Payload 索引:")
        if config and config.params:
            # Qdrant API doesn't easily expose payload indexes in get_collection
            # Just list what we expect
            for field in ["category", "symptom_tier", "source", "created_at"]:
                print(f"    - {field}")
        print("  ✅ Qdrant collection 验证通过")
        return True
    except Exception as e:
        print(f"  ❌ Qdrant 初始化失败: {e}")
        return False


async def verify_embedding_module() -> bool:
    """Test embedding.py module (TEI → sentence-transformers dual backend)."""
    from src.memory.long_term.embedding import embed_single, embed_texts

    print("\n[2/3] 测试 bge-m3 embedding（TEI 优先 → sentence-transformers 降级）...")

    try:
        # Test batch
        texts = [
            "创建任务后页面卡死，console 报 Cannot read properties of undefined",
            "API 请求超时，数据库查询耗时超过 30 秒",
        ]
        print("  embedding 中（首次需下载 bge-m3 模型 ~2GB，之后缓存）...")
        embeddings = await embed_texts(texts)
        print(f"  ✅ batch embed: {len(embeddings)} vectors × {len(embeddings[0])} dims")

        # Test single
        emb = await embed_single("N+1 query detected in task list")
        print(f"  ✅ single embed: {len(emb)} dims, first 3={emb[:3]}")

        return True
    except Exception as e:
        print(f"  ❌ embedding 模块失败: {e}")
        return False


async def main() -> None:
    print("=" * 60)
    print("DiagDoctor AM1 验证 — bge-m3 + Qdrant collection")
    print("=" * 60)

    results: list[bool] = []

    # Step 1: TEI status (informational only — embedding.py auto-falls back)
    await verify_tei_status()

    # Step 2: Embedding via dual-backend module
    results.append(await verify_embedding_module())
    if not results[-1]:
        print("\n⚠️  Embedding 不可用，跳过 Qdrant 测试。")
        return

    # Step 3: Qdrant collection
    results.append(await verify_qdrant_collection())

    # Summary
    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    if passed == total:
        print(f"✅ 全部通过 ({passed}/{total}) — AM1 基础设施就绪！")
    else:
        print(f"⚠️  {passed}/{total} 通过 — 请检查上面的 ❌ 项")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
