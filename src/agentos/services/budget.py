"""预算服务（FR-GOV-04）：预留→调用→按 usage 结算；超限熔断下一次派发。

并行任务共享预留账本：先预留再调用，避免并行子任务各自检查都通过。
在途请求可能产生少量结算差额（记录为审计事件，不追溯熔断已完成的调用）。
"""

from __future__ import annotations

from agentos.kernel.errors import AgentOSError, ErrorCode
from agentos.kernel.store import StoreProtocol


class BudgetService:
    def __init__(self, store: StoreProtocol) -> None:
        self._store = store

    def reserve(self, tenant_id: str, run_id: str, call_id: str, amount_microusd: int) -> None:
        ok = self._store.budget_reserve(tenant_id, run_id, call_id, amount_microusd)
        if not ok:
            totals = self._store.budget_totals(tenant_id, run_id)
            raise AgentOSError(
                ErrorCode.BUDGET_EXCEEDED,
                "budget exceeded: reservation denied",
                detail={"reserved": totals.get("reserved", 0), "budget": totals.get("budget", 0)},
            )

    def settle(self, tenant_id: str, call_id: str, actual_microusd: int) -> None:
        self._store.budget_settle(tenant_id, call_id, actual_microusd)

    def release(self, tenant_id: str, call_id: str) -> None:
        self._store.budget_release(tenant_id, call_id)

    def totals(self, tenant_id: str, run_id: str) -> dict[str, int]:
        return self._store.budget_totals(tenant_id, run_id)
