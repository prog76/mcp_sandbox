"""
Extension loader that discovers and loads extensions based on project.yaml.
"""

import importlib
import importlib.util
import logging
import os
import sys
from typing import Dict, List, Optional

import yaml

log = logging.getLogger("ipybox.extensions")


class ExtensionRegistry:
    """Registry of callable extensions for the kernel."""

    def __init__(self):
        self._helpers: Dict[str, callable] = {}
        self._metadata: Dict[str, Dict] = {}

    def add(self, name: str, fn: callable, description: str = "", category: str = "core") -> None:
        self._helpers[name] = fn
        self._metadata[name] = {
            "name": name,
            "description": description or fn.__doc__ or "",
            "category": category,
        }

    def get(self, name: str) -> Optional[callable]:
        return self._helpers.get(name)

    def list(self) -> List[str]:
        return sorted(self._helpers.keys())

    def describe(self, name: str) -> Optional[Dict]:
        return self._metadata.get(name)

    def inject_into_builtins(self) -> None:
        import builtins
        for name, fn in self._helpers.items():
            setattr(builtins, name, fn)

    def __contains__(self, name: str) -> bool:
        return name in self._helpers

    def __iter__(self):
        return iter(self._helpers)


_registry: Optional[ExtensionRegistry] = None


def get_registry() -> ExtensionRegistry:
    global _registry
    if _registry is None:
        _registry = ExtensionRegistry()
        load_extensions_from_config()
    return _registry


def load_extension_file(filepath: str, registry: ExtensionRegistry) -> None:
    try:
        spec = importlib.util.spec_from_file_location(
            f"ipybox_ext_{hash(filepath) % 1000000}",
            filepath,
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        if hasattr(module, "register") and callable(module.register):
            module.register(registry)
            log.debug("Loaded extension: %s", filepath)
    except Exception as e:
        log.warning("Failed to load extension %s: %s", filepath, e)


def discover_extensions(extension_dirs: List[str]) -> List[str]:
    files = []
    for base_dir in extension_dirs:
        if not os.path.exists(base_dir):
            continue
        if os.path.isdir(base_dir):
            for root, _, filenames in os.walk(base_dir):
                for f in filenames:
                    if f.endswith(".py") and f != "__init__.py":
                        files.append(os.path.join(root, f))
    return files


def load_extensions_from_config(
    project_yaml_path: str = "/etc/ipybox/project.yaml",
    extension_dirs: List[str] = None,
) -> ExtensionRegistry:
    registry = get_registry()

    if extension_dirs is None:
        import ipybox.extensions
        ext_dir = os.path.dirname(ipybox.extensions.__file__)
        extension_dirs = [
            os.path.join(ext_dir, "core"),
            os.path.join(ext_dir, "remote"),
        ]
        env_dir = os.environ.get("IPYBOX_EXTENSIONS_DIR", "")
        if env_dir:
            extension_dirs.append(env_dir)

    available_extensions = {}
    for filepath in discover_extensions(extension_dirs):
        dir_name = os.path.basename(os.path.dirname(filepath))
        base_name = os.path.splitext(os.path.basename(filepath))[0]
        ext_name = f"{dir_name}.{base_name}"
        available_extensions[ext_name] = filepath

    requested_extensions = []
    extra_paths = []

    if os.path.exists(project_yaml_path):
        try:
            with open(project_yaml_path) as f:
                config = yaml.safe_load(f) or {}
            extensions_config = config.get("extensions", {})
            requested_extensions = extensions_config.get("load", [])
            extra_paths = extensions_config.get("extra_extensions", [])
            log.info("Loaded project.yaml: %s", config.get("name", "unknown"))
        except Exception as e:
            log.warning("Failed to read project.yaml: %s", e)

    if not requested_extensions and not extra_paths:
        requested_extensions = [
            "core.exec_run",
            "core.mcp_call",
            "core.skill_mgmt",
            "core.introspection",
        ]
        log.info("No extensions specified in project.yaml, loading defaults")

    loaded = []
    for ext_name in requested_extensions:
        if ext_name in available_extensions:
            load_extension_file(available_extensions[ext_name], registry)
            loaded.append(ext_name)
        else:
            log.warning("Extension '%s' not found", ext_name)

    for extra_path in extra_paths:
        if os.path.isfile(extra_path) and extra_path.endswith(".py"):
            load_extension_file(extra_path, registry)
            loaded.append(f"extra:{os.path.basename(extra_path)}")

    log.info("Loaded %d extensions: %s", len(loaded), ", ".join(loaded))
    return registry


def load_extensions(
    project_yaml_path: str = "/etc/ipybox/project.yaml",
) -> ExtensionRegistry:
    return load_extensions_from_config(project_yaml_path)
