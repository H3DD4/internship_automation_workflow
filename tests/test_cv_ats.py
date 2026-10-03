"""The CV check is rules only: the same CV always gets the same report, and
every finding quotes what it found."""

import io
import zipfile

import cv_ats

GOOD = """Lina Ben Salah
lina.bensalah@example.com | +216 22 333 444 | linkedin.com/in/lina
EDUCATION
Engineering degree in Computer Science, ENSIT, 2022 - 2027
SKILLS
Python, SQL, React, Docker, Linux, Git, FastAPI, PostgreSQL
EXPERIENCE
Software intern, Acme, Jun 2025 - Aug 2025
- Built a data pipeline processing 1200 records a day
- Reduced report time by 40% with caching
- Designed 12 dashboards used by 3 teams
- Automated 25 weekly checks
PROJECTS
- Developed a chat app for 200 students
- Tested 15 API endpoints with pytest
ACTIVITIES
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 1
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 2
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 3
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 4
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 5
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 6
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 7
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 8
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 9
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 10
Member of the university robotics club, organising workshops and mentoring first-year students on embedded systems and teamwork during term 11
LANGUAGES
French, English, Arabic
""".splitlines()

PDF = {"kind": "pdf", "pages": 1, "rows": 20, "two_sided_rows": 0}


def _by_id(report):
    return {c["id"]: c for c in report["checks"]}


def test_a_clean_cv_scores_high_and_is_stable():
    first, second = cv_ats.evaluate(GOOD, dict(PDF)), cv_ats.evaluate(GOOD, dict(PDF))
    assert first == second                                 # deterministic
    assert first["score"] >= 85 and first["tone"] == "good" and not first["blocking"]
    assert all(c["status"] == "ok" for c in first["checks"] if c["category"] == "Readable by machines")


def test_a_scanned_cv_is_blocked_with_the_reason():
    report = cv_ats.evaluate(["Lina"], {"kind": "pdf", "pages": 1, "images": 1})
    assert report["blocking"] and report["score"] <= 30
    text = _by_id(report)["text"]
    assert text["status"] == "fail" and "scan" in text["detail"] and "text PDF" in text["fix"]


def test_missing_sections_are_named_with_a_heading_to_use():
    lines = [l for l in GOOD if l not in ("SKILLS", "EXPERIENCE")]
    report = cv_ats.evaluate(lines, dict(PDF))
    assert report["missing_sections"] == ["Experience", "Skills"]
    assert "standard heading" in _by_id(report)["section_experience"]["fix"]


def test_french_headings_are_recognised():
    lines = ["Nom", "a@b.fr", "FORMATION", "2022 - 2027", "COMPÉTENCES", "Python, SQL, Git, Docker, Linux, React",
             "EXPÉRIENCES PROFESSIONNELLES", "- Développé un outil pour 30 utilisateurs en 2025"]
    ids = _by_id(cv_ats.evaluate(lines, dict(PDF)))
    assert all(ids[f"section_{s}"]["status"] == "ok" for s in ("education", "skills", "experience"))


def test_a_two_column_pdf_is_flagged_with_an_example():
    meta = dict(PDF, rows=40, two_sided_rows=22, side_example="Python  |  Built a pipeline")
    layout = _by_id(cv_ats.evaluate(GOOD, meta))["layout"]
    assert layout["status"] == "fail" and "Built a pipeline" in layout["detail"]


def test_tables_and_text_boxes_in_word_are_flagged():
    layout = _by_id(cv_ats.evaluate(GOOD, {"kind": "docx", "tables": 2, "text_boxes": 1}))["layout"]
    assert layout["status"] == "fail" and "2 table(s)" in layout["detail"] and "1 text box" in layout["detail"]


def test_an_email_only_in_the_header_is_caught():
    lines = [l for l in GOOD if "@" not in l]
    email = _by_id(cv_ats.evaluate(lines, {"kind": "docx", "header_footer_text": "lina@example.com"}))["email"]
    assert email["status"] == "fail" and "header or footer" in email["detail"]


def test_decorative_bullets_and_first_person_are_marked_on_the_lines():
    lines = GOOD[:8] + ["➢ I built my own tool for my team", "➢ I led my group", "➢ I wrote my report"] + GOOD[12:]
    report = cv_ats.evaluate(lines, dict(PDF))
    ids = _by_id(report)
    assert ids["bullets"]["status"] == "warn" and "➢" in ids["bullets"]["detail"]
    assert ids["pronouns"]["status"] == "warn"
    assert any(l["mark"] for l in report["lines"] if l["text"].startswith("➢"))


def test_results_without_numbers_are_flagged_only_in_experience_and_projects():
    lines = [l for l in GOOD]
    lines[8:12] = ["- Built a data pipeline", "- Worked on reports", "- Designed dashboards", "- Helped the team"]
    lines[13:15] = ["- Developed a chat app", "- Tested the API"]
    numbers = _by_id(cv_ats.evaluate(lines, dict(PDF)))["numbers"]
    assert numbers["status"] == "fail"                      # the phone, years and "2D" don't count
    assert not cv_ats._measurable("Built a 3D viewer in 2024, Bac+5")
    assert cv_ats._measurable("Reduced costs by 40%")


def test_action_verbs_ignore_the_skills_list():
    verbs = _by_id(cv_ats.evaluate(GOOD, dict(PDF)))["verbs"]
    assert verbs["status"] == "ok" and "6 of 6" in verbs["detail"]


def test_a_word_file_is_inspected_for_tables_and_header_contact():
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = f'<w:document {w}><w:body><w:p><w:r><w:t>EDUCATION</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Python</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
    header = f'<w:hdr {w}><w:p><w:r><w:t>lina@example.com</w:t></w:r></w:p></w:hdr>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/header1.xml", header)
    lines, meta = cv_ats.inspect_file("cv.docx", buffer.getvalue())
    assert "EDUCATION" in lines and meta["tables"] == 1 and "lina@example.com" in meta["header_footer_text"]


def test_an_unsupported_file_is_refused_clearly():
    report = cv_ats.check("cv.png", b"\x89PNG")
    assert report["blocking"] and _by_id(report)["file_type"]["status"] == "fail"


# --- Found on real CVs: the same CV exported twice scored differently, with a
# --- wrong "no Education section", because one PDF spaces its letters apart.

def test_a_heading_with_spaced_letters_is_still_that_section():
    lines = [l.replace("EDUCATION", "EDUCA TION") for l in GOOD]
    report = cv_ats.evaluate(lines, dict(PDF))
    ids = _by_id(report)
    assert ids["section_education"]["status"] == "ok"           # the section is there
    whole = ids["whole_words"]                                   # …and the broken text is its own finding
    assert whole["status"] == "warn" and "EDUCA TION" in whole["detail"] and "standard font" in whole["fix"]
    assert any(l["mark"] and "split" in l["mark"]["why"] for l in report["lines"])


def test_letters_lost_at_fi_and_fl_are_reported_with_the_words():
    lines = GOOD + ["- Earned 3 certi cations in arti cial intelligence", "- Automated 4 work ows",
                    "- Wrote speci cations for 2 teams", "Honors & Certi"]
    whole = _by_id(cv_ats.evaluate(lines, dict(PDF)))["whole_words"]
    assert whole["status"] == "fail" and "certi cations" in whole["detail"] and "fi" in whole["detail"]


def test_intact_words_raise_nothing():
    assert cv_ats.broken_words(["Certifications in artificial intelligence", "Specific, significant workflows",
                                "Profile", "The results and the projects", "Trading and testing"]) == []
    assert _by_id(cv_ats.evaluate(GOOD, dict(PDF)))["whole_words"]["status"] == "ok"


def test_common_heading_variants_are_recognised():
    expected = {"TECHNICAL SKILLS": "skills", "Compétences Techniques": "skills", "KEY PROJECTS & ENGINEERING WORK": "projects",
                "Projets Significatifs": "projects", "Profil Professionnel": "summary", "Expérience Professionnelle": "experience",
                "Internships": "experience", "FORMA TION": "education", "Parcours Académique": "education",
                "Leadership & Activities": "activities", "Honors & Awards": "awards", "CER TIFICA TIONS": "certifications"}
    for line, section in expected.items():
        assert cv_ats._heading(line) == section, line


def test_ordinary_lines_are_not_mistaken_for_headings():
    for line in ("MOHAMED HEDDA", "XGBoost", "- Built projects for 3 teams", "Responsable RH et Formation — Club ENSIT Junior",
                 "I have experience in data engineering and education projects.", "Skills in Python, 2024",
                 "Problem Solving & Critical Thinking"):
        assert cv_ats._heading(line) is None, line


def test_a_missing_section_lists_the_headings_that_were_found():
    lines = [l for l in GOOD if l != "EDUCATION"]
    detail = _by_id(cv_ats.evaluate(lines, dict(PDF)))["section_education"]["detail"]
    assert "“SKILLS”" in detail and "“EXPERIENCE”" in detail
