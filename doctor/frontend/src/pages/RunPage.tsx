import { useParams } from "react-router-dom";

/**
 * RunPage — 单次评测 Run 详情
 *
 * Phase 5 将实现：RunScoreboard（雷达图 + 每 case 分数表格）
 */
export default function RunPage() {
  const { name } = useParams<{ name: string }>();

  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="max-w-md text-center">
        <div className="mb-4 text-5xl">🏃</div>
        <h2 className="mb-2 text-xl font-semibold text-foreground">
          Run: {name}
        </h2>
        <p className="text-sm text-muted-foreground">
          雷达图 + 分数表格将在 Phase 5 实现。
        </p>
      </div>
    </div>
  );
}
