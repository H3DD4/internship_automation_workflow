"""The shared Ntern list, the user's own named lists, choosing what a scan
works through, and where each company is."""

import io

import db
import pipeline
import runs
import utils
from agents import research_agent as ra


def _ntern(*emails):
    return db.replace_catalog([{"email": e, "company_name": e.split("@")[1], "website": "", "contact_name": ""}
                               for e in emails])


def test_the_ntern_list_is_shared_and_replaced_whole(isolated):
    assert _ntern("a@one.fr", "b@two.ch", "a@one.fr") == 2
    assert [r[1] for r in db.catalog_rows()] == ["a@one.fr", "b@two.ch"]
    assert all(r[4] == "ntern" for r in db.catalog_rows())
    assert _ntern("c@three.de") == 1 and db.catalog_count() == 1


def test_a_new_user_can_scan_the_ntern_list_right_away(user_id):
    _ntern("a@one.fr", "b@two.ch")
    rows, skipped = pipeline.select_rows(user_id)
    assert [r[1] for r in rows] == ["a@one.fr", "b@two.ch"] and skipped == 0


def test_own_lists_come_first_and_an_address_counts_once(user_id, data):
    _ntern("a@one.fr", "b@two.ch")
    data.add_companies([{"email": "b@two.ch", "company_name": "Two"}, {"email": "z@mine.com"}], "Swiss banks")
    rows = pipeline.source_rows(user_id)
    assert [(r[1], r[4]) for r in rows] == [("b@two.ch", "Swiss banks"), ("z@mine.com", "Swiss banks"),
                                            ("a@one.fr", "ntern")]


def test_a_scan_works_only_through_the_ticked_lists(user_id, data):
    _ntern("a@one.fr")
    data.add_companies([{"email": "m@mine.com"}])
    data.add_companies([{"email": "s@bank.ch"}], "Swiss banks")
    assert [r[1] for r in pipeline.source_rows(user_id, ["ntern"])] == ["a@one.fr"]
    assert [r[1] for r in pipeline.source_rows(user_id, ["Swiss banks"])] == ["s@bank.ch"]
    assert [r[1] for r in pipeline.source_rows(user_id, [""])] == ["m@mine.com"]


def test_the_run_form_offers_the_lists_and_the_run_keeps_the_choice(client, user_id, data, monkeypatch):
    _ntern("a@one.fr")
    data.add_companies([{"email": "s@bank.ch"}], "Swiss banks")
    page = client.get("/").data.decode()
    assert "Ntern list" in page and "Swiss banks" in page
    monkeypatch.setattr("dashboard.app._setup_state", lambda: {"prep_ready": True})
    client.post("/run", headers=client.origin, data={"csrf_token": client.csrf, "sources_shown": "1",
                                                     "source": ["ntern"]})
    assert runs.sources_of(runs.latest(user_id)) == ["ntern"]


def test_scanning_nothing_is_refused(client, user_id, monkeypatch):
    monkeypatch.setattr("dashboard.app._setup_state", lambda: {"prep_ready": True})
    client.post("/run", headers=client.origin, data={"csrf_token": client.csrf, "sources_shown": "1"})
    assert runs.latest(user_id) is None


def test_importing_into_a_named_list_and_removing_only_that_one(client, data):
    def upload(name, body):
        return client.post("/api/companies/import", headers=client.origin, content_type="multipart/form-data",
                           data={"companies_file": (io.BytesIO(body), "c.csv"), "mode": "append", "source": name})
    assert upload("Swiss banks", b"email\nhr@bank.ch\n").get_json()["ok"]
    assert upload("", b"email\nme@mine.com\n").get_json()["ok"]
    names = {s["label"]: s["count"] for s in data.company_sources()}
    assert names == {"My list": 1, "Swiss banks": 1}
    client.post("/api/companies/clear", headers=client.origin, json={"source": "Swiss banks"})
    assert {s["label"] for s in data.company_sources()} == {"My list"}


def test_ntern_is_a_reserved_list_name():
    assert db.clean_source_name("Ntern") == "" and db.clean_source_name("  Lyon   startups ") == "Lyon startups"


def test_each_company_shows_where_it_came_from_and_where_it_is(client, data):
    app_id = data.get_or_create_application("Acme", "jobs@acme.ch", "https://acme.ch", source="ntern")
    data.update_application(app_id, status="ready", subject="S", body="B", location="Genève, Switzerland")
    rows = client.get("/api/overview").get_json()["rows_html"]
    assert "src-tag--ntern" in rows and ">Ntern<" in rows and "Genève, Switzerland" in rows


def test_the_first_source_recorded_is_kept(data):
    app_id = data.get_or_create_application("Acme", "a@acme.fr", "", source="Lyon")
    data.get_or_create_application("Acme", "a@acme.fr", "", source="ntern")
    assert data.get_application_by_id(app_id)["source"] == "Lyon"


def test_the_country_comes_from_the_web_address_first():
    assert utils.country_from_domain("https://acme.ch", "x@acme.fr") == "Switzerland"
    assert utils.country_from_domain("https://acme.com", "x@acme.fr") == "France"
    assert utils.location_text("https://acme.com", "a@acme.com", {"city": "Lyon", "country": "France"}) == "Lyon, France"
    assert utils.location_text("https://acme.de", "", {"country": "France"}) == "Germany"


def test_a_city_or_country_the_site_never_names_is_dropped():
    assert ra.verify_location("Genève", "Switzerland", "Nos bureaux à Genève, Suisse.") == ("Genève", "Switzerland")
    assert ra.verify_location("", "Switzerland", "Wir sind ein Team in der Schweiz.") == ("", "Switzerland")
    assert ra.verify_location("Paris", "France", "We are based in Lyon.") == ("", "")


def test_the_admin_can_fill_the_ntern_list_from_a_file_or_an_account(admin_client, make_user):
    reply = admin_client.post("/admin/platform", headers=admin_client.origin, content_type="multipart/form-data",
                              data={"csrf_token": admin_client.csrf, "action": "ntern_upload",
                                    "ntern_file": (io.BytesIO(b"email\na@x.fr\nb@y.ch\n"), "n.csv")})
    assert reply.status_code == 302 and db.catalog_count() == 2
    owner = make_user("owner@example.com")
    db.for_user(owner).add_companies([{"email": "c@z.de"}])
    admin_client.post("/admin/platform", headers=admin_client.origin,
                      data={"csrf_token": admin_client.csrf, "action": "ntern_from_user", "user_id": str(owner)})
    assert [r[1] for r in db.catalog_rows()] == ["c@z.de"]


def test_users_cannot_change_the_ntern_list(client):
    client.post("/admin/platform", headers=client.origin, content_type="multipart/form-data",
                data={"csrf_token": client.csrf, "action": "ntern_upload",
                      "ntern_file": (io.BytesIO(b"email\na@x.fr\n"), "n.csv")})
    assert db.catalog_count() == 0
