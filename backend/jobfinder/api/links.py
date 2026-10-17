"""Profile links: GitHub, portfolio, LinkedIn and the like, fetched where we may."""
from fastapi import APIRouter

from jobfinder.api.deps import CurrentUser, Store
from jobfinder.schemas.links import ProfileLinkIn, ProfileLinkOut
from jobfinder.services import links as service

router = APIRouter(prefix="/api/links", tags=["links"])


@router.get("", response_model=list[ProfileLinkOut])
async def list_links(user: CurrentUser, store: Store):
    return [ProfileLinkOut.of(m) for m in service.list_links(store, user.id)]


@router.post("", response_model=ProfileLinkOut, status_code=201)
async def add_link(body: ProfileLinkIn, user: CurrentUser, store: Store):
    return ProfileLinkOut.of(await service.add(store, user.id, body.kind, str(body.url)))


@router.post("/{link_id}/refresh", response_model=ProfileLinkOut)
async def refresh_link(link_id: str, user: CurrentUser, store: Store):
    return ProfileLinkOut.of(await service.refresh(store, user.id, link_id))


@router.delete("/{link_id}", status_code=204)
async def delete_link(link_id: str, user: CurrentUser, store: Store):
    service.delete(store, user.id, link_id)
