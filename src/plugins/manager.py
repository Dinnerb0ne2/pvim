from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, Callable

from ..scripting import Environment, Parser, ScriptError, ScriptInterpreter, ScriptRuntimeError

PLUGIN_EXTENSIONS = {".pvi", ".pvs"}


@dataclass(slots=True)
class PluginSpec:
    name: str
    entry_path: Path


@dataclass(slots=True)
class PluginRuntime:
    name: str
    entry_path: Path
    interpreter: ScriptInterpreter
    env: Environment


class PluginManager:
    def __init__(
        self,
        *,
        plugins_root: Path,
        enabled: bool,
        step_limit: int,
        auto_load: bool,
        host_api: Callable[[int, str, list[Any]], Any] | None,
    ) -> None:
        self.enabled = enabled
        self.plugins_root = plugins_root.resolve()
        self.step_limit = max(1000, int(step_limit))
        self.auto_load = auto_load
        self._host_api = host_api

        self._next_object_id = 1
        self._objects: dict[int, Any] = {}
        self._pvim_id = self._register_object("pvim-facade")

        self._specs: dict[str, PluginSpec] = {}
        self._runtimes: dict[str, PluginRuntime] = {}
        self._errors: dict[str, str] = {}
        self._ast_cache: dict[Path, tuple[int, Any]] = {}

        if self.enabled:
            self.plugins_root.mkdir(parents=True, exist_ok=True)
            self.discover()
            if self.auto_load:
                self.load_all()

    def discover(self) -> None:
        self._specs = {}
        if not self.enabled:
            return

        for entry in sorted(self.plugins_root.iterdir(), key=lambda item: item.name.lower()):
            if entry.name.startswith("."):
                continue

            if entry.is_file() and entry.suffix.lower() in PLUGIN_EXTENSIONS:
                spec = PluginSpec(name=entry.stem, entry_path=entry.resolve())
                self._specs[spec.name] = spec
                continue

            if entry.is_dir():
                spec = self._read_directory_plugin(entry)
                if spec is not None:
                    self._specs[spec.name] = spec

        for name in list(self._runtimes):
            if name not in self._specs:
                self._runtimes.pop(name, None)
                self._errors.pop(name, None)

    def _read_directory_plugin(self, directory: Path) -> PluginSpec | None:
        manifest = directory / "plugin.json"
        if manifest.exists():
            try:
                loaded = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                return None
            if not isinstance(loaded, dict):
                return None
            name = loaded.get("name", directory.name)
            main = loaded.get("main", "main.pvi")
            if not isinstance(name, str) or not isinstance(main, str):
                return None
            entry_path = (directory / main).resolve()
            if not entry_path.exists() or entry_path.suffix.lower() not in PLUGIN_EXTENSIONS:
                return None
            return PluginSpec(name=name, entry_path=entry_path)

        default_main = directory / "main.pvi"
        if not default_main.exists():
            default_main = directory / "main.pvs"
        if default_main.exists():
            return PluginSpec(name=directory.name, entry_path=default_main.resolve())
        return None

    def install(self, source_path: Path | str) -> str:
        if not self.enabled:
            raise ScriptRuntimeError("Plugin system is disabled.", line=1)

        source = Path(source_path).expanduser().resolve()
        if not source.exists():
            raise ScriptRuntimeError(f"Plugin source not found: {source}", line=1)

        if source.is_file():
            if source.suffix.lower() not in PLUGIN_EXTENSIONS:
                raise ScriptRuntimeError("Plugin file must be .pvi or .pvs.", line=1)
            target = self.plugins_root / source.name
            shutil.copy2(source, target)
            self.discover()
            return f"Installed plugin file: {target.name}"

        if source.is_dir():
            target = self.plugins_root / source.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            self.discover()
            return f"Installed plugin directory: {target.name}"

        raise ScriptRuntimeError("Unsupported plugin source type.", line=1)

    def load_all(self) -> list[str]:
        messages: list[str] = []
        self.discover()
        for name in sorted(self._specs.keys(), key=str.lower):
            result = self.load_plugin(name)
            if result:
                messages.append(result)
        return messages

    def load_plugin(self, name: str) -> str:
        if not self.enabled:
            return "Plugin system disabled."

        spec = self._specs.get(name)
        if spec is None:
            return f"Plugin not found: {name}"

        try:
            runtime = self._create_runtime(spec)
            self._runtimes[name] = runtime
            self._errors.pop(name, None)

            if self._has_function(runtime, "on_load"):
                runtime.interpreter.call_function(runtime.env.get("on_load", line=1), [], line=1)
            return f"Plugin loaded: {name}"
        except ScriptError as exc:
            self._runtimes.pop(name, None)
            self._errors[name] = str(exc)
            return f"Plugin '{name}' error: {exc}"
        except Exception as exc:
            self._runtimes.pop(name, None)
            self._errors[name] = str(exc)
            return f"Plugin '{name}' error: {exc}"

    def run(self, plugin_name: str, function_name: str, args: list[Any]) -> Any:
        runtime = self._runtimes.get(plugin_name)
        if runtime is None:
            raise ScriptRuntimeError(f"Plugin is not loaded: {plugin_name}", line=1)
        target = runtime.env.get(function_name, line=1)
        try:
            return runtime.interpreter.call_function(target, args, line=1)
        except ScriptError:
            raise
        except Exception as exc:
            raise ScriptRuntimeError(str(exc), line=1) from exc

    def execute_script(self, source_path: Path | str) -> str:
        if not self.enabled:
            raise ScriptRuntimeError("Plugin system is disabled.", line=1)

        path = Path(source_path).expanduser().resolve()
        if not path.exists():
            raise ScriptRuntimeError(f"Script not found: {path}", line=1)

        program = self._load_program(path)

        interpreter = ScriptInterpreter(step_limit=self.step_limit)
        interpreter.register_native("api", self._native_api)
        interpreter.register_native("print", self._native_print)
        env = interpreter.create_global_env()
        env.define("pvim", self._pvim_id)
        interpreter.execute(program, env)
        return f"Script executed: {path.name}"

    def run_on_key(self, key: str) -> list[str]:
        if not self.enabled:
            return []
        outputs: list[str] = []
        for name in sorted(self._runtimes.keys(), key=str.lower):
            runtime = self._runtimes[name]
            if not self._has_function(runtime, "on_key"):
                continue
            try:
                value = runtime.interpreter.call_function(runtime.env.get("on_key", line=1), [key], line=1)
                if isinstance(value, str) and value:
                    outputs.append(f"{name}: {value}")
            except ScriptError as exc:
                self._errors[name] = str(exc)
                outputs.append(f"{name}: {exc}")
            except Exception as exc:
                self._errors[name] = str(exc)
                outputs.append(f"{name}: {exc}")
        return outputs

    def list_plugins(self) -> list[dict[str, str]]:
        self.discover()
        names = sorted(set(self._specs.keys()) | set(self._errors.keys()) | set(self._runtimes.keys()), key=str.lower)
        result: list[dict[str, str]] = []
        for name in names:
            spec = self._specs.get(name)
            result.append(
                {
                    "name": name,
                    "path": str(spec.entry_path) if spec else "-",
                    "loaded": "yes" if name in self._runtimes else "no",
                    "error": self._errors.get(name, ""),
                }
            )
        return result

    def _has_function(self, runtime: PluginRuntime, name: str) -> bool:
        try:
            value = runtime.env.get(name, line=1)
        except ScriptError:
            return False
        return hasattr(value, "call")

    def _create_runtime(self, spec: PluginSpec) -> PluginRuntime:
        program = self._load_program(spec.entry_path)

        interpreter = ScriptInterpreter(step_limit=self.step_limit)
        interpreter.register_native("api", self._native_api)
        interpreter.register_native("print", self._native_print)

        env = interpreter.create_global_env()
        env.define("pvim", self._pvim_id)

        runtime = PluginRuntime(
            name=spec.name,
            entry_path=spec.entry_path,
            interpreter=interpreter,
            env=env,
        )
        interpreter.execute(program, env)
        return runtime

    def _load_program(self, path: Path) -> Any:
        entry = path.resolve()
        stat = entry.stat()
        fingerprint = int(stat.st_mtime_ns)
        cached = self._ast_cache.get(entry)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        source = entry.read_text(encoding="utf-8")
        program = Parser.parse_script(source)
        self._ast_cache[entry] = (fingerprint, program)
        return program

    def _native_print(self, args: list[Any], line: int) -> str:
        return " ".join(str(item) for item in args)

    def _native_api(self, args: list[Any], line: int) -> Any:
        if self._host_api is None:
            raise ScriptRuntimeError("Host API is not available.", line=line)
        if len(args) < 2:
            raise ScriptRuntimeError("api() expects at least id and action.", line=line)

        object_id = args[0]
        action = args[1]
        payload = args[2:]

        if not isinstance(object_id, int):
            raise ScriptRuntimeError("api() first argument must be object id (int).", line=line)
        if object_id not in self._objects:
            raise ScriptRuntimeError(f"Unknown object id: {object_id}", line=line)
        if not isinstance(action, str):
            raise ScriptRuntimeError("api() second argument must be action string.", line=line)

        for item in payload:
            if not self._is_primitive(item):
                raise ScriptRuntimeError("api() only accepts primitive payload values.", line=line)

        try:
            result = self._host_api(object_id, action, payload)
        except ScriptError:
            raise
        except Exception as exc:
            raise ScriptRuntimeError(f"Host API '{action}' failed: {exc}", line=line) from exc
        if self._is_primitive(result):
            return result
        return self._register_object(result)

    def _register_object(self, value: Any) -> int:
        object_id = self._next_object_id
        self._next_object_id += 1
        self._objects[object_id] = value
        return object_id

    def _is_primitive(self, value: Any) -> bool:
        return value is None or isinstance(value, (bool, int, float, str))
