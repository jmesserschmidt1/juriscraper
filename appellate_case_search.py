#!/usr/bin/env python
"""Find the appellate case that corresponds to a district-court case.

Given an *originating* (district court) case number, this searches the relevant
U.S. Court of Appeals on PACER and returns the matching appellate case(s) --
appellate docket number, case name, and (when confirmed) the originating court
information read back from the appellate docket.

It builds on the same Juriscraper PACER stack as ``pacer_docket_scraper.py`` and
reuses that script's login (including MFA). Two pieces are solid and Juriscraper
-backed:

  * the district -> circuit mapping (which appellate court to search), and
  * the "confirm" step, which fetches the appellate docket with Juriscraper's
    ``AppellateDocketReport`` (``incOrigDkt=Y``) and reads its "Originating
    Court Information" -- the district court and case number the appeal came
    from -- so a search hit can be verified against the number you searched for.

The one best-effort piece is the live appellate *case search* request itself.
Juriscraper does not implement an appellate search-by-originating-number, and
the classic appellate CM/ECF search endpoint is not publicly documented, so the
request in ``AppellateCaseSearch.query`` is a best-effort reconstruction that
may need a tweak for a given circuit's CM/ECF version. To keep things reliable
in the meantime, you can also run the court's Case Search in a browser, save the
results page, and parse it here with ``--search-html`` -- the parsing and the
confirm step do not depend on the live request.

Usage::

    # Live: derive the circuit from the district and search it.
    python appellate_case_search.py --district-court nhd \
        --originating-case-number 1:19-cv-00143

    # Live: name the appellate court explicitly instead of deriving it.
    python appellate_case_search.py --appellate-court ca1 \
        --originating-case-number 1:19-cv-00143

    # Offline: parse a saved appellate Case Search results page.
    python appellate_case_search.py --appellate-court ca1 \
        --originating-case-number 1:19-cv-00143 \
        --search-html ./ca1_search_results.html

Credentials come from PACER_USERNAME / PACER_PASSWORD (or --username /
--password), with the same MFA handling as pacer_docket_scraper.py.

NOTE: PACER is a paid service; live searches and docket confirmations incur
charges.
"""

import argparse
import logging
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

from juriscraper.lib.string_utils import clean_string
from juriscraper.pacer import AppellateDocketReport
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

# Appellate docket numbers look like "19-2244".
APPELLATE_DOCKET_RE = re.compile(r"\b(\d\d-\d{3,5})\b")


def appellate_court_for_district(district_court_id):
    """Return the appellate court id that hears appeals from a district court.

    :param district_court_id: A Juriscraper district court id, e.g. ``"nhd"``.
    :return: An appellate court id, e.g. ``"ca1"``.
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


class AppellateCaseSearch(BaseReport):
    """Search an appellate court's PACER site by originating case number.

    Reliability note: the live request in ``query`` is a best-effort
    reconstruction of the appellate CM/ECF "Case Search" (Juriscraper has no
    such report, and the endpoint isn't publicly documented). ``parse`` /
    ``data`` -- the results parsing -- are independent of it and can be run
    against a saved results page via ``_parse_text``.
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

    def query(self, originating_case_number):
        """Search the appellate court for cases from an originating case number.

        Best-effort: posts the originating case number to the court's Case
        Search servlet. If a circuit's CM/ECF rejects this or returns no
        parsable rows, fall back to saving the browser results page and parsing
        it with ``_parse_text`` (the ``--search-html`` mode).

        :param originating_case_number: The district court case number, e.g.
        ``"1:19-cv-00143"``.
        :return: None; sets ``self.response`` and runs ``self.parse()``.
        """
        assert self.session is not None, (
            "session attribute of AppellateCaseSearch cannot be None."
        )
        params = {
            "servlet": "CaseSearch.jsp",
            # The originating (lower court) case number to search by. Field
            # name is a best-effort reconstruction; adjust if your circuit
            # names it differently.
            "origCaseNumber": originating_case_number,
            "caseNumber": originating_case_number,
        }
        logger.info(
            "Searching %s for originating case '%s' (params=%s)",
            self.court_id,
            originating_case_number,
            params,
        )
        self.response = self.session.get(self.url, params=params)
        self.parse()

    @property
    def data(self):
        """Return the matching appellate cases parsed from the results page.

        :return: A list of dicts with ``appellate_docket_number``,
        ``case_name``, ``pacer_case_id`` (when present), and ``url``.
        """
        if self.tree is None:
            return []

        results = []
        seen = set()
        # Appellate Case Search results link each hit to its CaseSummary.
        anchors = self.tree.xpath(
            '//a[contains(@href, "CaseSummary.jsp") '
            'or contains(@href, "caseNum=") '
            'or contains(@href, "caseid=") '
            'or contains(@href, "caseId=")]'
        )
        for anchor in anchors:
            href = anchor.get("href", "")
            query = parse_qs(urlparse(href).query)

            docket_number = (query.get("caseNum") or [""])[0].strip()
            if not docket_number:
                text_match = APPELLATE_DOCKET_RE.search(anchor.text_content())
                if text_match:
                    docket_number = text_match.group(1)
            if not docket_number:
                continue

            pacer_case_id = (
                query.get("caseid")
                or query.get("caseId")
                or [None]
            )[0]

            # Prefer the enclosing row's text as the case name; fall back to
            # the text right after the link.
            case_name = ""
            rows = anchor.xpath("./ancestor::tr[1]")
            if rows:
                row_text = clean_string(rows[0].text_content())
                case_name = clean_string(
                    row_text.replace(docket_number, "", 1)
                )
            if not case_name:
                case_name = clean_string(anchor.tail or "")

            key = (docket_number, pacer_case_id)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "appellate_docket_number": docket_number,
                    "case_name": case_name,
                    "pacer_case_id": pacer_case_id,
                    "url": href,
                }
            )
        return results


def normalize_docket_number(docket_number):
    """Strip office/judge decoration so two docket numbers compare equal.

    E.g. ``"1:19-cv-00143-JD"`` and ``"1:19-cv-00143"`` both reduce to
    ``"1:19-cv-00143"``.

    :param docket_number: A district docket number string.
    :return: The core docket number, or the cleaned input if it doesn't match.
    """
    match = AppellateDocketReport.docket_number_dist_regex.search(
        docket_number or ""
    )
    return match.group(1) if match else clean_string(docket_number or "")


def confirm_originating_case(
    session, appellate_court, appellate_docket_number, originating_case_number
):
    """Read an appellate docket's originating info to confirm/annotate a hit.

    :param session: A logged-in PACER session.
    :param appellate_court: The appellate court id, e.g. ``"ca1"``.
    :param appellate_docket_number: The appellate docket number, e.g.
    ``"19-2244"``.
    :param originating_case_number: The district case number searched for.
    :return: A dict with the parsed ``originating_court_information`` and a
    ``matches`` boolean, or ``{"matches": False, "error": ...}`` on failure.
    """
    report = AppellateDocketReport(appellate_court, session)
    try:
        report.query(appellate_docket_number, show_orig_docket=True)
    except Exception as exc:  # noqa: BLE001 - report the failure, keep going
        return {"matches": False, "error": str(exc)}

    info = (report.data or {}).get("originating_court_information") or {}
    found = normalize_docket_number(info.get("docket_number"))
    wanted = normalize_docket_number(originating_case_number)
    return {
        "matches": bool(found) and found == wanted,
        "originating_court_information": info,
    }


def search_appellate(
    session,
    appellate_court,
    originating_case_number,
    search_html=None,
    confirm=True,
):
    """Run (or load) an appellate search and optionally confirm each hit.

    :param session: A logged-in PACER session (may be None when only parsing a
    local results page with confirm disabled).
    :param appellate_court: The appellate court id to search.
    :param originating_case_number: The district case number to search by.
    :param search_html: Optional path to a saved results page to parse instead
    of querying PACER.
    :param confirm: Whether to fetch each hit's appellate docket to confirm the
    originating case number.
    :return: A results dict ready to serialize to JSON.
    """
    report = AppellateCaseSearch(appellate_court, session)
    if search_html:
        logger.info("Parsing saved appellate search results: %s", search_html)
        report._parse_text(read_html_file(search_html))
    else:
        report.query(originating_case_number)

    hits = report.data
    logger.info(
        "Found %s candidate appellate case(s) in %s.", len(hits), appellate_court
    )

    if confirm and session is not None:
        for hit in hits:
            confirmation = confirm_originating_case(
                session,
                appellate_court,
                hit["appellate_docket_number"],
                originating_case_number,
            )
            hit.update(confirmation)

    confirmed = [h for h in hits if h.get("matches")]
    return {
        "appellate_court": appellate_court,
        "originating_case_number": originating_case_number,
        "confirmed_matches": confirmed,
        "all_candidates": hits,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Search a U.S. Court of Appeals by originating (district-court) "
            "case number and return the matching appellate case(s)."
        ),
        epilog=(
            "Give the appellate court with --appellate-court, or let it be "
            "derived from --district-court. Use --search-html to parse a saved "
            "Case Search results page instead of querying PACER live.\n"
        ),
    )
    parser.add_argument(
        "--originating-case-number",
        required=True,
        help="District court case number to search by, e.g. '1:19-cv-00143'.",
    )
    parser.add_argument(
        "--district-court",
        default=None,
        help=(
            "District court id (e.g. 'nhd', 'nysd'); the appellate court is "
            "derived from it when --appellate-court is not given."
        ),
    )
    parser.add_argument(
        "--appellate-court",
        default=None,
        help="Appellate court id to search (e.g. 'ca1'). Overrides derivation.",
    )
    parser.add_argument(
        "--search-html",
        default=None,
        help=(
            "Path to a saved appellate Case Search results page to parse "
            "instead of querying PACER (no login needed unless --confirm)."
        ),
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help=(
            "Don't fetch each hit's appellate docket to confirm the "
            "originating case number (saves PACER charges)."
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
            "A PACER username is required for live requests. Set "
            "PACER_USERNAME or pass --username."
        )
    password = resolve_password(args.password)
    if not password:
        sys.exit(
            "A PACER password is required for live requests. Set "
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

    confirm = not args.no_confirm
    # A session is needed for a live search, and for confirming hits.
    need_session = (args.search_html is None) or confirm
    session = login_if_needed(args, need_session)

    results = search_appellate(
        session,
        appellate_court,
        args.originating_case_number,
        search_html=args.search_html,
        confirm=confirm,
    )

    base = (
        f"{appellate_court}_orig_"
        f"{docket_number_slug(args.originating_case_number)}_appeals"
    )
    out_path = os.path.join(args.output_dir, f"{base}.json")
    save_json(results, out_path)

    logger.info(
        "Done: %s confirmed / %s candidate appellate case(s). Wrote %s",
        len(results["confirmed_matches"]),
        len(results["all_candidates"]),
        os.path.abspath(out_path),
    )


if __name__ == "__main__":
    main()
