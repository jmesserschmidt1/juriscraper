#!/usr/bin/env python
"""Search PACER for a case by docket number and court, fetch its Docket Report
and Docket History Report, save the HTML of each, and parse each into JSON.

This is a thin, self-contained caller built on top of the Juriscraper PACER
library. It ties together the pieces Juriscraper already provides:

  1. ``PacerSession``          -- logs into PACER and holds the session cookies.
  2. ``PossibleCaseNumberApi`` -- turns a (docket_number, court) pair into the
                                  internal ``pacer_case_id`` that every report
                                  endpoint needs.
  3. ``DocketReport``          -- the full docket sheet (parties, counsel,
                                  docket entries).
  4. ``DocketHistoryReport``   -- the lighter "history/documents" report.

For each report we download the raw HTML exactly as PACER served it, write it
to disk, and then parse that saved HTML into a JSON document. Parsing is done
from the file on disk (not the in-memory response) to demonstrate that the
download and parse steps are independent -- you can re-parse saved HTML at any
time without touching PACER again.

Multi-factor authentication (MFA)
---------------------------------
PACER accounts can require a one-time passcode (OTP) in addition to the
username and password. The PACER Authentication API takes the OTP in the same
login request as the ``otpCode`` field -- there is no separate challenge step.
Juriscraper's stock ``PacerSession.login`` does not send ``otpCode``, so this
script adds a small ``MfaPacerSession`` subclass that injects it.

Because authenticator-app codes rotate roughly every 30 seconds and backup
codes are single-use, the OTP has to be fresh at the moment of login. This
script therefore acquires it as late as possible (an interactive prompt right
before logging in, unless you pass ``--otp`` / ``PACER_OTP``). One consequence:
Juriscraper's automatic mid-session re-login cannot work for MFA accounts,
since it has no way to obtain a new OTP on its own. That only matters if the
session cookie expires mid-run; for a single case it will not. If you hit a
``PacerLoginException`` partway through a long job, just re-run with a fresh
code.

Usage::

    export PACER_USERNAME=your_username
    export PACER_PASSWORD=your_password

    python pacer_docket_scraper.py \
        --court cand \
        --docket-number 4:06-cv-07294 \
        --output-dir ./pacer_output

    # With MFA: you'll be prompted for the one-time passcode, or pass it in:
    python pacer_docket_scraper.py --court cand \
        --docket-number 4:06-cv-07294 --otp 123456

Credentials may also be supplied with ``--username`` / ``--password``. When the
password or OTP is omitted and the script is run interactively, it prompts for
them (the password without echo) rather than reading them off the command line.

NOTE: PACER is a paid service. Running this against the live system incurs
charges for the docket pages it retrieves. There is no free "test" mode here;
point it at a case you actually intend to purchase.
"""

import argparse
import datetime
import getpass
import json
import logging
import os
import sys

from juriscraper.lib.log_tools import make_default_logger
from juriscraper.pacer import (
    DocketHistoryReport,
    DocketReport,
    PossibleCaseNumberApi,
)
from juriscraper.pacer.http import PacerSession

logger = make_default_logger()


class MfaPacerSession(PacerSession):
    """A ``PacerSession`` that can submit a one-time passcode (OTP) at login.

    The PACER Authentication API accepts the OTP for MFA-enabled accounts as an
    ``otpCode`` field in the same JSON body as the username and password. The
    stock ``PacerSession`` builds that body internally and offers no hook to add
    fields, so rather than copy its ~80-line ``login`` method (and risk drifting
    from upstream), we override the one small seam where the request is sent and
    splice ``otpCode`` into the JSON. All of the parent's response parsing and
    cookie handling is reused untouched.
    """

    def __init__(self, *args, otp_code=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.otp_code = otp_code

    def _prepare_login_request(self, url, data, headers, *args, **kwargs):
        """Inject ``otpCode`` into the login payload before it is sent.

        :param data: The JSON-encoded login body assembled by ``login``.
        :return: The parent's response, sent with the OTP added when present.
        """
        if self.otp_code:
            payload = json.loads(data)
            payload["otpCode"] = self.otp_code
            data = json.dumps(payload)
        return super()._prepare_login_request(
            url, data, headers, *args, **kwargs
        )


def json_serializer(obj):
    """Fallback serializer so ``json.dump`` can handle the ``date`` and
    ``datetime`` objects that the Juriscraper parsers return.

    :param obj: An object json doesn't know how to serialize.
    :return: An ISO-8601 string for dates/datetimes.
    :raises TypeError: for anything else, matching json's default behavior.
    """
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def resolve_password(cli_password):
    """Return the PACER password, prompting (without echo) if needed.

    Precedence: ``--password`` flag / ``PACER_PASSWORD`` env var, then an
    interactive ``getpass`` prompt when running on a TTY. Returns None when no
    password can be obtained.

    :param cli_password: The value from --password (already defaulted to the
    PACER_PASSWORD env var by argparse), or None.
    :return: The password string, or None.
    """
    if cli_password:
        return cli_password
    if sys.stdin.isatty():
        return getpass.getpass("PACER password: ") or None
    return None


def resolve_otp(cli_otp):
    """Return the one-time passcode to submit with login, or None.

    Precedence: ``--otp`` flag, then the ``PACER_OTP`` env var, then an
    interactive prompt (only when stdin is a TTY). Authenticator-app codes
    rotate every ~30 seconds and backup codes are single-use, so when prompting
    we do it immediately before login to keep the code fresh. Returns None when
    no OTP is supplied, which is the correct behavior for accounts that do not
    have MFA enabled.

    :param cli_otp: The value passed to --otp, or None.
    :return: The OTP string, or None.
    """
    if cli_otp:
        return cli_otp.strip()
    env_otp = os.environ.get("PACER_OTP")
    if env_otp:
        return env_otp.strip()
    if sys.stdin.isatty():
        entered = input(
            "PACER one-time passcode (press Enter to skip if MFA is off): "
        )
        return entered.strip() or None
    return None


def build_session(username, password, client_code, otp_code):
    """Construct an MFA-capable PACER session and log in.

    :param username: PACER username.
    :param password: PACER password.
    :param client_code: Optional PACER client code.
    :param otp_code: Optional one-time passcode for MFA-enabled accounts.
    :return: A logged-in ``MfaPacerSession``.
    """
    logger.info("Logging into PACER as '%s'", username)
    if otp_code:
        logger.info("Submitting a one-time passcode with the login request.")
    session = MfaPacerSession(
        username=username,
        password=password,
        client_code=client_code,
        otp_code=otp_code,
    )
    session.login()
    return session


def get_pacer_case_id(session, court_id, docket_number):
    """Resolve a docket number + court into PACER's internal pacer_case_id.

    PACER's report endpoints are keyed on an internal, court-specific integer
    (the ``pacer_case_id``), not on the human-readable docket number. The
    "possible case numbers" hidden API is the same endpoint the PACER website
    hits over AJAX when you type a docket number into the docket report search
    box.

    :param session: A logged-in ``PacerSession``.
    :param court_id: The Juriscraper court id, e.g. ``"cand"``.
    :param docket_number: A docket number string, e.g. ``"4:06-cv-07294"``.
    :return: The ``pacer_case_id`` string.
    :raises SystemExit: if the case cannot be found.
    """
    logger.info(
        "Looking up pacer_case_id for docket '%s' in court '%s'",
        docket_number,
        court_id,
    )
    report = PossibleCaseNumberApi(court_id, session)
    report.query(docket_number)
    result = report.data(docket_number_letters=None)

    if not result:
        sys.exit(
            f"Could not find a case for docket '{docket_number}' in "
            f"court '{court_id}'. It may not exist or may be sealed."
        )

    pacer_case_id = result["pacer_case_id"]
    logger.info(
        "Found case: %s (pacer_case_id=%s, docket_number=%s)",
        result.get("title", "").strip(),
        pacer_case_id,
        result.get("docket_number"),
    )
    return pacer_case_id


def save_html(html, path):
    """Write report HTML to disk.

    :param html: The unicode HTML of the report.
    :param path: The destination file path.
    :return: None
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("Saved HTML to %s", path)


def save_json(data, path):
    """Write parsed report data to disk as pretty-printed JSON.

    :param data: The dict returned by a report's ``.data`` property.
    :param path: The destination file path.
    :return: None
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=json_serializer, sort_keys=True)
    logger.info("Saved JSON to %s", path)


def parse_html_file(report_class, court_id, html_path):
    """Parse a saved HTML report file into a JSON-ready dict.

    This deliberately parses from the file on disk rather than reusing the
    in-memory ``report.response`` from the query. Every Juriscraper report can
    parse HTML from any source via the ``_parse_text`` hook, so a report that
    was downloaded earlier (or by some other process) can be turned into
    structured data at any time without a fresh PACER hit.

    :param report_class: A report class such as ``DocketReport``.
    :param court_id: The Juriscraper court id the HTML came from.
    :param html_path: Path to the saved HTML file.
    :return: The parsed ``.data`` dict.
    """
    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    report = report_class(court_id)
    report._parse_text(html)
    return report.data


def scrape_docket(session, court_id, pacer_case_id, output_dir):
    """Fetch, save, and parse the full Docket Report.

    :param session: A logged-in ``PacerSession``.
    :param court_id: The Juriscraper court id.
    :param pacer_case_id: The internal PACER case id.
    :param output_dir: Directory to write outputs to.
    :return: The parsed docket data dict.
    """
    logger.info("Fetching docket report for pacer_case_id=%s", pacer_case_id)
    report = DocketReport(court_id, session)
    report.query(
        pacer_case_id,
        show_parties_and_counsel=True,
        show_terminated_parties=True,
        show_list_of_member_cases=True,
    )

    html_path = os.path.join(output_dir, "docket_report.html")
    json_path = os.path.join(output_dir, "docket_report.json")

    save_html(report.response.text, html_path)
    # Parse from the saved HTML file to keep download and parse independent.
    data = parse_html_file(DocketReport, court_id, html_path)
    save_json(data, json_path)

    logger.info(
        "Docket report parsed: %s docket entries, %s parties",
        len(data.get("docket_entries", [])),
        len(data.get("parties", [])),
    )
    return data


def scrape_docket_history(session, court_id, pacer_case_id, output_dir):
    """Fetch, save, and parse the Docket History Report.

    :param session: A logged-in ``PacerSession``.
    :param court_id: The Juriscraper court id.
    :param pacer_case_id: The internal PACER case id.
    :param output_dir: Directory to write outputs to.
    :return: The parsed docket history data dict.
    """
    logger.info(
        "Fetching docket history report for pacer_case_id=%s", pacer_case_id
    )
    report = DocketHistoryReport(court_id, session)
    report.query(
        pacer_case_id,
        query_type="History",
        order_by="asc",
        show_de_descriptions=True,
    )

    html_path = os.path.join(output_dir, "docket_history_report.html")
    json_path = os.path.join(output_dir, "docket_history_report.json")

    save_html(report.response.text, html_path)
    # Parse from the saved HTML file to keep download and parse independent.
    data = parse_html_file(DocketHistoryReport, court_id, html_path)
    save_json(data, json_path)

    logger.info(
        "Docket history report parsed: %s docket entries",
        len(data.get("docket_entries", [])),
    )
    return data


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Search PACER for a case by docket number and court, download the "
            "Docket Report and Docket History Report as HTML, and parse each "
            "into JSON."
        )
    )
    parser.add_argument(
        "--court",
        required=True,
        help="Juriscraper court id (e.g. 'cand', 'nysd', 'txsd').",
    )
    parser.add_argument(
        "--docket-number",
        required=True,
        help="Docket number to search for (e.g. '4:06-cv-07294').",
    )
    parser.add_argument(
        "--output-dir",
        default="./pacer_output",
        help="Directory for the saved HTML and JSON (default: ./pacer_output).",
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
        help=(
            "Optional PACER client code, required by some courts/accounts "
            "(defaults to the PACER_CLIENT_CODE env var)."
        ),
    )
    parser.add_argument(
        "--otp",
        default=None,
        help=(
            "One-time passcode for MFA-enabled PACER accounts. If omitted and "
            "the PACER_OTP env var is unset, you will be prompted for it right "
            "before login (so the code is fresh). Leave the prompt blank if "
            "your account does not use MFA."
        ),
    )
    parser.add_argument(
        "--pacer-case-id",
        default=None,
        help=(
            "Skip the docket-number lookup and use this internal pacer_case_id "
            "directly. --docket-number is still used only for logging."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    if not args.username:
        sys.exit(
            "A PACER username is required. Set PACER_USERNAME or pass "
            "--username."
        )

    password = resolve_password(args.password)
    if not password:
        sys.exit(
            "A PACER password is required. Set PACER_PASSWORD, pass "
            "--password, or run interactively to be prompted."
        )

    os.makedirs(args.output_dir, exist_ok=True)

    # 1. Log into PACER. The session holds the auth cookies used by every
    #    subsequent report request. The OTP (if any) is acquired here, as late
    #    as possible, so a rotating authenticator code is still valid at login.
    otp_code = resolve_otp(args.otp)
    session = build_session(
        args.username, password, args.client_code, otp_code
    )

    # 2. Resolve the docket number + court into the internal pacer_case_id.
    if args.pacer_case_id:
        pacer_case_id = args.pacer_case_id
        logger.info("Using supplied pacer_case_id=%s", pacer_case_id)
    else:
        pacer_case_id = get_pacer_case_id(
            session, args.court, args.docket_number
        )

    # 3. Docket Report -> HTML -> JSON.
    scrape_docket(session, args.court, pacer_case_id, args.output_dir)

    # 4. Docket History Report -> HTML -> JSON.
    scrape_docket_history(session, args.court, pacer_case_id, args.output_dir)

    logger.info("Done. Outputs written to %s", os.path.abspath(args.output_dir))


if __name__ == "__main__":
    main()
