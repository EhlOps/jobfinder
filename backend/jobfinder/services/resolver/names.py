"""Readable company names from a board slug or a domain. Shared by the resolver and its rungs."""
import re

from jobfinder.core.urls import registrable_domain


def name_from_slug(ats: str, slug: str) -> str:
    """A readable company name from the board slug ('acme-robotics' -> 'Acme Robotics'). Admins can rename."""
    if ats == "oracle_hcm":  # "eeho.fa.us2__CX_45001": only the pod name says anything about the company
        return name_from_slug("", slug.split(".")[0])
    if "__" in slug:  # composite custom-domain key "careers.acme.com__us__en" -> name from the domain
        return name_from_domain(registrable_domain("https://" + slug.split("__")[0]))
    base = slug.split(".")[0] if ats == "workday" else slug
    if ats == "successfactors":  # "careers.acme.com__brand" -> the brand, else the domain's name
        host, _, brand = slug.partition("__")
        base = brand or registrable_domain(f"https://{host}/").split(".")[0]
    if ats == "icims":  # "careers-acme-corp" -> "acme-corp" (the label's prefix is the portal kind)
        base = slug.split("-", 1)[-1]
    if ats == "eightfold":  # "host__domain": the tenant domain names the company
        base = slug.rsplit("__", 1)[-1].split(".")[0]
    words = [w for w in re.split(r"[-_.\s]+", base) if w]
    return (" ".join(w if w.isupper() else w.capitalize() for w in words) or slug)[:200]


def name_from_domain(domain: str) -> str:
    """'acme-robotics.com' -> 'Acme Robotics'."""
    return name_from_slug("", domain.split(".")[0]) if domain else "Unknown company"
