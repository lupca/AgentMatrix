def test_create_knowledge_item(client):
    payload = {
        "title": "Architecture Overview",
        "category": "design",
        "content": "Detailed overview of Control Tower V2 architecture",
        "tags": ["architecture", "v2"],
        "project": "proj-ct",
        "author": "architect-agent"
    }
    response = client.post("/api/knowledge", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["id"].startswith("k-")
    assert data["title"] == "Architecture Overview"
    assert data["category"] == "design"
    assert data["content"] == "Detailed overview of Control Tower V2 architecture"
    assert data["tags"] == ["architecture", "v2"]
    assert data["project"] == "proj-ct"
    assert data["author"] == "architect-agent"


def test_create_knowledge_item_with_custom_id(client):
    payload = {
        "id": "k-custom-123",
        "title": "Custom ID Knowledge",
        "category": "guidelines"
    }
    res1 = client.post("/api/knowledge", json=payload)
    assert res1.status_code == 201
    assert res1.json()["id"] == "k-custom-123"

    # Duplicate ID should fail
    res2 = client.post("/api/knowledge", json=payload)
    assert res2.status_code == 400
    assert "already exists" in res2.json()["detail"]


def test_get_knowledge_items_filtering_and_search(client):
    client.post("/api/knowledge", json={"title": "Python Style Guide", "category": "coding", "project": "backend"})
    client.post("/api/knowledge", json={"title": "React Best Practices", "category": "coding", "project": "frontend"})
    client.post("/api/knowledge", json={"title": "Deployment Runbook", "category": "ops", "project": "backend"})

    # Filter by category
    res_coding = client.get("/api/knowledge?category=coding")
    assert res_coding.status_code == 200
    assert len(res_coding.json()) == 2

    # Filter by project
    res_backend = client.get("/api/knowledge?project=backend")
    assert res_backend.status_code == 200
    assert len(res_backend.json()) == 2

    # Search term
    res_search = client.get("/api/knowledge?search=Runbook")
    assert res_search.status_code == 200
    assert len(res_search.json()) == 1
    assert res_search.json()[0]["title"] == "Deployment Runbook"


def test_get_knowledge_item_by_id(client):
    res_create = client.post("/api/knowledge", json={"id": "k-find-me", "title": "Find Me"})
    assert res_create.status_code == 201

    res = client.get("/api/knowledge/k-find-me")
    assert res.status_code == 200
    assert res.json()["title"] == "Find Me"

    res_404 = client.get("/api/knowledge/k-nonexistent")
    assert res_404.status_code == 404


def test_patch_knowledge_item(client):
    client.post("/api/knowledge", json={"id": "k-patch-me", "title": "Old Title", "category": "draft"})

    res = client.patch("/api/knowledge/k-patch-me", json={"title": "New Title", "category": "final"})
    assert res.status_code == 200
    assert res.json()["title"] == "New Title"
    assert res.json()["status"] if "status" in res.json() else True
    assert res.json()["category"] == "final"


def test_delete_knowledge_item(client):
    client.post("/api/knowledge", json={"id": "k-del-me", "title": "To Delete"})

    del_res = client.delete("/api/knowledge/k-del-me")
    assert del_res.status_code == 204

    get_res = client.get("/api/knowledge/k-del-me")
    assert get_res.status_code == 404
