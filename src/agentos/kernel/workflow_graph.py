"""Versioned flow definitions; executable implementations are registered server-side."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, model_validator

from agentos.kernel.types import BoundaryModel


class FlowNode(BoundaryModel):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    kind: Literal['agent', 'tool', 'condition', 'wait', 'custom', 'template']
    label: str = ''
    depends_on: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    when: dict[str, bool] = Field(default_factory=dict)


class FlowGraph(BoundaryModel):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    name: str = ''
    nodes: list[FlowNode] = Field(min_length=1, max_length=100)
    input_schema: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_graph(self) -> FlowGraph:
        names = {n.id for n in self.nodes}
        if len(names) != len(self.nodes):
            raise ValueError('duplicate node IDs')
        done: set[str] = set()
        for node in self.nodes:
            if set(node.depends_on) - names or set(node.when) - set(node.depends_on):
                raise ValueError('unknown dependency or condition outside dependencies')
            if node.kind == 'wait' and not 0 <= int(node.config.get('seconds', 0)) <= 86400 * 30:
                raise ValueError('wait must be between zero and 30 days')
            if node.kind == 'tool' and not re.fullmatch(r'[a-zA-Z0-9_.:-]+', str(node.config.get('tool', ''))):
                raise ValueError('tool node requires a registered tool name')
            if node.kind == 'custom' and not re.fullmatch(r'[a-zA-Z0-9_-]+', str(node.config.get('adapter', ''))):
                raise ValueError('custom node requires a registered adapter name')
            if node.kind == 'template' and node.config.get('template') not in (
                    'material-report', 'activity-collaboration'):
                raise ValueError('unknown registered business template')
            if node.kind == 'condition' and node.config.get('operator') not in ('eq', 'ne', 'contains', 'exists'):
                raise ValueError('condition operator must be explicitly supported')
            def check_refs(value: Any, dependencies: list[str] = node.depends_on) -> None:
                if isinstance(value, dict) and set(value) == {'$ref'}:
                    path = value['$ref']
                    if not isinstance(path, str) or not re.fullmatch(
                            r'(inputs|nodes)(\.[a-zA-Z0-9_-]+)*', path):
                        raise ValueError('invalid input reference')
                    parts = path.split('.')
                    if parts[0] == 'nodes' and (len(parts) < 2 or parts[1] not in dependencies):
                        raise ValueError('node reference requires a declared dependency')
                elif isinstance(value, dict):
                    for item in value.values():
                        check_refs(item)
                elif isinstance(value, list):
                    for item in value:
                        check_refs(item)
            check_refs(node.config)
        while len(done) < len(names):
            ready = {n.id for n in self.nodes if n.id not in done and set(n.depends_on) <= done}
            if not ready:
                raise ValueError('workflow contains a dependency cycle')
            done |= ready
        return self

    def schedule(self) -> list[dict[str, Any]]:
        """Minimal Temporal history: node routing metadata only, inputs/config stay in AgentOS."""
        return [{'id': n.id, 'kind': n.kind, 'depends_on': n.depends_on, 'when': n.when,
                 'seconds': int(n.config.get('seconds', 0)) if n.kind == 'wait' else 0} for n in self.nodes]
