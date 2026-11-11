import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resume_tailor


def test_pdf_extraction_preserves_tightly_spaced_words(tmp_path):
    # A 2.7-point gap separates the words without a literal space glyph.
    stream = b"BT /F1 10 Tf 30 100 Td [(Tight) -270 (word) -270 (spacing)] TJ ET"
    pdf = tmp_path / "spacing.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        + f"5 0 obj << /Length {len(stream)} >>\nstream\n".encode()
        + stream + b"\nendstream\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
    )
    assert resume_tailor.extract_pdf_text(pdf) == "Tight word spacing"


def test_markdown_preserves_years_and_reflows_pdf_wraps():
    source = (
        "Example Candidate\n\nSummary\nFreelancing since\n2005 with full-\nstack work."
        "\n\nExperience\nEngineer\n\nExample Labs | May 2005 - Present\n"
        "Software consulting\n\n• Built a Mid-\ndleman site\n– Deployed to a test server"
    )
    assert resume_tailor.format_resume_markdown(source, ["Middleman"]) == (
        "# Example Candidate\n\n## Summary\n\nFreelancing since 2005 with full-stack work."
        "\n\n## Experience\n\n### Engineer\n\nExample Labs | May 2005 - Present\n\n"
        "Software consulting\n\n- Built a Middleman site\n  - Deployed to a test server"
    )


def _edit(change, before, after):
    return {"section": "Experience", "change": change, "before": before,
            "after": after, "why": "Prioritize relevant experience."}


def test_preview_and_download_use_the_same_edits(monkeypatch):
    resume = "Summary\nBuilt APIs.\nExperience\n• Maintained tests.\n• Built APIs.\n• Old item.\nEducation\nBuilt APIs."
    suggestions = [
        _edit("Reframe", "Built APIs.", "Built production APIs."),
        _edit("Reorder", "• Maintained tests.\n• Built APIs.", "• Built APIs.\n• Maintained tests."),
        _edit("Rephrase", "• Built APIs.", "• Shipped APIs."),  # overlaps with reorder
        _edit("Remove", "• Old item.", ""),
        _edit("Reorder", "Education", "Education."),  # wording only
        _edit("Rephrase", "Maintained tests.", "Shipped tests."),  # only part of a line
    ]

    def response(*args, **kwargs):
        plan = {"summary": "Backend experience", "suggestions": suggestions,
                "tailored_resume_markdown": "Independent draft to ignore."}
        return json.dumps({"result": json.dumps(plan)})

    monkeypatch.setattr(resume_tailor, "wrapped_run_p", response)
    plan = resume_tailor.build_tailor_plan_ai(resume, {})

    assert plan["suggestions"] == [suggestions[i] for i in (1, 3)]
    assert plan["tailored_resume_markdown"] == (
        "Summary\nBuilt APIs.\nExperience\n• Built APIs.\n• Maintained tests.\nEducation\nBuilt APIs."
    )
    assert plan["version"] == resume_tailor.TAILOR_PLAN_VERSION
