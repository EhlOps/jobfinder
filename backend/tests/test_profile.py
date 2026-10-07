async def test_profile_versioning(authed):
    p = (await authed.get("/api/profile")).json()
    assert p["version"] == 1 and p["status"] == {}

    r = await authed.put("/api/profile/status", json={"is_new_grad": True, "salary_min": 90000})
    assert r.json()["version"] == 2 and r.json()["status"]["salary_min"] == 90000

    r = await authed.put("/api/profile/background", json={"skills": ["python"]})
    assert r.json()["version"] == 3

    r = await authed.post("/api/profile/facts", json={"question": "Used k8s?", "answer": "Yes"})
    assert r.status_code == 201
    assert (await authed.get("/api/profile")).json()["version"] == 4
    assert len((await authed.get("/api/profile/facts")).json()) == 1


async def test_status_validation(authed):
    r = await authed.put("/api/profile/status", json={"prestige_preference": 9})
    assert r.status_code == 422


async def test_users_are_isolated(client, make_account):
    await make_account("a@x.com", "password-aaaa")
    await client.post("/api/profile/facts", json={"question": "q", "answer": "a"})
    await client.post("/api/auth/logout")
    await make_account("b@x.com", "password-bbbb")
    assert (await client.get("/api/profile/facts")).json() == []
