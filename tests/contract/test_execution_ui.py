"""Product evidence projection: no fabricated telemetry, missing route or executable HTML."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / 'frontend/js/execution.js'


def run_node(code: str) -> object:
    node = shutil.which('node')
    assert node
    result = subprocess.run([node, '--input-type=module', '-e',
                             'import {projectExecution, executionMetrics, inspector} from '
                             + json.dumps(MODULE.as_uri()) + ';\n'
                             + code],
                            check=True, capture_output=True, text=True, timeout=10)
    return json.loads(result.stdout)


def test_missing_evidence_is_not_success_or_zero_usage() -> None:
    result = run_node("""
      const d = {run:{goal:'test',state:'succeeded'},steps:[{id:'s1',tool_name:'image.generate',state:'completed'}]};
      const nodes = projectExecution(d, []);
      console.log(JSON.stringify({metrics:executionMetrics(d.run,d.steps,[],{}),tool:nodes[1]}));
    """)
    assert isinstance(result, dict)
    assert result['metrics']['tokens'] is None
    assert result['metrics']['cost'] == '未记录'
    fields = dict(result['tool']['fields'])
    assert fields['实际路由'] == '未记录节点派发'
    assert fields['策略裁定'] == '历史裁定未记录'
    assert result['tool']['args'] == {}


def test_real_event_order_metrics_and_untrusted_prompts() -> None:
    result = run_node("""
      const d = {run:{goal:'<script>bad</script>',state:'succeeded'},
        steps:[{id:'s1',tool_name:'image.generate',state:'completed'}]};
      const events = [
        {kind:'model.request',payload:{call_id:'m1',messages:[{role:'user',content:'<img src=x onerror=bad()> foo'}]}},
        {kind:'model.usage',payload:{call_id:'m1',input_tokens:10,output_tokens:20}},
        {kind:'tool.prepared',payload:{step_id:'s1',arguments:{prompt:'<script>bad</script>'},policy:'allowed'}},
        {kind:'node.dispatched',payload:{step_id:'s1',node_id:'win',os:'Windows',tool:'image.generate'}},
      ];
      const nodes = projectExecution(d,events);
      console.log(JSON.stringify({ids:nodes.map(n=>n.id),metrics:executionMetrics(d.run,d.steps,events,{settled:0}),html:inspector(nodes[1]),route:dictFields(nodes[2])}));
      function dictFields(n) {return Object.fromEntries(n.fields);}
    """)
    assert isinstance(result, dict)
    assert result['ids'] == ['goal', 'm1', 's1']
    assert result['metrics']['tokens'] == 30
    assert result['route']['实际路由'] == 'win · Windows'
    assert '<img src=x' not in result['html'] and '&lt;img' in result['html']
