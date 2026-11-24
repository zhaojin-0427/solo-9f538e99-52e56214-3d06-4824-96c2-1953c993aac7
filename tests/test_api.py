"""HTTP-level behaviour: input by record number, correction, and statistics."""

from __future__ import annotations


def put(client, rid, day, district, tag):
    return client.post(
        "/observations",
        json={
            "recognition_id": rid,
            "observed_on": day,
            "district": district,
            "ear_tag": tag,
        },
    )


def stats(client, district, start, end):
    return client.get(
        f"/stats/districts/{district}",
        params={"start_date": start, "end_date": end},
    )


# ---------------------------------------------------------------------------
# Record entry and correction
# ---------------------------------------------------------------------------


def test_create_record_and_read_it_back(client):
    response = put(client, "REC-1", "2026-03-01", "北沟片区", "ET-1")
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "created"
    assert body["record"] == {
        "recognition_id": "REC-1",
        "observed_on": "2026-03-01",
        "district": "北沟片区",
        "ear_tag": "ET-1",
    }

    fetched = client.get("/observations/REC-1")
    assert fetched.status_code == 200
    assert fetched.json()["district"] == "北沟片区"


def test_resubmitting_same_record_number_corrects_it(client):
    put(client, "REC-1", "2026-03-01", "北沟片区", "ET-1")
    response = put(client, "REC-1", "2026-03-02", "南坡片区", "ET-9")
    assert response.status_code == 200
    assert response.json()["action"] == "updated"

    listing = client.get("/observations").json()
    assert listing["total"] == 1  # corrected in place, not duplicated
    assert listing["records"][0]["district"] == "南坡片区"
    assert listing["records"][0]["ear_tag"] == "ET-9"
    assert listing["records"][0]["observed_on"] == "2026-03-02"


def test_patch_corrects_only_given_fields(client):
    put(client, "REC-1", "2026-03-01", "北沟片区", "ET-1")
    response = client.patch(
        "/observations/REC-1",
        json={"observed_on": "2026-03-05"},
    )
    assert response.status_code == 200
    record = response.json()["record"]
    assert record["observed_on"] == "2026-03-05"
    assert record["district"] == "北沟片区"
    assert record["ear_tag"] == "ET-1"


def test_patch_requires_at_least_one_field(client):
    put(client, "REC-1", "2026-03-01", "北沟片区", "ET-1")
    response = client.patch("/observations/REC-1", json={})
    assert response.status_code == 422


def test_patch_unknown_record_is_404(client):
    response = client.patch("/observations/NOPE", json={"district": "北沟片区"})
    assert response.status_code == 404


def test_put_with_path_id(client):
    response = client.put(
        "/observations/REC-7",
        json={
            "observed_on": "2026-03-01",
            "district": "北沟片区",
            "ear_tag": "ET-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["record"]["recognition_id"] == "REC-7"


# ---------------------------------------------------------------------------
# Statistics semantics
# ---------------------------------------------------------------------------


def test_same_day_repeats_count_as_one_day(client):
    put(client, "A1", "2026-03-01", "北沟片区", "ET-1")
    put(client, "A2", "2026-03-01", "北沟片区", "ET-1")
    put(client, "A3", "2026-03-01", "北沟片区", "ET-1")

    body = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    assert body["one_day_count"] == 1
    assert body["multi_day_count"] == 0
    # The query read a single summary record, not three identifications.
    assert body["summary_rows_used"] == 1


def test_two_days_move_tag_into_multi_day_bucket(client):
    put(client, "A1", "2026-03-01", "北沟片区", "ET-1")
    put(client, "A2", "2026-03-01", "北沟片区", "ET-1")
    put(client, "B1", "2026-03-04", "北沟片区", "ET-1")
    put(client, "C1", "2026-03-04", "北沟片区", "ET-2")

    body = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    assert body["one_day_count"] == 1  # ET-2
    assert body["multi_day_count"] == 1  # ET-1
    assert body["total_individuals"] == 2


def test_range_includes_both_endpoints(client):
    put(client, "A", "2026-03-01", "北沟片区", "ET-1")
    put(client, "B", "2026-03-10", "北沟片区", "ET-1")

    inclusive = stats(client, "北沟片区", "2026-03-01", "2026-03-10").json()
    assert inclusive["multi_day_count"] == 1

    narrowed = stats(client, "北沟片区", "2026-03-02", "2026-03-10").json()
    assert narrowed["one_day_count"] == 1
    assert narrowed["multi_day_count"] == 0


def test_days_outside_range_are_excluded(client):
    put(client, "A", "2026-02-28", "北沟片区", "ET-1")
    put(client, "B", "2026-03-15", "北沟片区", "ET-1")
    put(client, "C", "2026-04-01", "北沟片区", "ET-1")

    body = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    assert body["one_day_count"] == 1
    assert body["multi_day_count"] == 0


def test_district_isolation(client):
    put(client, "A", "2026-03-01", "北沟片区", "ET-1")
    put(client, "B", "2026-03-02", "南坡片区", "ET-1")

    north = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    south = stats(client, "南坡片区", "2026-03-01", "2026-03-31").json()
    assert (north["one_day_count"], north["multi_day_count"]) == (1, 0)
    assert (south["one_day_count"], south["multi_day_count"]) == (1, 0)


def test_same_tag_in_two_districts_counts_per_district(client):
    put(client, "A", "2026-03-01", "北沟片区", "ET-1")
    put(client, "B", "2026-03-02", "南坡片区", "ET-1")

    all_stats = client.get(
        "/stats/districts",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    ).json()
    by_name = {d["district"]: d for d in all_stats["districts"]}
    assert by_name["北沟片区"]["one_day_count"] == 1
    assert by_name["南坡片区"]["one_day_count"] == 1
    assert all_stats["total_one_day_count"] == 2


def test_inverted_range_is_rejected(client):
    response = stats(client, "北沟片区", "2026-03-10", "2026-03-01")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Correction of a same-day repeat, then a date change (the requested self-test)
# ---------------------------------------------------------------------------


def test_rescheduling_one_of_two_same_day_captures(client):
    """ET-1 was captured twice on 2026-03-01; move one capture to 2026-03-05.

    Before: ET-1 has one day -> one-day bucket.
    After:  ET-1 has two days -> multi-day bucket, and it is still one
            individual, not two.
    """
    put(client, "A1", "2026-03-01", "北沟片区", "ET-1")
    put(client, "A2", "2026-03-01", "北沟片区", "ET-1")

    before = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    assert (before["one_day_count"], before["multi_day_count"]) == (1, 0)

    response = client.patch("/observations/A2", json={"observed_on": "2026-03-05"})
    assert response.status_code == 200
    assert response.json()["action"] == "updated"

    after = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    assert (after["one_day_count"], after["multi_day_count"]) == (0, 1)
    assert after["total_individuals"] == 1


def test_moving_the_only_capture_of_a_day_removes_that_day(client):
    put(client, "A1", "2026-03-01", "北沟片区", "ET-1")
    put(client, "A2", "2026-03-01", "北沟片区", "ET-1")

    # Moving both captures away from 03-01 must leave no trace of that day.
    client.patch("/observations/A1", json={"observed_on": "2026-03-02"})
    client.patch("/observations/A2", json={"observed_on": "2026-03-03"})

    body = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    assert body["multi_day_count"] == 1
    assert body["summary_rows_used"] == 1

    only_old_day = stats(client, "北沟片区", "2026-03-01", "2026-03-01").json()
    assert only_old_day["total_individuals"] == 0


def test_correcting_district_and_tag_moves_the_summary(client):
    put(client, "A", "2026-03-01", "北沟片区", "ET-1")
    put(client, "B", "2026-03-02", "北沟片区", "ET-1")

    client.patch(
        "/observations/B",
        json={"district": "南坡片区", "ear_tag": "ET-2"},
    )

    north = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    south = stats(client, "南坡片区", "2026-03-01", "2026-03-31").json()
    assert north["total_individuals"] == 1
    assert north["one_day_count"] == 1
    assert south["total_individuals"] == 1
    assert south["one_day_count"] == 1


def test_all_district_stats_matches_per_district_stats(client):
    put(client, "A", "2026-03-01", "北沟片区", "ET-1")
    put(client, "B", "2026-03-03", "北沟片区", "ET-1")
    put(client, "C", "2026-03-02", "南坡片区", "ET-2")
    put(client, "D", "2026-03-02", "南坡片区", "ET-2")

    overall = client.get(
        "/stats/districts",
        params={"start_date": "2026-03-01", "end_date": "2026-03-31"},
    ).json()
    north = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    south = stats(client, "南坡片区", "2026-03-01", "2026-03-31").json()

    by_name = {d["district"]: d for d in overall["districts"]}
    assert (
        by_name["北沟片区"]["one_day_count"],
        by_name["北沟片区"]["multi_day_count"],
    ) == (north["one_day_count"], north["multi_day_count"])
    assert (
        by_name["南坡片区"]["one_day_count"],
        by_name["南坡片区"]["multi_day_count"],
    ) == (south["one_day_count"], south["multi_day_count"])


def test_listing_filters(client):
    put(client, "A", "2026-03-01", "北沟片区", "ET-1")
    put(client, "B", "2026-03-05", "北沟片区", "ET-2")
    put(client, "C", "2026-03-09", "南坡片区", "ET-1")

    body = client.get(
        "/observations",
        params={
            "district": "北沟片区",
            "ear_tag": "ET-1",
            "start_date": "2026-03-01",
            "end_date": "2026-03-05",
        },
    ).json()
    assert body["total"] == 1
    assert body["records"][0]["recognition_id"] == "A"


def test_date_query_aliases(client):
    put(client, "A", "2026-03-01", "北沟片区", "ET-1")
    put(client, "B", "2026-03-05", "北沟片区", "ET-1")

    for start_key, end_key in (
        ("start_date", "end_date"),
        ("start", "end"),
        ("from", "to"),
    ):
        body = client.get(
            "/stats/districts/北沟片区",
            params={start_key: "2026-03-01", end_key: "2026-03-05"},
        ).json()
        assert body["multi_day_count"] == 1, (start_key, end_key)


def test_input_field_aliases(client):
    response = client.post(
        "/observations",
        json={
            "record_id": "ALIAS-1",
            "date": "2026-03-01",
            "area": "北沟片区",
            "tag": "ET-1",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["record"]["recognition_id"] == "ALIAS-1"

    patched = client.patch("/observations/ALIAS-1", json={"tag": "ET-2"})
    assert patched.status_code == 200
    assert patched.json()["record"]["ear_tag"] == "ET-2"


def test_stats_alias_endpoint(client):
    put(client, "A", "2026-03-01", "北沟片区", "ET-1")
    body = client.get(
        "/stats",
        params={"from": "2026-03-01", "to": "2026-03-31", "district": "北沟片区"},
    ).json()
    assert body["total_one_day_count"] == 1
    assert body["districts"][0]["district"] == "北沟片区"


def test_summary_with_days_on_both_sides_of_window(client):
    """A tag seen in January and March counts once when querying March only."""
    put(client, "A", "2026-01-10", "北沟片区", "ET-1")
    put(client, "B", "2026-03-10", "北沟片区", "ET-1")
    put(client, "C", "2026-03-10", "北沟片区", "ET-1")

    march = stats(client, "北沟片区", "2026-03-01", "2026-03-31").json()
    assert march["one_day_count"] == 1
    assert march["multi_day_count"] == 0
    assert march["summary_rows_used"] == 1

    full = stats(client, "北沟片区", "2026-01-01", "2026-03-31").json()
    assert full["one_day_count"] == 0
    assert full["multi_day_count"] == 1

    january = stats(client, "北沟片区", "2026-01-01", "2026-01-31").json()
    assert january["one_day_count"] == 1


def test_docs_page_and_openapi_available(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/observations" in spec["paths"]
    assert "/stats/districts/{district}" in spec["paths"]
    assert "/stats/districts" in spec["paths"]
