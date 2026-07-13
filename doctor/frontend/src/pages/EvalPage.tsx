/**
 * EvalPage — 评测面板
 *
 * Phase 5 将实现：RunList + RunScoreboard（雷达图 + 表格）
 */
export default function EvalPage() {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="max-w-md text-center">
        <div className="mb-4 text-5xl">📈</div>
        <h2 className="mb-2 text-xl font-semibold text-foreground">
          评测面板
        </h2>
        <p className="text-sm text-muted-foreground">
          9 维分数雷达图 + Run 对比将在 Phase 5 实现。
        </p>
      </div>
    </div>
  );
}
