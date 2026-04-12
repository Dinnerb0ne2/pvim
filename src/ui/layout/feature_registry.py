from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .component import LayoutContext


StatusProvider = Callable[[LayoutContext], str]


@dataclass(slots=True)
class FeatureDescriptor:
    name: str
    enabled: bool
    ui_components: set[str]
    trigger: str
    setup_fn: Callable[[], None] | None = None


class FeatureRegistry:
    __slots__ = ("_features", "_status_left", "_status_center", "_status_right")

    def __init__(self) -> None:
        self._features: dict[str, FeatureDescriptor] = {}
        self._status_left: list[tuple[str, StatusProvider]] = []
        self._status_center: list[tuple[str, StatusProvider]] = []
        self._status_right: list[tuple[str, StatusProvider]] = []

    def register(self, descriptor: FeatureDescriptor) -> None:
        self._features[descriptor.name] = descriptor
        if descriptor.setup_fn is not None:
            descriptor.setup_fn()

    def set_enabled(self, name: str, enabled: bool) -> bool:
        item = self._features.get(name)
        if item is None:
            return False
        item.enabled = enabled
        return True

    def is_enabled(self, name: str) -> bool:
        item = self._features.get(name)
        return bool(item and item.enabled)

    def enabled_components(self) -> set[str]:
        result: set[str] = set()
        for descriptor in self._features.values():
            if descriptor.enabled:
                result.update(descriptor.ui_components)
        return result

    def register_status_segment(self, region: str, feature_name: str, provider: StatusProvider) -> None:
        target = self._status_target(region)
        target.append((feature_name, provider))

    def status_segments(self, region: str, context: LayoutContext) -> list[str]:
        target = self._status_target(region)
        segments: list[str] = []
        for feature_name, provider in target:
            if not self.is_enabled(feature_name):
                continue
            text = provider(context)
            if text:
                segments.append(text)
        return segments

    def list_features(self) -> list[FeatureDescriptor]:
        return sorted(self._features.values(), key=lambda item: item.name.lower())

    def _status_target(self, region: str) -> list[tuple[str, StatusProvider]]:
        match region:
            case "left":
                return self._status_left
            case "center":
                return self._status_center
            case "right":
                return self._status_right
            case _:
                raise ValueError(f"Unknown status region: {region}")
