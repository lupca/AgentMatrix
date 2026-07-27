def test_create_project(client):
    payload = {
        "id": "proj-alpha",
        "name": "Project Alpha",
        "description": "First test project",
        "status": "active"
    }
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["id"] == "proj-alpha"
    assert data["name"] == "Project Alpha"
    assert data["description"] == "First test project"
    assert data["status"] == "active"


def test_create_duplicate_project_id_fails(client):
    payload = {
        "id": "proj-dup",
        "name": "Duplicate Project"
    }
    res1 = client.post("/api/projects", json=payload)
    assert res1.status_code == 201

    res2 = client.post("/api/projects", json=payload)
    assert res2.status_code == 400
    assert "already exists" in res2.json()["detail"]


def test_get_projects_filtering_and_pagination(client):
    client.post("/api/projects", json={"id": "p1", "name": "P1", "status": "active"})
    client.post("/api/projects", json={"id": "p2", "name": "P2", "status": "archived"})
    client.post("/api/projects", json={"id": "p3", "name": "P3", "status": "active"})

    res = client.get("/api/projects?status=active")
    assert res.status_code == 200
    projects = res.json()
    assert len(projects) == 2

    res_page = client.get("/api/projects?limit=1&offset=0")
    assert res_page.status_code == 200
    assert len(res_page.json()) == 1


def test_get_project_by_id(client):
    client.post("/api/projects", json={"id": "find-me", "name": "Find Me"})

    res = client.get("/api/projects/find-me")
    assert res.status_code == 200
    assert res.json()["name"] == "Find Me"

    res_404 = client.get("/api/projects/nonexistent")
    assert res_404.status_code == 404


def test_patch_project(client):
    client.post("/api/projects", json={"id": "patch-me", "name": "Original Name"})

    res = client.patch("/api/projects/patch-me", json={"name": "Updated Name", "status": "inactive"})
    assert res.status_code == 200
    assert res.json()["name"] == "Updated Name"
    assert res.json()["status"] == "inactive"


def test_delete_project(client):
    client.post("/api/projects", json={"id": "del-me", "name": "To Delete"})

    del_res = client.delete("/api/projects/del-me")
    assert del_res.status_code == 204

    get_res = client.get("/api/projects/del-me")
    assert get_res.status_code == 404


def test_archive_project_via_patch_is_a_status_change_not_a_delete(client):
    client.post("/api/projects", json={"id": "archive-me", "name": "Archive Me"})

    res = client.patch("/api/projects/archive-me", json={"status": "archived"})
    assert res.status_code == 200
    assert res.json()["status"] == "archived"

    # Still readable — archiving never hard-deletes the row.
    get_res = client.get("/api/projects/archive-me")
    assert get_res.status_code == 200
    assert get_res.json()["status"] == "archived"


def test_build_graph(client):
    res = client.post("/api/projects/proj-graph-test/build-graph")
    assert res.status_code == 200
    assert res.json() == {"status": "building", "project_id": "proj-graph-test"}


def test_create_and_patch_project_autonomy_policy(client):
    policy = {"autonomy": "auto", "auto_max_risk": "normal", "auto_max_rounds": 4}
    res = client.post(
        "/api/projects",
        json={"id": "auto-policy-proj", "name": "Policy Proj", "autonomy_policy": policy},
    )
    assert res.status_code == 201
    assert res.json()["autonomy_policy"] == policy

    updated_policy = {"autonomy": "plan-only", "auto_max_risk": "low", "auto_max_rounds": 2}
    patch_res = client.patch(
        "/api/projects/auto-policy-proj",
        json={"autonomy_policy": updated_policy},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["autonomy_policy"] == updated_policy


