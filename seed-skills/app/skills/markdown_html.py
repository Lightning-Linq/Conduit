"""markdown-html — render Markdown to HTML (sanitized by default)."""

from __future__ import annotations

import markdown as markdown_lib
import nh3

from app.registry import Skill, SkillError, register


def run(input_data: dict) -> dict:
    text = input_data.get("markdown")
    if not isinstance(text, str):
        raise SkillError("`markdown` (string) is required")
    allow_raw_html = bool(input_data.get("allow_raw_html", False))
    html = markdown_lib.markdown(text, extensions=["extra", "sane_lists"])
    if not allow_raw_html:
        # Strip scripts / active markup so the result is safe to render in a browser.
        # Opt out with allow_raw_html=true only if you fully trust the Markdown source.
        html = nh3.clean(html)
    return {"html": html, "sanitized": not allow_raw_html}


register(
    Skill(
        name="markdown-html",
        description="Render Markdown to HTML; output is HTML-sanitized unless allow_raw_html=true.",
        handler=run,
        input_example={"markdown": "# Title\n\nSome **bold** text."},
    )
)
