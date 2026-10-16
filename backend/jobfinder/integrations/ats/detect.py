"""Spot a known ATS behind a custom careers domain from the page's HTML.

Some platforms (Phenom, Eightfold, SuccessFactors RMK, ...) serve boards on the company's own domain, so the
URL alone says nothing; the page source does ("phenompeople", "rmkcdn", ...). Each detector looks at one
platform and returns the (ats, slug) to fetch, or None. Register new detectors in HTML_DETECTORS.
"""
from collections.abc import Callable

from jobfinder.integrations.ats.eightfold import detect_html as detect_eightfold
from jobfinder.integrations.ats.icims import detect_icims
from jobfinder.integrations.ats.oracle_hcm import detect_ce_page
from jobfinder.integrations.ats.phenom import detect_phenom
from jobfinder.integrations.ats.successfactors import detect_successfactors

Detector = Callable[[str, str], tuple[str, str] | None]  # (page_url, html) -> (ats, slug) | None

HTML_DETECTORS: list[Detector] = [
    detect_phenom,
    detect_ce_page,  # Oracle Recruiting Cloud
    detect_successfactors,
    detect_icims,
    detect_eightfold,
]


def detect_board(page_url: str, html: str) -> tuple[str, str] | None:
    for detect in HTML_DETECTORS:
        try:
            found = detect(page_url, html)
        except Exception:  # one broken detector must not stop the others
            found = None
        if found:
            return found
    return None
