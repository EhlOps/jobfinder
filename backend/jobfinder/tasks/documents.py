"""worker-io's document handlers (#51): parse_document reads an upload, fetch_link fetches a profile link,
off the api. Both write the text and the document's status (ready or error) into the store; a file that
can't be read or a link that can't be fetched is recorded there, so the task itself still finishes. If the
task fails for good anyway, tasks.failures marks the item error.
tasks.io imports this module, so worker-io registers them."""
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from jobfinder.models import Task
from jobfinder.tasks.brokers import io_broker
from jobfinder.tasks.runtime import ledger_task


class DocumentArgs(BaseModel):
    doc_id: str = ""  # an unknown id finds nothing in the store and is skipped


@ledger_task(io_broker, "parse_document", payload=DocumentArgs, ai=False)
async def parse_document(db: AsyncSession, ai, task: Task, p: DocumentArgs) -> dict:
    from jobfinder.context import default_context
    from jobfinder.services import documents

    return await documents.parse(default_context().store, task.user_id, p.doc_id)


@ledger_task(io_broker, "fetch_link", payload=DocumentArgs, ai=False)
async def fetch_link(db: AsyncSession, ai, task: Task, p: DocumentArgs) -> dict:
    from jobfinder.context import default_context
    from jobfinder.services import links

    return await links.fetch(default_context().store, task.user_id, p.doc_id)
