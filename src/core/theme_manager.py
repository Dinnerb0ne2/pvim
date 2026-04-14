from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any


@dataclass(slots=True, frozen=True)
class ThemeRecord:
    name: str
    path: Path
    version: str
    description: str
    preview: str
    source: str


def _sanitize_theme_name(value: str) -> str:
    cleaned = "".join(ch for ch in value.strip().lower() if ch.isalnum() or ch in {"-", "_", "."})
    cleaned = cleaned.strip("._-")
    return cleaned or "theme"


def _derive_name_from_file(path: Path) -> str:
    stem = path.stem
    if stem.startswith("pvim.theme."):
        stem = stem[len("pvim.theme.") :]
    return stem or path.stem


class ThemeManager:
    def __init__(self, *, builtin_dirs: list[Path], user_dir: Path) -> None:
        self._builtin_dirs = [item.resolve() for item in builtin_dirs]
        self._user_dir = user_dir.resolve()

    def list_themes(self) -> list[ThemeRecord]:
        merged: dict[str, ThemeRecord] = {}
        for directory in self._builtin_dirs:
            for record in self._scan_directory(directory, source="builtin"):
                merged.setdefault(record.name.lower(), record)
        for record in self._scan_directory(self._user_dir, source="user"):
            merged[record.name.lower()] = record
        return sorted(merged.values(), key=lambda item: item.name.lower())

    def resolve(self, name_or_path: str) -> Path | None:
        path = Path(name_or_path).expanduser()
        if path.exists() and path.is_file():
            return path.resolve()
        lookup = name_or_path.strip().lower()
        if not lookup:
            return None
        for record in self.list_themes():
            if record.name.lower() == lookup:
                return record.path
            if record.path.name.lower() == lookup:
                return record.path
            if record.path.stem.lower() == lookup:
                return record.path
        return None

    def install(self, source: Path | str) -> ThemeRecord:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists() or not source_path.is_file():
            raise ValueError(f"Theme source not found: {source_path}")
        payload = self._read_theme_payload(source_path)
        if payload is None:
            raise ValueError(f"Invalid theme JSON: {source_path}")

        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            payload["meta"] = meta
        declared_name = meta.get("name")
        display_name = str(declared_name).strip() if isinstance(declared_name, str) else _derive_name_from_file(source_path)
        slug = _sanitize_theme_name(display_name)
        target_name = source_path.name
        if not target_name.lower().endswith(".json"):
            target_name = f"pvim.theme.{slug}.json"
        elif not target_name.lower().startswith("pvim.theme."):
            target_name = f"pvim.theme.{slug}.json"

        self._user_dir.mkdir(parents=True, exist_ok=True)
        target_path = (self._user_dir / target_name).resolve()

        preview = meta.get("preview")
        if isinstance(preview, str) and preview.strip():
            source_preview = (source_path.parent / preview).resolve()
            if source_preview.exists() and source_preview.is_file():
                preview_dir = (self._user_dir / "previews").resolve()
                preview_dir.mkdir(parents=True, exist_ok=True)
                target_preview = (preview_dir / source_preview.name).resolve()
                shutil.copy2(source_preview, target_preview)
                meta["preview"] = str(Path("previews") / source_preview.name).replace("\\", "/")

        target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        record = self._record_from_file(target_path, source="user")
        if record is None:
            raise ValueError("Installed theme metadata is invalid.")
        return record

    def uninstall(self, name: str) -> str:
        clean = name.strip().lower()
        if not clean:
            raise ValueError("Theme name is empty.")
        for record in self._scan_directory(self._user_dir, source="user"):
            if record.name.lower() != clean and record.path.name.lower() != clean and record.path.stem.lower() != clean:
                continue
            record.path.unlink(missing_ok=True)
            return f"Uninstalled theme: {record.name}"
        raise ValueError(f"Theme not found in installed themes: {name}")

    def _scan_directory(self, directory: Path, *, source: str) -> list[ThemeRecord]:
        if not directory.exists() or not directory.is_dir():
            return []
        records: list[ThemeRecord] = []
        for path in sorted(directory.glob("pvim.theme*.json"), key=lambda item: item.name.lower()):
            record = self._record_from_file(path, source=source)
            if record is not None:
                records.append(record)
        return records

    def _record_from_file(self, path: Path, *, source: str) -> ThemeRecord | None:
        payload = self._read_theme_payload(path)
        if payload is None:
            return None
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        raw_name = meta.get("name")
        name = str(raw_name).strip() if isinstance(raw_name, str) and raw_name.strip() else _derive_name_from_file(path)
        raw_version = meta.get("version", "0.0.0")
        raw_description = meta.get("description", "")
        preview_ref = meta.get("preview", "")
        preview_path = ""
        if isinstance(preview_ref, str) and preview_ref.strip():
            preview_path = str((path.parent / preview_ref).resolve())
        return ThemeRecord(
            name=name,
            path=path.resolve(),
            version=str(raw_version).strip() or "0.0.0",
            description=str(raw_description).strip(),
            preview=preview_path,
            source=source,
        )

    def _read_theme_payload(self, path: Path) -> dict[str, Any] | None:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(loaded, dict):
            return None
        ui = loaded.get("ui")
        syntax = loaded.get("syntax")
        if not isinstance(ui, dict) or not isinstance(syntax, dict):
            return None
        return loaded
