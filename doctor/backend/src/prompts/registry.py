"""
Prompt template registry.

Loads Jinja2 templates from the templates/ directory and provides
a unified interface for rendering prompts with context variables.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template

_templates_dir = Path(__file__).resolve().parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_templates_dir)) if _templates_dir.exists() else None,
    autoescape=False,
)


def get_template(name: str) -> Template:
    """
    Get a Jinja2 template by name.

    Args:
        name: Template filename (e.g., 'triage.j2').

    Returns:
        Compiled Jinja2 Template.
    """
    return _env.get_template(name)


def render_prompt(template_name: str, **context: object) -> str:
    """
    Render a prompt template with the given context variables.

    Args:
        template_name: Template filename to render.
        **context: Variables to substitute into the template.

    Returns:
        Rendered prompt string.
    """
    template = get_template(template_name)
    return template.render(**context)


def template_exists(name: str) -> bool:
    """Check if a template file exists."""
    return (_templates_dir / name).exists()
