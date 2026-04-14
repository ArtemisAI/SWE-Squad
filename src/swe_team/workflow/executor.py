"""Workflow graph executor."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from src.swe_team.workflow.models import WorkflowDefinition


NodeHandler = Callable[[Dict[str, Any], Dict[str, Any]], Any]
HookHandler = Callable[[str, Dict[str, Any]], None]


@dataclass
class NodeExecution:
    node_id: str
    node_type: str
    success: bool
    output: int
    attempts: int = 1
    error: Optional[str] = None


@dataclass
class PipelineExecutionResult:
    success: bool
    visited_nodes: List[str] = field(default_factory=list)
    node_results: List[NodeExecution] = field(default_factory=list)


class PipelineExecutor:
    """Execute workflow definitions as a directed graph."""

    def __init__(
        self,
        workflow: WorkflowDefinition,
        handlers: Optional[Dict[str, NodeHandler]] = None,
        hooks: Optional[Dict[str, HookHandler]] = None,
    ) -> None:
        self.workflow = workflow
        self.handlers = handlers or {}
        self.hooks = hooks or {}
        self._nodes = {n.id: n for n in workflow.nodes}
        self._edges_by_source: Dict[str, List[Any]] = defaultdict(list)
        self._in_degree: Dict[str, int] = {n.id: 0 for n in workflow.nodes}
        for conn in workflow.connections:
            self._edges_by_source[conn.source_node_id].append(conn)
            if conn.target_node_id in self._in_degree:
                self._in_degree[conn.target_node_id] += 1

    def topological_order(self) -> List[str]:
        """Return a stable topological ordering for acyclic parts of the graph."""
        in_degree = dict(self._in_degree)
        queue: Deque[str] = deque(sorted([n for n, degree in in_degree.items() if degree == 0]))
        ordered: List[str] = []
        while queue:
            node_id = queue.popleft()
            ordered.append(node_id)
            for edge in self._edges_by_source.get(node_id, []):
                if edge.target_node_id not in in_degree:
                    continue
                in_degree[edge.target_node_id] -= 1
                if in_degree[edge.target_node_id] == 0:
                    queue.append(edge.target_node_id)
        # Preserve deterministic behavior for cyclic leftovers.
        if len(ordered) < len(self._nodes):
            leftovers = sorted([node_id for node_id in self._nodes if node_id not in set(ordered)])
            ordered.extend(leftovers)
        return ordered

    def execute(self, context: Optional[Dict[str, Any]] = None, max_steps: int = 1000) -> PipelineExecutionResult:
        """Walk the workflow graph from entry nodes and execute handlers."""
        ctx: Dict[str, Any] = dict(context or {})
        result = PipelineExecutionResult(success=True)
        entry_nodes = [n for n, degree in self._in_degree.items() if degree == 0]
        if not entry_nodes:
            return result

        queue: Deque[Tuple[str, int]] = deque((node_id, 1) for node_id in sorted(entry_nodes))
        visited: Set[str] = set()
        steps = 0

        while queue and steps < max_steps:
            node_id, attempt = queue.popleft()
            steps += 1
            if node_id not in self._nodes:
                continue
            node = self._nodes[node_id]

            self._emit("on_node_start", node_id, {"attempt": attempt, "context": ctx})
            output = 0
            success = True
            error: Optional[str] = None

            try:
                node_result = self._run_node(node.type, node.parameters, ctx)
                success, output = self._interpret_result(node_result)
            except Exception as exc:
                success = False
                output = 1
                error = str(exc)

            if node.type == "subworkflow" and success:
                sub = node.parameters.get("workflow")
                if isinstance(sub, WorkflowDefinition):
                    sub_res = PipelineExecutor(sub, handlers=self.handlers, hooks=self.hooks).execute(ctx, max_steps=max_steps)
                    success = sub_res.success
                    output = 0 if success else 1

            if not success:
                max_retries = int(node.parameters.get("max_retries", 0))
                if attempt <= max_retries:
                    self._emit("on_retry", node_id, {"attempt": attempt, "context": ctx})
                    queue.appendleft((node_id, attempt + 1))
                    continue
                result.success = False
                self._emit("on_error", node_id, {"attempt": attempt, "error": error, "context": ctx})

            visited.add(node_id)
            result.visited_nodes.append(node_id)
            result.node_results.append(
                NodeExecution(
                    node_id=node_id,
                    node_type=node.type,
                    success=success,
                    output=output,
                    attempts=attempt,
                    error=error,
                )
            )
            self._emit("on_node_complete", node_id, {"success": success, "output": output, "context": ctx})

            for edge in self._edges_by_source.get(node_id, []):
                if int(edge.source_output) == output:
                    queue.append((edge.target_node_id, 1))

        if steps >= max_steps:
            result.success = False
        return result

    def _run_node(self, node_type: str, parameters: Dict[str, Any], context: Dict[str, Any]) -> Any:
        handler = self.handlers.get(node_type)
        if handler is None:
            return True
        return handler(parameters, context)

    @staticmethod
    def _interpret_result(handler_result: Any) -> Tuple[bool, int]:
        if isinstance(handler_result, bool):
            return handler_result, 0 if handler_result else 1
        if isinstance(handler_result, int):
            return handler_result == 0, handler_result
        if isinstance(handler_result, dict):
            if "output" in handler_result:
                output = int(handler_result.get("output", 0))
                return output == 0, output
            if "success" in handler_result:
                success = bool(handler_result.get("success"))
                return success, 0 if success else 1
        return True, 0

    def _emit(self, hook: str, node_id: str, payload: Dict[str, Any]) -> None:
        callback = self.hooks.get(hook)
        if callback:
            callback(node_id, payload)
