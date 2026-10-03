"""The first-time setup: a brand-new account is guided step by step, can
leave and come back where it stopped, and is never trapped."""

import io
import json
import zipfile

import db
import pipeline
import profiles
import runs
from dashboard import onboarding_routes as ob
from test_templates_and_profiles import FACTS
from user_config import UserConfig


def _docx(lines):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
    return buffer.getvalue()


CV_LINES = ["Lina Ben Salah", "lina@example.com +216 22 333 444 linkedin.com/in/lina", "EDUCATION",
            "Engineering degree, ENSIT, 2022 - 2027", "SKILLS", "Python, SQL, React, Docker, Linux, Git, FastAPI",
            "EXPERIENCE", "- Built a pipeline processing 1200 records a day in 2025",
            "- Reduced report time by 40% for 3 teams", "- Designed 12 dashboards"] + \
           [f"- Automated {n} weekly checks across the data platform used by the whole department" for n in range(20, 45)]


def _step(client, step=None):
    return client.get("/welcome" + (f"?step={step}" if step else "")).data.decode()


def test_a_new_account_starts_in_the_guided_setup(new_client):
    reply = new_client.get("/")
    assert reply.status_code == 302 and reply.headers["Location"].endswith("/welcome")
    page = _step(new_client)
    assert "What are you looking for?" in page and "1 of 8 done" in page     # the account already counts
    for title in ("About you", "Your CV", "Where", "AI key", "Your profile", "Email style", "Your mailbox"):
        assert title in page


def test_an_account_already_in_use_is_left_alone(client):
    assert client.get("/").status_code == 200


def test_the_administrator_never_sees_the_setup(admin_client):
    assert admin_client.get("/").status_code == 200


def test_step_one_saves_the_name_and_the_dates(new_client, user_id):
    reply = new_client.post("/welcome/you", headers=new_client.origin, data={
        "csrf_token": new_client.csrf, "full_name": "Lina Ben Salah", "kind": "end_of_study",
        "start_month": "3", "start_year": "2027", "duration": "6", "open_to_hire": "on"})
    assert reply.headers["Location"].endswith("step=cv")
    cfg = UserConfig(user_id)
    assert cfg.get("YOUR_NAME") == "Lina Ben Salah"
    assert ob.load_state(cfg)["dates"] == {"kind": "end_of_study", "month": 3, "year": 2027, "duration": 6,
                                           "open_to_hire": True}


def test_a_missing_name_is_asked_again(new_client):
    reply = new_client.post("/welcome/you", headers=new_client.origin, data={
        "csrf_token": new_client.csrf, "full_name": " ", "start_month": "3", "start_year": "2027"})
    assert reply.headers["Location"].endswith("step=you")


def test_uploading_a_cv_stores_it_and_shows_the_check(new_client, user_id):
    reply = new_client.post("/api/cv/check", headers=new_client.origin, content_type="multipart/form-data",
                            data={"cv_file": (io.BytesIO(_docx(CV_LINES)), "cv.docx")}).get_json()
    assert reply["ok"] and reply["report"]["score"] >= 70 and not reply["report"]["blocking"]
    assert UserConfig(user_id).cv_info()["name"].endswith(".docx")
    page = _step(new_client, "cv")
    assert "ats-ring" in page and "How a machine reads your CV" in page and "lina@example.com" in page


def test_an_unreadable_cv_blocks_the_next_step(new_client):
    new_client.post("/api/cv/check", headers=new_client.origin, content_type="multipart/form-data",
                    data={"cv_file": (io.BytesIO(_docx(["Hi"])), "cv.docx")})
    page = _step(new_client, "cv")
    assert "can't read this file" in page and 'aria-disabled="true"' in page


def test_target_places_are_saved_or_skipped(new_client, user_id):
    new_client.post("/welcome/places", headers=new_client.origin,
                    data={"csrf_token": new_client.csrf, "country": ["France", "Switzerland", "Atlantis"]})
    assert ob.target_countries(UserConfig(user_id)) == ["France", "Switzerland"]
    reply = new_client.post("/welcome/skip/places", headers=new_client.origin, data={"csrf_token": new_client.csrf})
    assert reply.headers["Location"].endswith("step=ai")


def test_required_steps_cannot_be_skipped(new_client, user_id):
    new_client.post("/welcome/skip/cv", headers=new_client.origin, data={"csrf_token": new_client.csrf})
    assert "cv" not in (ob.load_state(UserConfig(user_id)).get("skipped") or [])


def test_the_setup_resumes_at_the_first_open_step(new_client, user_id):
    cfg = UserConfig(user_id)
    ob.save_state(cfg, dates={"kind": "internship", "month": 3, "year": 2027, "duration": None, "open_to_hire": False})
    cfg.save_cv("cv.docx", _docx(CV_LINES))
    assert "Where would you like to work?" in _step(new_client)


def test_skip_setup_leads_to_the_app_and_stays_there(new_client):
    reply = new_client.post("/welcome/finish", headers=new_client.origin, data={"csrf_token": new_client.csrf})
    assert reply.headers["Location"].endswith("/")
    assert new_client.get("/").status_code == 200


def test_the_profile_step_shows_what_was_found(new_client, user_id):
    profiles.apply_template(user_id, FACTS, "specialist")
    page = _step(new_client, "profile")
    assert "what we found in your CV" in page and "Looks right" in page


def test_companies_in_the_target_places_are_marked(client, data, user_id):
    UserConfig(user_id).set_many({"TARGET_COUNTRIES": "France|Switzerland"})
    for name, email, where in (("Fit", "a@fit.ch", "Genève, Switzerland"), ("Far", "a@far.de", "Germany")):
        app_id = data.get_or_create_application(name, email, "")
        data.update_application(app_id, status="ready", subject="S", body="B", location=where)
    rows = client.get("/api/overview").get_json()["rows_html"]
    assert rows.count("Your target") == 1 and "loc-tag--fit" in rows


def test_a_scan_can_keep_to_the_target_places(user_id, data):
    db.replace_catalog([{"email": e} for e in ("a@one.de", "b@two.com", "c@three.fr", "d@four.ch")])
    rows, _ = pipeline.select_rows(user_id, countries=["France", "Switzerland"])
    # In the target first, then unknown countries; Germany is left out.
    assert [r[1] for r in rows] == ["c@three.fr", "d@four.ch", "b@two.com"]


def test_the_run_form_remembers_the_target_places(client, user_id, monkeypatch):
    UserConfig(user_id).set_many({"TARGET_COUNTRIES": "France"})
    db.replace_catalog([{"email": "a@one.fr"}])
    assert "Only my target places" in client.get("/").data.decode()
    monkeypatch.setattr("dashboard.app._setup_state", lambda: {"prep_ready": True})
    client.post("/run", headers=client.origin, data={"csrf_token": client.csrf, "sources_shown": "1",
                                                     "source": ["ntern"], "targets_only": "on"})
    assert runs.countries_of(runs.latest(user_id)) == ["France"]


def test_the_profile_page_shows_the_cv_check_and_the_places(client, user_id):
    UserConfig(user_id).save_cv("cv.docx", _docx(CV_LINES))
    page = client.get("/profile").data.decode()
    assert "On this page" in page and "how hiring software reads your CV" in page and "Target places" in page
    client.post("/welcome/places", headers=client.origin,
                data={"csrf_token": client.csrf, "country": ["Belgium"], "return": "settings"})
    assert UserConfig(user_id).get("TARGET_COUNTRIES") == "Belgium"
