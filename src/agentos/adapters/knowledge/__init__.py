"""知识后端适配器：内置（`builtin`）与外部（`weknora`）实现同一个 KnowledgePort。

业务调用者（任务里的知识工具、合同测试）只依赖 kernel.ports.KnowledgePort；
切换后端只改组合根与配置，不改 kernel、runtime 或业务调用者。
"""

from agentos.adapters.knowledge.builtin import BuiltinKnowledgeAdapter, backend_from_settings

__all__ = ["BuiltinKnowledgeAdapter", "backend_from_settings"]
