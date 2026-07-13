import { useParams } from "react-router-dom";

/**
 * CasePage — 单个 Case 详情
 *
 * Phase 5 将实现：CaseDetailCompare（diagnosis vs expected_output 并排 + diff 视图）
 */
export default function CasePage() {
  const { id } = useParams<{ id: string }>();

  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="max-w-md text-center">
        <div className="mb-4 text-5xl">🔍</div>
        <h2 className="mb-2 text-xl font-semibold text-foreground">
          Case: {id}
        </h2>
        <p className="text-sm text-muted-foreground">
          Case 详情 + 诊断对比将在 Phase 5 实现。
        </p>
      </div>
    </div>
  );
}
