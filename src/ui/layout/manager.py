from __future__ import annotations

from dataclasses import dataclass, field
import time

from ...core.display import pad_to_display, slice_by_display
from .component import LayoutContext
from .feature_registry import FeatureRegistry


@dataclass(slots=True, frozen=True)
class LayoutPlan:
    tabline_height: int
    winbar_height: int
    statusline_height: int
    commandline_height: int
    editor_top: int
    editor_height: int


@dataclass(slots=True)
class _Notification:
    message: str
    expires_at: float


class NotificationCenter:
    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: list[_Notification] = []

    def push(self, message: str, *, ttl_seconds: float = 2.0) -> None:
        clean = message.strip()
        if not clean:
            return
        self._items.append(_Notification(message=clean, expires_at=time.monotonic() + max(0.1, ttl_seconds)))
        if len(self._items) > 10:
            self._items = self._items[-10:]

    def active(self) -> list[str]:
        now = time.monotonic()
        self._items = [item for item in self._items if item.expires_at > now]
        return [item.message for item in self._items]


class LayoutManager:
    __slots__ = ("_registry", "_last_regions")

    def __init__(self, registry: FeatureRegistry) -> None:
        self._registry = registry
        self._last_regions: dict[str, str] = {}

    def plan(self, width: int, height: int) -> LayoutPlan:
        components = self._registry.enabled_components()
        tabline_height = 1 if "tabline" in components else 0
        winbar_height = 1 if "winbar" in components else 0
        statusline_height = 1
        commandline_height = 1

        editor_top = tabline_height + winbar_height
        editor_height = max(1, height - editor_top - statusline_height - commandline_height)
        return LayoutPlan(
            tabline_height=tabline_height,
            winbar_height=winbar_height,
            statusline_height=statusline_height,
            commandline_height=commandline_height,
            editor_top=editor_top,
            editor_height=editor_height,
        )

    def render_tabline(self, context: LayoutContext, tabs: list[str], current_index: int, *, separator: str = " │ ") -> str:
        if not tabs:
            line = " [No Buffers] "
            return pad_to_display(slice_by_display(line, 0, context.width), context.width)
        parts: list[str] = []
        for index, item in enumerate(tabs):
            label = f"[{item}]" if index == current_index else item
            parts.append(label)
        text = separator.join(parts)
        return pad_to_display(slice_by_display(text, 0, context.width), context.width)

    def render_winbar(self, context: LayoutContext, breadcrumb: str) -> str:
        text = breadcrumb or context.file_name or "[No Name]"
        return pad_to_display(slice_by_display(text, 0, context.width), context.width)

    def render_statusline(self, context: LayoutContext) -> str:
        left = " | ".join(self._registry.status_segments("left", context))
        center = " | ".join(self._registry.status_segments("center", context))
        right = " | ".join(self._registry.status_segments("right", context))

        if not left:
            left = f"{context.mode}"
        if not right:
            right = f"Ln {context.row}, Col {context.col}"

        if center:
            full = f" {left}   {center}   {right} "
        else:
            full = f" {left}   {right} "
        return pad_to_display(slice_by_display(full, 0, context.width), context.width)

    def changed_region(self, name: str, content: str) -> bool:
        previous = self._last_regions.get(name)
        if previous == content:
            return False
        self._last_regions[name] = content
        return True
