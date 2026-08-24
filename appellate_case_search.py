#!/usr/bin/env python
"""Find the appellate case that corresponds to a district-court case.

Given an *originating* (district court) case number, this searches the relevant
U.S. Court of Appeals on PACER and returns the matching appellate case(s). The
appellate Case Search's own results already carry the originating case number
and a direct link to the district docket, so each result also reports the
district court and case number it came from -- i.e. "the corresponding case in
the district court."

It builds on the same Juriscraper PACER stack as ``pacer_docket_scraper.py`` and
reuses that script's login (including MFA). The mechanism was confirmed against
Second Circuit (ca2) Case Search pages:

  * Search: a request to the court's ``TransportRoom`` endpoint with
    ``servlet=CaseSelectionTable.jsp`` and ``origCase=<originating number>``
    (the "Originating Case Number" field on the advanced Case Search form).
  * Results: the "Case Selection Table" lists each matching appellate case with
    its number, title, opening date, and -- crucially -- the originating case
    number linked back to the district court's docket report.

Usage::

    # Live: derive the circuit from the district and search it.
    python appellate_case_search.py --district-court nysd \
        --originating-case-number 17-cv-1545

    # Live: name the appellate court explicitly instead of deriving it.
    python appellate_case_search.py --appellate-court ca2 \
        --originating-case-number 17-cv-1545

    # Offline: parse a saved Case Selection Table (no login needed).
    python appellate_case_search.py --appellate-court ca2 \
        --originating-case-number 17-cv-1545 \
        --search-html ./case_selection_page_ca2.html

Credentials come from PACER_USERNAME / PACER_PASSWORD (or --username /
--password), with the same MFA handling as pacer_docket_scraper.py.

NOTE: PACER is a paid service; live searches incur charges.
"""

import argparse
import logging
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

from juriscraper.lib.html_utils import get_html_parsed_text
from juriscraper.lib.string_utils import clean_string
from juriscraper.pacer.reports import BaseReport

# Reuse the login, credential, and IO helpers from the district script so the
# MFA handling lives in exactly one place.
from pacer_docket_scraper import (
    build_session,
    docket_number_slug,
    read_html_file,
    resolve_otp,
    resolve_password,
    save_json,
)

logger = logging.getLogger("appellate_case_search")


# Which circuit hears appeals from each state / territory. District court ids
# in Juriscraper are "<state><division>" (nysd, cacd, txsd, ...), so the state
# is normally the first two letters; the territorial and same-prefix cases are
# handled by SPECIAL_DISTRICT_TO_APPELLATE below.
STATE_TO_APPELLATE = {
    # First Circuit
    "ME": "ca1", "MA": "ca1", "NH": "ca1", "RI": "ca1", "PR": "ca1",
    # Second Circuit
    "CT": "ca2", "NY": "ca2", "VT": "ca2",
    # Third Circuit
    "DE": "ca3", "NJ": "ca3", "PA": "ca3", "VI": "ca3",
    # Fourth Circuit
    "MD": "ca4", "NC": "ca4", "SC": "ca4", "VA": "ca4", "WV": "ca4",
    # Fifth Circuit
    "LA": "ca5", "MS": "ca5", "TX": "ca5",
    # Sixth Circuit
    "KY": "ca6", "MI": "ca6", "OH": "ca6", "TN": "ca6",
    # Seventh Circuit
    "IL": "ca7", "IN": "ca7", "WI": "ca7",
    # Eighth Circuit
    "AR": "ca8", "IA": "ca8", "MN": "ca8", "MO": "ca8",
    "NE": "ca8", "ND": "ca8", "SD": "ca8",
    # Ninth Circuit
    "AK": "ca9", "AZ": "ca9", "CA": "ca9", "HI": "ca9", "ID": "ca9",
    "MT": "ca9", "NV": "ca9", "OR": "ca9", "WA": "ca9", "GU": "ca9",
    # Tenth Circuit
    "CO": "ca10", "KS": "ca10", "NM": "ca10", "OK": "ca10",
    "UT": "ca10", "WY": "ca10",
    # Eleventh Circuit
    "AL": "ca11", "FL": "ca11", "GA": "ca11",
    # D.C. Circuit
    "DC": "cadc",
}

# District ids whose first two letters don't give the right state (territories
# and collisions). "nmid" is the Northern Mariana Islands (Ninth Circuit); note
# it collides with "nm" = New Mexico ("nmd", Tenth Circuit), which resolves
# correctly through the state map.
SPECIAL_DISTRICT_TO_APPELLATE = {
    "nmid": "ca9",
}

APPELLATE_COURTS = set(STATE_TO_APPELLATE.values()) | {"cafc"}


def appellate_court_for_district(district_court_id):
    """Return the appellate court id that hears appeals from a district court.

    :param district_court_id: A Juriscraper district court id, e.g. ``"nysd"``.
    :return: An appellate court id, e.g. ``"ca2"``.
    :raises ValueError: if the circuit can't be determined.
    """
    court = district_court_id.strip().lower()
    if court in SPECIAL_DISTRICT_TO_APPELLATE:
        return SPECIAL_DISTRICT_TO_APPELLATE[court]
    state = court[:2].upper()
    if state in STATE_TO_APPELLATE:
        return STATE_TO_APPELLATE[state]
    raise ValueError(
        f"Could not determine the appellate court for district '{court}'. "
        f"Pass --appellate-court explicitly (note the Federal Circuit, cafc, "
        f"hears appeals by subject matter, not geography)."
    )


def normalize_originating_number(docket_number):
    """Reduce a district case number to the short form the appellate court uses.

    The appellate "Originating Case Number" is stored without the office prefix
    and without leading zeros, e.g. a district ``1:17-cv-01545`` is recorded and
    searched as ``17-cv-1545``. Normalizing both sides lets us build the search
    key and compare results regardless of how the number was typed.

    :param docket_number: A district docket number in any common form.
    :return: The normalized short form (e.g. ``"17-cv-1545"``), or the cleaned
    input if it doesn't look like a district docket number.
    """
    s = clean_string(docket_number or "").lower().replace("\xa0", " ")
    # Drop a leading office number ("1:").
    s = re.sub(r"^\s*\d+\s*:\s*", "", s)
    m = re.search(r"(\d+)-([a-z]+)-(\d+)", s)
    if m:
        return f"{int(m.group(1))}-{m.group(2)}-{int(m.group(3))}"
    return s


def court_id_from_ecf_url(url):
    """Extract the Juriscraper court id from an ``ecf.<court>.uscourts.gov`` URL.

    :param url: A PACER URL.
    :return: The court id (e.g. ``"nysd"``), or ``""`` if not found.
    """
    match = re.search(r"ecf\.([^.]+)\.uscourts\.gov", url or "")
    return match.group(1) if match else ""


class AppellateCaseSearch(BaseReport):
    """Search an appellate court's PACER Case Search by originating case number.

    ``query`` performs the live search; ``_parse_text`` / ``data`` parse the
    resulting "Case Selection Table" and work equally on a saved page.
    """

    @property
    def url(self):
        """The court's TransportRoom endpoint (mirrors AppellateDocketReport)."""
        if self.court_id == "psc":
            return (
                "https://dcecf.psc.uscourts.gov/n/beam/servlet/TransportRoom"
            )
        if self.court_id in ("ca5", "ca7", "ca11"):
            return (
                f"https://ecf.{self.court_id}.uscourts.gov/"
                "cmecf/servlet/TransportRoom"
            )
        return (
            f"https://ecf.{self.court_id}.uscourts.gov/"
            "n/beam/servlet/TransportRoom"
        )

    def _get_csrf(self):
        """Fetch the Case Search form and pull its CSRF token (best effort)."""
        try:
            r = self.session.get(
                self.url, params={"servlet": "CaseSearch.jsp"}
            )
            tree = get_html_parsed_text(r.content)
            values = tree.xpath('//input[@name="CSRF"]/@value')
            if values:
                return values[0]
        except Exception as exc:  # noqa: BLE001 - CSRF is optional; log & go on
            logger.debug("Could not obtain a CSRF token: %s", exc)
        return ""

    def query(self, originating_case_number):
        """Search the appellate court for cases from an originating case number.

        Submits the "Originating Case Number" (``origCase``) search to the
        court's ``CaseSelectionTable.jsp`` servlet, mirroring the fields the
        advanced Case Search form sends.

        :param originating_case_number: The district case number, in any common
        form (it's normalized to the appellate short form for the search).
        :return: None; sets ``self.response`` and runs ``self.parse()``.
        """
        assert self.session is not None, (
            "session attribute of AppellateCaseSearch cannot be None."
        )
        orig_case = normalize_originating_number(originating_case_number)
        params = {
            "servlet": "CaseSelectionTable.jsp",
            "origCase": orig_case,
            "searchPty": "pty",
            "open_closed": "both",
            "sortby": "casenumber",
            "sortbyorder": "asc",
            # The form also submits these empty; include them for fidelity.
            "csnum1": "",
            "csnum2": "",
            "aName": "",
            "filedate_begin": "",
            "filedate_end": "",
            "closedate_begin": "",
            "closedate_end": "",
            "lastdate_begin": "",
            "lastdate_end": "",
        }
        csrf = self._get_csrf()
        if csrf:
            params["CSRF"] = csrf

        logger.info(
            "Searching %s Case Search for originating case '%s'.",
            self.court_id,
            orig_case,
        )
        self.response = self.session.get(self.url, params=params)
        self.parse()

    @property
    def data(self):
        """Parse the Case Selection Table into a list of appellate cases.

        Each result is anchored on its "Case Summary" link (the appellate case
        number); the rest of the row supplies the title, dates, and originating
        case. Returns a list of dicts with keys: ``appellate_docket_number``,
        ``case_name``, ``pacer_case_id``, ``appellate_url``, ``opening_date``,
        ``last_docket_entry``, ``originating_case_number``,
        ``originating_court_id``, ``origin``, ``origin_code``, and
        ``originating_docket_url``.
        """
        if self.tree is None:
            return []

        results = []
        seen = set()
        for summary in self.tree.xpath(
            '//a[contains(@href, "CaseSummary.jsp")]'
        ):
            rows = summary.xpath("./ancestor::tr[1]")
            if not rows:
                continue
            cells = rows[0].xpath("./td")
            if len(cells) < 4:
                continue

            summary_q = parse_qs(urlparse(summary.get("href", "")).query)
            appellate_docket_number = (summary_q.get("caseNum") or [""])[0]
            if not appellate_docket_number:
                appellate_docket_number = clean_string(summary.text_content())
            if appellate_docket_number in seen:
                continue
            seen.add(appellate_docket_number)

            # Case title + internal caseid come from the "Case Query" link.
            case_name, pacer_case_id = "", None
            query_links = cells[0].xpath(
                './/a[contains(@href, "CaseQuery.jsp")]'
            )
            if query_links:
                case_name = clean_string(query_links[0].text_content())
                q = parse_qs(urlparse(query_links[0].get("href", "")).query)
                pacer_case_id = (q.get("caseid") or [None])[0]

            # Originating cell: "<origin_code> : <orig number> <origin name>".
            orig_cell = cells[3]
            orig_full = clean_string(
                orig_cell.text_content().replace("\xa0", " ")
            )
            originating_case_number = ""
            originating_court_id = ""
            originating_docket_url = ""
            dkt_links = orig_cell.xpath(
                './/a[contains(@href, "DktRpt.pl") '
                'or contains(@href, "iquery.pl") '
                'or contains(@href, "DocketSheet")]'
            )
            if dkt_links:
                originating_docket_url = dkt_links[0].get("href", "")
                originating_case_number = clean_string(
                    dkt_links[0].text_content()
                )
                originating_court_id = court_id_from_ecf_url(
                    originating_docket_url
                )

            origin_code = ""
            if ":" in orig_full:
                origin_code = orig_full.split(":", 1)[0].strip()
            origin = orig_full
            if originating_case_number and originating_case_number in orig_full:
                origin = orig_full.split(originating_case_number, 1)[-1].strip()

            results.append(
                {
                    "appellate_docket_number": appellate_docket_number,
                    "case_name": case_name,
                    "pacer_case_id": pacer_case_id,
                    "appellate_url": summary.get("href", ""),
                    "opening_date": clean_string(cells[1].text_content()),
                    # Date and time sit on either side of a <br>; join with a
                    # space so they don't run together ("...2025" + "10:11...").
                    "last_docket_entry": clean_string(
                        " ".join(cells[2].xpath(".//text()")).replace(
                            "\xa0", " "
                        )
                    ),
                    "originating_case_number": originating_case_number,
                    "originating_court_id": originating_court_id,
                    "origin": origin,
                    "origin_code": origin_code,
                    "originating_docket_url": originating_docket_url,
                }
            )
        return results


def search_appellate(
    session, appellate_court, originating_case_number, search_html=None
):
    """Run (or load) an appellate originating-case search.

    :param session: A logged-in PACER session (unused when ``search_html`` is
    given).
    :param appellate_court: The appellate court id to search.
    :param originating_case_number: The district case number to search by.
    :param search_html: Optional path to a saved Case Selection Table to parse
    instead of querying PACER.
    :return: A results dict ready to serialize to JSON.
    """
    report = AppellateCaseSearch(appellate_court, session)
    if search_html:
        logger.info("Parsing saved Case Selection Table: %s", search_html)
        report._parse_text(read_html_file(search_html))
    else:
        report.query(originating_case_number)

    wanted = normalize_originating_number(originating_case_number)
    hits = report.data
    for hit in hits:
        hit["matches_search"] = (
            normalize_originating_number(hit["originating_case_number"])
            == wanted
        )

    logger.info(
        "Found %s appellate case(s) in %s for originating case '%s'.",
        len(hits),
        appellate_court,
        originating_case_number,
    )
    return {
        "appellate_court": appellate_court,
        "originating_case_number": originating_case_number,
        "results": hits,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Search a U.S. Court of Appeals by originating (district-court) "
            "case number and return the matching appellate case(s), each with "
            "the district court and case number it came from."
        ),
        epilog=(
            "Give the appellate court with --appellate-court, or let it be "
            "derived from --district-court. Use --search-html to parse a saved "
            "Case Selection Table instead of querying PACER live.\n"
        ),
    )
    parser.add_argument(
        "--originating-case-number",
        required=True,
        help="District court case number to search by, e.g. '17-cv-1545'.",
    )
    parser.add_argument(
        "--district-court",
        default=None,
        help=(
            "District court id (e.g. 'nysd'); the appellate court is derived "
            "from it when --appellate-court is not given."
        ),
    )
    parser.add_argument(
        "--appellate-court",
        default=None,
        help="Appellate court id to search (e.g. 'ca2'). Overrides derivation.",
    )
    parser.add_argument(
        "--search-html",
        default=None,
        help=(
            "Path to a saved Case Selection Table page to parse instead of "
            "querying PACER (no login needed)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="./pacer_output",
        help="Directory for the JSON output (default: ./pacer_output).",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("PACER_USERNAME"),
        help="PACER username (defaults to the PACER_USERNAME env var).",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("PACER_PASSWORD"),
        help="PACER password (defaults to the PACER_PASSWORD env var).",
    )
    parser.add_argument(
        "--client-code",
        default=os.environ.get("PACER_CLIENT_CODE"),
        help="Optional PACER client code (defaults to PACER_CLIENT_CODE).",
    )
    parser.add_argument(
        "--otp",
        default=None,
        help=(
            "One-time passcode for MFA-enabled accounts. Prompted for if "
            "omitted and needed (see pacer_docket_scraper.py)."
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable DEBUG logging."
    )
    return parser.parse_args(argv)


def resolve_appellate_court(args):
    """Determine the appellate court id from the args."""
    if args.appellate_court:
        court = args.appellate_court.strip().lower()
        if court not in APPELLATE_COURTS:
            logger.warning(
                "'%s' is not a recognized appellate court id; using it anyway.",
                court,
            )
        return court
    if args.district_court:
        return appellate_court_for_district(args.district_court)
    sys.exit(
        "Provide --appellate-court, or --district-court to derive it from."
    )


def login_if_needed(args, needed):
    """Log in only when a live PACER request is actually required."""
    if not needed:
        return None
    if not args.username:
        sys.exit(
            "A PACER username is required for a live search. Set "
            "PACER_USERNAME or pass --username."
        )
    password = resolve_password(args.password)
    if not password:
        sys.exit(
            "A PACER password is required for a live search. Set "
            "PACER_PASSWORD, pass --password, or run interactively."
        )
    otp_code = resolve_otp(args.otp)
    return build_session(args.username, password, args.client_code, otp_code)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    appellate_court = resolve_appellate_court(args)
    os.makedirs(args.output_dir, exist_ok=True)

    session = login_if_needed(args, needed=args.search_html is None)
    results = search_appellate(
        session,
        appellate_court,
        args.originating_case_number,
        search_html=args.search_html,
    )

    base = (
        f"{appellate_court}_orig_"
        f"{docket_number_slug(args.originating_case_number)}_appeals"
    )
    out_path = os.path.join(args.output_dir, f"{base}.json")
    save_json(results, out_path)

    logger.info(
        "Done: %s appellate case(s). Wrote %s",
        len(results["results"]),
        os.path.abspath(out_path),
    )


if __name__ == "__main__":
    main()
