"""证据采集与标准化 — raw evidence → NormalizedEvidence。

包含：
- normalizer.py: 9步管线主入口
- denoiser.py: 噪声去除
- deduplicator.py: 去重
- correlator.py: 跨层关联
- signal_extractor.py: 黄金信号提取
- tier_aware.py: 前后端层级标记
- formatter.py: NormalizedEvidence → HumanMessage 格式化
"""

from src.evidence.normalizer import ingest

__all__ = ["ingest"]
