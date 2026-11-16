"""Time the embedding model on this machine (#126): texts per second for each side.

    cd backend && EMBED_MODEL_DIR=/path/to/bge-small-jobfinder-v2 uv run --extra embed \\
        python scripts/embed_throughput.py [--n 200] [--threads 2]

Postings are synthetic but posting-shaped: built with `job_doc`, so they hit its 1,500-char cap like
real ones; queries are `profile_queries`-shaped one-liners. One warm-up call is excluded from the timing.
Reads settings like any jobfinder command (backend/.env, so SECRET_KEY must be set): EMBED_MODEL,
EMBED_MODEL_DIR, EMBED_BATCH, and EMBED_THREADS unless --threads is given.
"""
import argparse
import random
import time

from jobfinder.core.config import settings
from jobfinder.integrations.embed import load_embedder
from jobfinder.services.matching.embed_text import job_doc

WORDS = ["design", "build", "operate", "scalable", "distributed", "services", "APIs", "data", "pipelines",
         "Python", "Go", "Kubernetes", "PostgreSQL", "Kafka", "observability", "reliability", "customers",
         "product", "engineers", "collaborate", "review", "mentor", "ship", "features", "own", "roadmap",
         "infrastructure", "cloud", "security", "testing", "performance"]
ROLES = ["Backend Engineer", "Data Engineer", "Platform Engineer", "ML Engineer", "Site Reliability Engineer"]


def _posting(rng: random.Random, i: int) -> str:
    bullets = "\n".join("- " + " ".join(rng.choices(WORDS, k=14)).capitalize() + "." for _ in range(18))
    body = (f"About us. {' '.join(rng.choices(WORDS, k=40))}.\n\nWhat you'll do\n{bullets}\n\n"
            f"Requirements\n{bullets}")
    return job_doc({"title": rng.choice(ROLES), "company_name": f"Company {i}", "description_text": body})


def _query(rng: random.Random) -> str:
    return f"{rng.choice(ROLES)}. Skills: {', '.join(rng.sample(WORDS, 8))}"


def _rate(label: str, fn, texts: list[str]) -> None:
    fn(texts[:4])  # warm-up: the first run allocates the session's buffers
    start = time.perf_counter()
    fn(texts)
    elapsed = time.perf_counter() - start
    avg = sum(map(len, texts)) / len(texts)
    print(f"{label:<14} {len(texts):>4} texts  avg {avg:>6.0f} chars  {elapsed:>7.2f} s  "
          f"{len(texts) / elapsed:>7.1f} texts/s")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--n", type=int, default=200, help="texts per side (at least 8)")
    p.add_argument("--threads", type=int, default=settings.embed_threads)
    args = p.parse_args()
    if args.n < 8:
        p.error("--n must be at least 8")

    embedder = load_embedder(threads=args.threads)
    print(f"{embedder.model} from {settings.embed_model_dir}: threads={args.threads} "
          f"batch={settings.embed_batch}")
    rng = random.Random(0)
    _rate("postings", embedder.embed_documents, [_posting(rng, i) for i in range(args.n)])
    _rate("queries", embedder.embed_queries, [_query(rng) for _ in range(args.n)])


if __name__ == "__main__":
    main()
