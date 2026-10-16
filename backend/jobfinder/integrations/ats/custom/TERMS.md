# Custom careers sites: terms and robots review (2026-10)

Reviewed 2026-10-14 for issue #18, decision 5 ("review robots.txt/terms, then build adapters only for
sites that allow it; record refusals with reasons"). The machine-readable copy of this table is
`CUSTOM_SITES` in `__init__.py`; keep the two in sync.

Rule applied (conservative): **REFUSE** if the terms forbid automated access/scraping, if robots.txt
disallows the listing endpoint we would need, or if the site blocks our identified crawler
(`JobFinder/0.1 (+personal job search)`). We never evade bot protection.

A refused site is never registered as an ATS and `board_from_url` never recognises it. A pasted link is
marked unresolved with the reason (rung `terms_refused`) when it is added, without queueing a resolve,
and `safe_fetch` refuses every hop that lands on a refused site, so a shortener or redirect can't lead a
resolver rung or recipe there. Where the terms cover the whole site (Google, Apple, Amazon, Meta, IBM),
the whole domain is refused, not just the careers host. An admin can't add one either, since there is no
adapter for it.

## Outcome

| Site | Decision | Reason | Adapter |
|---|---|---|---|
| Google (all google.* domains, incl. careers.google.com) | REFUSED | robots.txt disallows results paging; terms name scraping as grounds for suspension | none |
| Apple (apple.com, incl. jobs.apple.com) | REFUSED | Terms of Use forbid page-scrape/robot/spider access | none |
| Amazon (amazon.jobs, amazon.* incl. hiring.amazon.com) | REFUSED | Conditions of Use exclude data mining, robots and extraction tools | none |
| Meta (metacareers.com, facebook.com, meta.com) | REFUSED | robots.txt: automated collection prohibited without written permission | none |
| Tesla (tesla.com/careers) | REFUSED | 403 to our crawler (Akamai), even for robots.txt | none |
| IBM (ibm.com) | REFUSED | Terms of Use forbid robots/spiders to retrieve, index or data-mine | none |
| Uber (uber.com/careers) | REFUSED | robots.txt disallows `*/api/` (the careers search API) | none |
| Goldman Sachs (higher.gs.com) | REFUSED | robots.txt allows only `/roles/`; search disallowed | none |
| Citadel (citadel.com/careers) | REFUSED | 403 to our crawler (bot protection) | none |
| Jane Street (janestreet.com/join-jane-street) | ALLOWED via Greenhouse | jobs are the public Greenhouse board `janestreet`; no custom adapter needed | `greenhouse` / `janestreet` |
| Intel (jobs.intel.com) | ALLOWED via Workday | jobs.intel.com blocks crawlers, but the jobs are the public Workday site `intel.wd1.External` | `workday` / `intel.wd1.External` |

Not reviewed (already covered by another adapter or unit): Nvidia (Workday), Microsoft and Netflix
(Eightfold unit).

So **no custom adapter was built**. Jane Street and Intel links resolve to boards we already read,
without fetching their careers sites.

## Evidence

### Google
- robots.txt: https://www.google.com/robots.txt (`User-agent: *`):
  `Disallow: /about/careers/applications/jobs/results?page=`,
  `Disallow: /about/careers/applications/jobs/results/?*&page=` (and the two variants). Only the first
  page of results may be fetched, so the board can't be listed. `careers.google.com/robots.txt`
  redirects to google.com (404).
- Terms: https://policies.google.com/terms, "Don't abuse our services": "using automated means to
  access content from any of our services in violation of the machine-readable instructions on our web
  pages (for example, robots.txt files that disallow crawling...)"; suspension grounds include
  "scraping content that doesn't belong to you".

### Apple
- robots.txt: https://jobs.apple.com/robots.txt redirects (301) to apple.com/pagenotfound, so there are
  no rules for the jobs host.
- Terms: https://www.apple.com/legal/internet-services/terms/site.html, "Your Use of the Site": "You
  may not use any 'deep-link', 'page-scrape', 'robot', 'spider' or other automatic device, program,
  algorithm or methodology ... to access, acquire, copy or monitor any portion of the Site or any
  Content". Refused on terms.

### Amazon
- robots.txt: https://www.amazon.jobs/robots.txt (`User-agent: *`) only disallows `/internal` paths, so
  `/en/search.json` would be allowed.
- Terms: the amazon.jobs footer ("Legal disclosures and notices",
  https://www.amazon.jobs/content/en/legal-disclosures-and-notices) links "Conditions of use" to
  https://www.amazon.com/gp/help/customer/display.html?nodeId=508088, whose licence "does not include
  ... any use of data mining, robots, or similar data gathering and extraction tools". Refused on terms
  despite permissive robots.txt.

### Meta
- robots.txt: https://www.metacareers.com/robots.txt opens with "Notice: Collection of data on
  Facebook through automated means is prohibited unless you have express written permission from
  Facebook" (see http://www.facebook.com/apps/site_scraping_tos_terms.php). It also disallows
  `/ajax/` and `/*cursor=` for `*`.
- Terms page (https://www.metacareers.com/legal/terms) answered 429 to our fetch. Refused on the
  robots.txt notice.

### Tesla
- https://www.tesla.com/robots.txt and https://www.tesla.com/legal/terms both returned **403 Access
  Denied** (Akamai edge) to our User-Agent. A site that blocks identified crawlers is refused.

### IBM
- robots.txt: https://www.ibm.com/robots.txt disallows e.g. `/search/?`, `/Search`, `/docs/api`.
- Terms: https://www.ibm.com/legal/terms. "General": "You may only use a crawler to crawl this Web
  site as permitted by this Web site's robots.txt protocol, and IBM may block any crawlers in its sole
  discretion." "Rules of conduct": no use of "any robot, spider, site search/retrieval application, or
  other manual or automatic device or process to retrieve, index, 'data mine', or in any way reproduce
  or circumvent the navigational structure or presentation of IBM Websites or contents". Refused on terms.

### Uber
- robots.txt: https://www.uber.com/robots.txt (`User-agent: *`): `disallow: */api/` and
  `disallow: */custom-api/`. The careers search loads results from `/api/loadSearchJobsResults`.
  Refused on robots.

### Goldman Sachs
- robots.txt: https://higher.gs.com/robots.txt: `Allow: /roles/` then `Disallow: /` for `*`. The results
  listing is disallowed. Refused on robots.

### Citadel
- robots.txt: https://www.citadel.com/robots.txt is permissive (`Disallow: /cdn-cgi/`, `Crawl-delay: 10`),
  but https://www.citadel.com/ and https://www.citadel.com/terms-of-use/ returned **403** to our crawler,
  so the terms couldn't be read. Refused (bot-blocked, terms unreadable).

### Jane Street
- robots.txt: https://www.janestreet.com/robots.txt allows `/jobs/main.json` (the site's own listing)
  and disallows a few specific position pages. No terms of use page is linked (only a privacy notice).
- Its job ids are Greenhouse ids and https://boards-api.greenhouse.io/v1/boards/janestreet/jobs is a
  public board, which the Greenhouse adapter already reads. So instead of a custom adapter, a pasted
  `janestreet.com/join-jane-street/...` link maps to `greenhouse/janestreet`.

### Intel
- https://jobs.intel.com/robots.txt returned 403 to our crawler. Intel's postings are on the public
  Workday site `intel.wd1.myworkdayjobs.com/External` (the CXS jobs endpoint answered 200), which the
  Workday adapter already reads. A pasted `jobs.intel.com` link maps to `workday/intel.wd1.External`;
  jobs.intel.com itself is never fetched.

## Revisiting

Re-review a site when its terms change or it publishes an API/feed licence. To enable one: record the
new evidence here, set `terms_ok=True`, `ats=...`, `board=(ats, fixed_slug)` in `CUSTOM_SITES`, add the
adapter module in this package (host fixed in code, robots honoured with `safe_fetch(...,
robots=PoliteFetcher(client))`), and register it in `integrations/ats/registry.py`.
