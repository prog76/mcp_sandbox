"""Introspection extensions — list_functions, describe_function."""

import builtins
import importlib
import inspect


def _short_doc(fn):
    """Return the first docstring line of a callable (or a fallback)."""
    doc = inspect.getdoc(fn)
    if not doc:
        return "No description"
    return doc.strip().splitlines()[0]


def _resolve_callable(name, registry):
    """Resolve ``name`` against the registry, module globals, builtins, or an
    imported module (allowing dotted names such as ``os.getcwd``)."""
    if name in registry._helpers:
        return registry._helpers[name]

    parts = name.split(".")
    head = globals().get(parts[0])
    if head is None:
        head = getattr(builtins, parts[0], None)
    if head is None:
        try:
            head = importlib.import_module(parts[0])
        except Exception:
            return None
    for p in parts[1:]:
        head = getattr(head, p, None)
        if head is None:
            return None
    return head


def register(registry):
    """Register introspection helpers."""

    def list_functions() -> str:
        helpers = registry._helpers
        visible = sorted(
            (name, fn)
            for name, fn in helpers.items()
            if not name.startswith("_")
        )
        if not visible:
            return "No functions available."
        lines = [f"Available kernel functions ({len(visible)}):"]
        for name, fn in visible:
            lines.append(f"- {name}: {_short_doc(fn)}")
        return "\n".join(lines)

    def describe_function(name: str) -> str:
        fn = _resolve_callable(name, registry)
        if fn is None or not callable(fn):
            return f"Error: Function '{name}' not found."

        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None

        parts = [f"{name}{sig if sig is not None else '(...) '}"]
        parts.append("")
        if sig is not None:
            parts.append("Input parameters:")
            params = list(sig.parameters.values())
            if not params:
                parts.append("  (none)")
            for p in params:
                ann = f": {p.annotation.__name__}" if p.annotation is not inspect.Parameter.empty else ""
                parts.append(f"  - {p.name}{ann}")
        parts.append(f"Output type: {getattr(fn, '__annotations__', {}).get('return', 'not annotated')}")
        parts.append("")
        parts.append("Docstring:")
        doc = inspect.getdoc(fn)
        parts.append(doc if doc else "No docstring available.")
        return "\n".join(parts)

    registry.add("list_functions", list_functions,
                 description="List available kernel helper functions", category="core")
    registry.add("describe_function", describe_function,
                 description="Describe a kernel helper function's signature", category="core")
