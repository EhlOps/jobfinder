async def test_healthz(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
