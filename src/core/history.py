from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ActionSnapshot:
    lines: tuple[str, ...]
    cursor_x: int
    cursor_y: int
    line_ending: str


@dataclass(slots=True, frozen=True)
class ActionRecord:
    label: str
    before: ActionSnapshot
    after: ActionSnapshot


@dataclass(slots=True)
class _HistoryNode:
    record: ActionRecord | None
    parent: int | None
    children: list[int]
    active_child: int = -1


class HistoryStack:
    __slots__ = ("_nodes", "_current", "_max_actions", "_next_id")

    def __init__(self, *, max_actions: int = 400) -> None:
        self._nodes: dict[int, _HistoryNode] = {}
        self._current = 0
        self._max_actions = max(20, int(max_actions))
        self._next_id = 1
        self.clear()

    def set_limit(self, value: int) -> None:
        self._max_actions = max(20, int(value))
        self._trim()

    def clear(self) -> None:
        self._nodes = {
            0: _HistoryNode(
                record=None,
                parent=None,
                children=[],
                active_child=-1,
            )
        }
        self._current = 0
        self._next_id = 1

    def push(self, record: ActionRecord) -> None:
        parent = self._nodes[self._current]
        node_id = self._next_id
        self._next_id += 1
        parent.children.append(node_id)
        parent.active_child = len(parent.children) - 1
        self._nodes[node_id] = _HistoryNode(
            record=record,
            parent=self._current,
            children=[],
            active_child=-1,
        )
        self._current = node_id
        self._trim()

    def undo(self) -> ActionRecord | None:
        if self._current == 0:
            return None
        node_id = self._current
        node = self._nodes.get(node_id)
        if node is None or node.record is None or node.parent is None:
            return None
        parent = self._nodes.get(node.parent)
        if parent is not None:
            try:
                parent.active_child = parent.children.index(node_id)
            except ValueError:
                pass
        self._current = node.parent
        return node.record

    def redo(self) -> ActionRecord | None:
        node = self._nodes.get(self._current)
        if node is None or not node.children:
            return None
        index = node.active_child
        if index < 0 or index >= len(node.children):
            index = len(node.children) - 1
        node.active_child = index
        child_id = node.children[index]
        child = self._nodes.get(child_id)
        if child is None or child.record is None:
            return None
        self._current = child_id
        return child.record

    def branch_prev(self) -> ActionRecord | None:
        return self._switch_sibling(-1)

    def branch_next(self) -> ActionRecord | None:
        return self._switch_sibling(1)

    def stats(self) -> tuple[int, int]:
        return self._record_count(), self._depth(self._current)

    def _trim(self) -> None:
        while self._record_count() > self._max_actions:
            candidate = self._oldest_trim_candidate()
            if candidate is None:
                break
            self._remove_leaf(candidate)

    def _switch_sibling(self, step: int) -> ActionRecord | None:
        if self._current == 0:
            return None
        node = self._nodes.get(self._current)
        if node is None or node.parent is None:
            return None
        parent = self._nodes.get(node.parent)
        if parent is None or len(parent.children) <= 1:
            return None
        try:
            index = parent.children.index(self._current)
        except ValueError:
            return None
        target_index = (index + step) % len(parent.children)
        if target_index == index:
            return None
        target_id = parent.children[target_index]
        target = self._nodes.get(target_id)
        if target is None or target.record is None:
            return None
        parent.active_child = target_index
        self._current = target_id
        return target.record

    def _record_count(self) -> int:
        return max(0, len(self._nodes) - 1)

    def _depth(self, node_id: int) -> int:
        depth = 0
        current = node_id
        while current != 0:
            node = self._nodes.get(current)
            if node is None or node.parent is None:
                break
            depth += 1
            current = node.parent
        return depth

    def _ancestor_ids(self, node_id: int) -> set[int]:
        ancestors: set[int] = set()
        current = node_id
        while True:
            ancestors.add(current)
            if current == 0:
                break
            node = self._nodes.get(current)
            if node is None or node.parent is None:
                break
            current = node.parent
        return ancestors

    def _oldest_trim_candidate(self) -> int | None:
        protected = self._ancestor_ids(self._current)
        for node_id in sorted(self._nodes.keys()):
            if node_id == 0 or node_id in protected:
                continue
            node = self._nodes[node_id]
            if node.children:
                continue
            return node_id
        return None

    def _remove_leaf(self, node_id: int) -> None:
        node = self._nodes.get(node_id)
        if node is None or node.parent is None or node.children:
            return
        parent = self._nodes.get(node.parent)
        if parent is not None:
            try:
                index = parent.children.index(node_id)
            except ValueError:
                index = -1
            if index >= 0:
                parent.children.pop(index)
                if parent.active_child >= len(parent.children):
                    parent.active_child = len(parent.children) - 1
                elif index <= parent.active_child:
                    parent.active_child -= 1
        self._nodes.pop(node_id, None)
