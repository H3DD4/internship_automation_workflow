"""Favorites: star the companies you care about, then send them together."""

import db


def _star(client, ids, on=True):
    return client.post("/api/favorite", json={"app_ids": ids, "favorite": on},
                       headers=client.origin).get_json()


def test_starring_and_unstarring(client, make_app):
    app_id = make_app(email="a@x.com")
    data = _star(client, [app_id])
    assert data["ok"] and data["favorites_total"] == 1
    assert db.get_application_by_id(app_id)["favorite"] == 1
    data = _star(client, [app_id], on=False)
    assert data["favorites_total"] == 0
    assert db.get_application_by_id(app_id)["favorite"] == 0


def test_starring_does_not_reorder_the_table(client, make_app):
    """The table sorts by updated_at; a star isn't a change to the company."""
    app_id = make_app(email="a@x.com")
    before = db.get_application_by_id(app_id)["updated_at"]
    _star(client, [app_id])
    assert db.get_application_by_id(app_id)["updated_at"] == before


def test_many_can_be_starred_at_once(client, make_app):
    ids = [make_app(email=f"{i}@x.com") for i in range(3)]
    assert _star(client, ids)["changed"] == 3


def test_bad_input_is_refused(client):
    assert client.post("/api/favorite", json={"app_ids": []}, headers=client.origin).status_code == 400
    assert client.post("/api/favorite", json={"app_ids": ["x"]}, headers=client.origin).status_code == 400


def test_the_favorites_tab_shows_only_starred_companies(client, make_app):
    starred = make_app(company="Starred Co", email="s@x.com")
    make_app(company="Other Co", email="o@x.com")
    _star(client, [starred])
    page = client.get("/?status=favorites").data.decode()
    assert "Starred Co" in page and "Other Co" not in page
    assert 'id="send-favorites-btn"' in page


def test_every_row_has_a_star_in_every_tab(client, make_app):
    make_app(company="Sent Co", email="s@x.com", status="sent")
    page = client.get("/").data.decode()
    assert 'class="fav-btn' in page   # even a sent company can be starred


def test_send_favorites_offers_only_ready_ones_across_all_pages(client, make_app):
    """A favorite that's already sent, or still being prepared, is left out —
    and nothing is limited to the 50 rows on screen."""
    ready = [make_app(email=f"r{i}@x.com", status="ready") for i in range(55)]
    sent = make_app(email="sent@x.com", status="sent")
    pending = make_app(email="p@x.com", status="pending", subject=None, body=None)
    _star(client, ready + [sent, pending])

    data = client.get("/api/favorites/sendable").get_json()
    ids = {r["id"] for r in data["recipients"]}
    assert ids == set(ready)
    counts = db.count_favorites()
    assert counts == {"total": 57, "sendable": 55}


def test_the_live_refresh_carries_favorite_counts(client, make_app):
    _star(client, [make_app(email="a@x.com")])
    data = client.get("/api/overview").get_json()
    assert data["favorites_total"] == 1 and data["favorites_sendable"] == 1


def test_the_company_page_has_a_favorite_toggle(client, make_app):
    app_id = make_app(email="a@x.com")
    assert "☆ Add to favorites" in client.get(f"/company/{app_id}").data.decode()
    _star(client, [app_id])
    assert "★ Favorite" in client.get(f"/company/{app_id}").data.decode()


def test_an_old_database_gains_the_favorite_column(isolated):
    """Upgrading must not break: the column is added by the migration."""
    conn = db.get_connection()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
    conn.close()
    assert "favorite" in cols
