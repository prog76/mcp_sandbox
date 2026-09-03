"""Skill management extensions — list, get, create, update skills."""

import os
import yaml

from ipybox.kernel.templating import render_template


def register(registry):
    """Register skill management helpers."""

    SKILLS_DIR = os.environ.get("IPYBOX_SKILLS_DIR", "/var/mcp/skills")

    def _safe_name(name: str) -> str:
        return name.replace("/", "_").replace("\\", "_").replace("..", "_")

    def _parse_frontmatter(text: str):
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        try:
            fm = yaml.safe_load(parts[1]) or {}
        except Exception:
            fm = {}
        return fm if isinstance(fm, dict) else {}, parts[2].lstrip("\n")

    def _skill_dirs():
        sds = set()
        for root, dirs, files in os.walk(SKILLS_DIR):
            dirs[:] = [d for d in dirs if d not in ("prompts",)]
            if "SKILL.md" in files:
                sds.add(root)
        return sds

    def list_skills() -> str:
        """List available skills as a catalog."""
        if not os.path.isdir(SKILLS_DIR):
            return "No skills directory found"

        sds = _skill_dirs()
        entries = []

        for root, dirs, files in os.walk(SKILLS_DIR):
            dirs[:] = sorted([d for d in dirs if d not in ("prompts",) and not d.startswith(".")])
            for f in sorted(files):
                if not f.endswith(".md"):
                    continue
                ap = os.path.join(root, f)
                rel = os.path.relpath(ap, SKILLS_DIR).replace(os.sep, "/")

                if f == "SKILL.md":
                    fm, _ = _parse_frontmatter(open(ap).read())
                    desc = (fm.get("description") or "").strip()
                    if not desc:
                        continue
                    skillpath = os.path.relpath(root, SKILLS_DIR).replace(os.sep, "/")
                    entries.append((skillpath, desc))
                elif os.path.dirname(ap) in sds:
                    continue
                else:
                    fm, _ = _parse_frontmatter(open(ap).read())
                    desc = (fm.get("description") or "").strip()
                    if not desc:
                        continue
                    entries.append((rel[:-3], desc))

        if not entries:
            return "No skills available."

        entries.sort()
        lines = ["Skills catalog:"]
        for path_label, desc in entries:
            first = desc.splitlines()[0] if desc else desc
            lines.append(f"- {path_label}: {first}")
        return "\n".join(lines)

    def get_skill(name: str) -> str:
        """Read a skill by name or path."""
        if name.startswith("/") or ".." in name.split("/"):
            return f"Error: Invalid skill path '{name}'"

        candidates = [
            os.path.join(SKILLS_DIR, name),
            os.path.join(SKILLS_DIR, name + ".md"),
            os.path.join(SKILLS_DIR, name, "SKILL.md"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                try:
                    with open(c) as f:
                        return render_template(f.read())
                except Exception as e:
                    return f"Error reading skill '{name}': {e}"

        bare = name.split("/")[-1]
        for root, dirs, files in os.walk(SKILLS_DIR):
            if "prompts" in dirs:
                dirs.remove("prompts")
            for f in files:
                if f.endswith(".md") and f[:-3] == bare:
                    try:
                        with open(os.path.join(root, f)) as fh:
                            return render_template(fh.read())
                    except Exception as e:
                        return f"Error reading skill '{name}': {e}"

        return f"Error: Skill '{name}' not found."

    def create_skill(name: str, content: str) -> str:
        """Create a new skill."""
        safe = _safe_name(name)
        path = os.path.join(SKILLS_DIR, f"{safe}.md")
        if os.path.exists(path):
            return f"Error: Skill '{name}' already exists."
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return f"Skill '{name}' created successfully."
        except Exception as e:
            return f"Error creating skill '{name}': {e}"

    def update_skill(name: str, content: str) -> str:
        """Update an existing skill."""
        safe = _safe_name(name)
        path = os.path.join(SKILLS_DIR, f"{safe}.md")
        if not os.path.exists(path):
            return f"Error: Skill '{name}' not found."
        try:
            with open(path, "w") as f:
                f.write(content)
            return f"Skill '{name}' updated successfully."
        except Exception as e:
            return f"Error updating skill '{name}': {e}"

    registry.add("list_skills", list_skills, description="List available skills", category="core")
    registry.add("get_skill", get_skill, description="Read a skill by name", category="core")
    registry.add("create_skill", create_skill, description="Create a new skill", category="core")
    registry.add("update_skill", update_skill, description="Update an existing skill", category="core")
