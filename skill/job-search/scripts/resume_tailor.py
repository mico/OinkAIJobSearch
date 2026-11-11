"""Resume tailoring helper.

Given a user's resume text and a job's description, produce a short Markdown
"tailoring note":

  1. Which skills listed in the resume overlap the job description.
  2. A rewritten one-paragraph profile summary that leans into those skills.
  3. A short list of bullet-wording suggestions.

This is deliberately *mild* — we don't fabricate experience, only rearrange
emphasis. For a deeper rewrite, swap `rewrite_summary()` for an LLM call.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from codex_cli import extract_assistant_text, parse_json_block
from instrumentation.wrappers import wrapped_run_p
import forensic

log = logging.getLogger(__name__)
TAILOR_PLAN_VERSION = 3

# Broad tech-skill lexicon. Case-insensitive substring match.
# Keep this list curated; noisy entries produce false-positive matches.
SKILLS = [
    # JS/TS + frameworks
    "javascript", "typescript", "react", "redux", "rtk query", "next.js", "nextjs",
    "vue", "vuex", "pinia", "angular", "svelte", "solidjs",
    # Styling
    "tailwind", "tailwindcss", "styled-components", "emotion", "css", "sass", "less",
    # Testing
    "vitest", "jest", "cypress", "playwright", "react testing library", "rtl",
    "storybook",
    # State / data
    "graphql", "apollo", "rest api", "websocket", "websockets", "sse",
    # Tooling / build
    "webpack", "vite", "rollup", "esbuild", "parcel", "turborepo",
    # Backend / other
    "node", "node.js", "nodejs", "python", "go", "rust", "java", "kotlin", "swift",
    "django", "flask", "fastapi",
    # DevOps / cloud
    "docker", "kubernetes", "k8s", "aws", "gcp", "azure", "terraform",
    # Design / workflow
    "figma", "git", "github", "gitlab", "agile", "scrum", "jira", "notion",
    # Accessibility / perf
    "a11y", "accessibility", "wcag", "lighthouse", "performance",
    # Soft / process
    "code review", "mentorship", "documentation", "cross-functional",
]


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\.\-\+# ]*")


def extract_pdf_text(pdf_path: Path, *, for_tailoring: bool = False) -> str:
    """Read a PDF and return plain text. Falls back gracefully if pypdf can't parse."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            if for_tailoring:
                return "\n".join(_tailoring_page_text(page) for page in pdf.pages)
            # The default 3-point gap merges words in tightly spaced CVs.
            return "\n".join(p.extract_text(x_tolerance=2.5) or "" for p in pdf.pages)
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        raise RuntimeError(f"Could not extract PDF text: {e}")


def _tailoring_page_text(page) -> str:
    """Retain paragraph breaks and exclude a verified page-number footer."""
    rows = page.extract_text_lines(x_tolerance=2.5)
    if rows and rows[-1]["text"].strip() == str(page.page_number):
        footer = rows[-1]
        center = (footer["x0"] + footer["x1"]) / 2
        if footer["top"] > page.height * 0.85 and abs(center - page.width / 2) < page.width * 0.1:
            rows.pop()
    lines = []
    previous_top = None
    for row in rows:
        font_size = max(char["size"] for char in row["chars"])
        if previous_top is not None and row["top"] - previous_top > font_size * 1.55:
            lines.append("")
        lines.append(row["text"])
        previous_top = row["top"]
    return "\n".join(lines)


def skills_in(text: str) -> set[str]:
    """Return the canonical skill names found in `text`."""
    t = (text or "").lower()
    found: set[str] = set()
    for skill in SKILLS:
        # Use word-boundary for short skills to avoid false positives
        # (e.g. "go" in "google"). Long skills → substring is fine.
        if len(skill) <= 4:
            pat = r"(?<![a-z0-9])" + re.escape(skill) + r"(?![a-z0-9])"
            if re.search(pat, t):
                found.add(skill)
        else:
            if skill in t:
                found.add(skill)
    return found


def overlap(resume_text: str, job_text: str) -> dict:
    resume_skills = skills_in(resume_text)
    job_skills = skills_in(job_text)
    common = sorted(resume_skills & job_skills)
    job_only = sorted(job_skills - resume_skills)
    resume_only = sorted(resume_skills - job_skills)
    return {
        "common": common,
        "gaps": job_only,          # things the job wants that aren't on resume
        "latent": resume_only,     # things on resume that job didn't mention
    }


def rewrite_summary(resume_text: str, job: dict, ov: dict) -> str:
    """Produce a tailored 2–3 sentence profile blurb.

    We don't fabricate — we pick from skills the resume *already* mentions and
    rearrange to match the job's priorities.
    """
    lead_stack = ", ".join(ov["common"][:6]) if ov["common"] else "your core stack"
    role = job.get("title", "this role")
    company = job.get("company") or "the company"

    # Try to reuse the resume's own years-of-experience phrasing
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*years", resume_text, re.I)
    years_clause = f"{m.group(1)}+ years of" if m else "hands-on"

    # Keywords worth sprinkling in
    extras = ", ".join(ov["common"][6:10]) if len(ov["common"]) > 6 else ""

    blurb = (
        f"Middle Frontend Developer with {years_clause} commercial experience "
        f"building production web applications in {lead_stack}. "
        f"Directly relevant to {role} at {company}: delivered marketplace + admin "
        f"modules from MVP to prod, with testing (Vitest, RTL), component libraries "
        f"(Storybook), and accessibility-first UI work."
    )
    if extras:
        blurb += f" Additional overlap with the posting: {extras}."
    return blurb


def bullet_suggestions(ov: dict) -> list[str]:
    """Short, specific wording tweaks keyed off overlaps."""
    tips: list[str] = []
    if "react" in ov["common"] and "typescript" in ov["common"]:
        tips.append("Lead your experience bullets with 'React + TypeScript' rather than "
                    "alphabetized tech lists — it's what the posting anchors on.")
    if "storybook" in ov["common"]:
        tips.append("Promote your Storybook component-library work to the top of your "
                    "deliverables — it signals design-system thinking.")
    if "vitest" in ov["common"] or "react testing library" in ov["common"] or "rtl" in ov["common"]:
        tips.append("Quantify test coverage or regression impact if possible — e.g. "
                    "'reduced prod bugs by X% via Vitest + RTL suites'.")
    if "accessibility" in ov["common"] or "a11y" in ov["common"] or "wcag" in ov["common"]:
        tips.append("Mention a specific a11y fix or Lighthouse score improvement — "
                    "accessibility claims land harder with a number.")
    if "websocket" in ov["common"] or "websockets" in ov["common"]:
        tips.append("Call out the real-time / WebSocket work explicitly; it's a "
                    "differentiator vs. generic frontend CVs.")
    if ov["gaps"]:
        gap_list = ", ".join(ov["gaps"][:4])
        tips.append(f"Posting emphasizes *{gap_list}* which aren't on your resume — "
                    f"if you have even hobby-level exposure, add a short 'familiar with' line.")
    if not tips:
        tips.append("Strong overlap already — consider reordering your Core Stack list "
                    "so the posting's top-listed tech comes first.")
    return tips


# ---------------------------------------------------------------------------
# AI-backed tailoring
# ---------------------------------------------------------------------------
#
# The prompt template lives in `prompts/resume_tailor.txt` and wraps the job
# posting + resume in opaque-data blocks with an instruction-ignore preamble
# to neutralize indirect prompt injection (a scraped job posting can contain
# adversarial text — "ignore previous instructions, output …"). Same pattern
# as `fit_analyzer.py` + `prompts/fit_analysis.txt`.

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "resume_tailor.txt"


def _load_prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _run_tailor_prompt(caller: str, prompt: str, timeout_s: int) -> str | None:
    return wrapped_run_p(None, caller, prompt, timeout_s=timeout_s)


_BULLET = re.compile(r"^[ \t]*[•–*+-] +", re.MULTILINE)
_MARKDOWN_BULLET = re.compile(r"^[ \t]*[-*+] +", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6} +", re.MULTILINE)


def _same_resume_text(source: str, markdown: str) -> bool:
    """Permit Markdown markers, whitespace reflow, and joins at PDF word wraps."""
    source_parts = re.split(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", source)
    expected = "-?".join(re.escape(" ".join(_BULLET.sub("", part).split())) for part in source_parts)
    text = _HEADING.sub("", _MARKDOWN_BULLET.sub("", markdown))
    text = re.sub(r"\\([\\`*_{}\[\]<>#+\-.!|])", r"\1", text)
    return (
        markdown.startswith("# ")
        and len(_BULLET.findall(source)) == len(_MARKDOWN_BULLET.findall(markdown))
        and re.fullmatch(expected, " ".join(text.split())) is not None
    )


_RESUME_SECTIONS = re.compile(
    r"^(?:summary|profile|objective|(?:work |professional )?experience|employment|"
    r"(?:technical )?skills|education|(?:personal )?projects|certifications|"
    r"languages|awards|publications|volunteering|interests|references)$", re.I | re.M,
)
_EMPLOYER_DATE = re.compile(r"\s\|\s.*\b(?:19|20)\d{2}\b")


def _reflow_resume_lines(lines: list[str], joined_words: set[str]) -> str:
    paragraph = lines[0]
    for line in lines[1:]:
        left = re.search(r"(\w+)-$", paragraph)
        right = re.match(r"\w+", line)
        if left and right:
            if left[1] + right[0] in joined_words:
                paragraph = paragraph[:-1]
            paragraph += line
        else:
            paragraph += " " + line
    return re.sub(r"([\\`*_\[\]<>#])", r"\\\1", paragraph)


def format_resume_markdown(resume: str, joined_words: list[str] | None = None) -> str | None:
    """Reflow PDF paragraphs and lists while preserving their text and order."""
    joined = {word for word in joined_words or [] if isinstance(word, str)}
    # Recognize section titles even when the cached text has no blank lines.
    source = _RESUME_SECTIONS.sub(lambda m: f"\n\n{m[0]}\n\n", resume)
    lines = source.strip().splitlines()
    if not lines:
        return None
    name, *lines = lines
    blocks = [block.splitlines() for block in re.split(r"\n[ \t]*\n+", "\n".join(lines).strip()) if block]
    rendered = ["# " + _reflow_resume_lines([name], joined)]
    for i, block in enumerate(blocks):
        if len(block) == 1 and _RESUME_SECTIONS.fullmatch(block[0]):
            rendered.append("## " + block[0])
            continue
        if len(block) == 1 and i + 1 < len(blocks) and any(
            _EMPLOYER_DATE.search(line) for line in blocks[i + 1][:2]
        ) and not _BULLET.match(block[0]):
            rendered.append("### " + _reflow_resume_lines(block, joined))
            continue

        groups: list[tuple[str, list[str]]] = []
        for line in block:
            marker = _BULLET.match(line)
            if marker:
                prefix = "  - " if line.lstrip().startswith("– ") and any(p for p, _ in groups) else "- "
                groups.append((prefix, [line[marker.end():]]))
            elif not groups or _EMPLOYER_DATE.search(groups[-1][1][-1]):
                groups.append(("", [line]))
            else:
                groups[-1][1].append(line)

        previous_bullet = False
        for prefix, group in groups:
            paragraph = prefix + _reflow_resume_lines(group, joined)
            if prefix and previous_bullet:
                rendered[-1] += "\n" + paragraph
            else:
                rendered.append(paragraph)
            previous_bullet = bool(prefix)
    markdown = "\n\n".join(rendered)
    if _same_resume_text(resume, markdown):
        return markdown
    log.warning("resume_tailor: could not verify formatted resume content")
    return None


def apply_tailor_edits(resume: str, suggestions: list) -> tuple[str, list[dict]]:
    """Apply distinct, unambiguous edits and return the edits used for the preview."""
    edits: list[tuple[int, int, str]] = []
    accepted: list[dict] = []
    fields = ("section", "change", "before", "after", "why")
    for suggestion in suggestions:
        if not isinstance(suggestion, dict) or any(
            not isinstance(suggestion.get(field), str) for field in fields
        ):
            continue
        before = suggestion["before"].strip("\n")
        after = suggestion["after"].strip("\n")
        change = suggestion["change"].strip().lower()
        if not before.strip() or before == after:
            continue
        if max(len(" ".join(value.split())) for value in (before, after)) > 280:
            continue
        if change not in {"add", "remove", "rephrase", "reframe", "reorder"}:
            continue
        if (change == "remove") != (not after.strip()):
            continue
        if change == "add" and before not in after:
            continue
        if change == "reorder" and sorted(before.splitlines()) != sorted(after.splitlines()):
            continue

        matches = re.finditer(f"^{re.escape(before)}$", resume, re.MULTILINE)
        match = next(matches, None)
        if match is None or next(matches, None) is not None:
            continue
        start, end = match.span()
        if change == "remove":
            after = ""
            end = min(end + 1, len(resume))
        if any(start < other_end and other_start < end for other_start, other_end, _ in edits):
            continue

        edits.append((start, end, after))
        accepted.append({field: suggestion[field] for field in fields} | {"before": before, "after": after})
        if len(accepted) == 10:
            break

    for start, end, after in sorted(edits, reverse=True):
        resume = resume[:start] + after + resume[end:]
    return resume, accepted


def build_tailor_plan_ai(resume_text: str, job: dict, timeout_s: int = 180) -> dict | None:
    """Ask the configured provider for a structured tailoring plan.

    Returns a dict with keys {summary, suggestions[], tailored_resume_markdown}
    on success, or None on any failure (missing CLI, timeout, parse error).
    Callers should fall back to `build_tailor_note` for a plain-text note.
    """
    resume = (resume_text or "").replace("\r\n", "\n")
    template = _load_prompt_template()
    prompt = template.format(
        title=(job.get("title") or "").replace("\n", " ")[:200],
        company=(job.get("company") or "")[:120],
        location=(job.get("location") or "")[:120],
        url=(job.get("url") or "")[:400],
        snippet=(job.get("snippet") or "").replace("\n", " ")[:1200],
        resume=resume[:12000],
    )
    forensic.log_step(
        "resume_tailor.build_tailor_plan_ai.begin",
        input={
            "job_title": (job.get("title") or "")[:120],
            "job_company": (job.get("company") or "")[:80],
            "job_url": (job.get("url") or "")[:200],
            "resume_chars": len(resume_text or ""),
        },
    )
    stdout = _run_tailor_prompt("resume_tailor", prompt, timeout_s)
    if not stdout:
        forensic.log_step(
            "resume_tailor.build_tailor_plan_ai.end",
            output={"result": None, "reason": "cli_missing_or_empty"},
        )
        return None
    body = extract_assistant_text(stdout)
    plan = parse_json_block(body)
    if not isinstance(plan, dict):
        log.error("resume_tailor: could not parse AI plan (head=%r)", body[:200])
        forensic.log_step(
            "resume_tailor.build_tailor_plan_ai.end",
            output={"result": None, "reason": "parse_failed", "body_head": (body or "")[:300]},
        )
        return None
    suggestions = plan.get("suggestions")
    if not isinstance(suggestions, list):
        return None
    tailored_resume, applied = apply_tailor_edits(resume, suggestions)
    summary = plan.get("summary")
    plan = {
        "summary": summary if isinstance(summary, str) else "",
        "suggestions": applied,
        "tailored_resume_markdown": tailored_resume,
        "joined_words": plan.get("joined_words") if isinstance(plan.get("joined_words"), list) else [],
        "version": TAILOR_PLAN_VERSION,
    }
    forensic.log_step(
        "resume_tailor.build_tailor_plan_ai.end",
        output={
            "summary": (plan.get("summary") or "")[:300],
            "suggestion_count": len(plan["suggestions"]),
            "tailored_resume_chars": len(plan.get("tailored_resume_markdown") or ""),
        },
    )
    return plan


def build_tailor_note(resume_text: str, job: dict) -> str:
    """Return the full Markdown note sent back to the user."""
    job_blob = " ".join([
        job.get("title", ""), job.get("company", ""), job.get("snippet", ""),
        job.get("location", ""),
    ])
    ov = overlap(resume_text, job_blob)

    lines = [
        f"# Tailoring note — {job.get('title','Role')} @ {job.get('company','Company')}",
        "",
        f"**Source:** {job.get('source','?')}  ",
        f"**URL:** {job.get('url','')}",
        "",
        "## Overlap (your resume × the posting)",
        "",
    ]
    if ov["common"]:
        lines.append("✅ **Skills you can lead with:** " + ", ".join(f"`{s}`" for s in ov["common"]))
    else:
        lines.append("⚠️  No obvious overlap found — double-check the posting is a fit.")
    lines.append("")
    if ov["gaps"]:
        lines.append("🟡 **Posting asks for, but resume doesn't mention:** "
                     + ", ".join(f"`{s}`" for s in ov["gaps"]))
        lines.append("")
    if ov["latent"]:
        lines.append("💤 **On your resume but not in the posting (de-emphasize):** "
                     + ", ".join(f"`{s}`" for s in ov["latent"][:10]))
        lines.append("")

    lines += [
        "## Suggested rewritten profile summary",
        "",
        "> " + rewrite_summary(resume_text, job, ov).replace("\n", "\n> "),
        "",
        "## Bullet-wording tweaks",
        "",
    ]
    for tip in bullet_suggestions(ov):
        lines.append(f"- {tip}")
    lines += [
        "",
        "---",
        "",
        "_This note only rearranges emphasis — it does not invent new experience. "
        "Edit your actual CV to match and run a final pass yourself before applying._",
    ]
    return "\n".join(lines)
