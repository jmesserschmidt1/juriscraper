#!/usr/bin/env python
"""Look up PACER case metadata for a list of cases using the hidden
``possible_case_numbers`` API.

For each case in an input JSON file, this script queries PACER's
``PossibleCaseNumberApi`` (see ``juriscraper/pacer/hidden_api.py``) using the
case's ``court_id`` and ``docket_no_slug``. The ``docket_number``,
``pacer_case_id``, and ``title`` returned by the API are appended back onto
each case, and the augmented list is written to an output JSON file.

Usage:
    export PACER_USERNAME='your-username'
    export PACER_PASSWORD='your-password'

    python search_possible_case_numbers.py \
        input.json \
        --output output.json

The input JSON is expected to be a list of objects, each containing at least
``court_id`` and ``docket_no_slug`` keys (as produced by the ISS search
results export).
"""

import argparse
import json
import os
import sys
import time

from juriscraper.lib.exceptions import ParsingException
from juriscraper.lib.log_tools import make_default_logger
from juriscraper.pacer import PossibleCaseNumberApi
from juriscraper.pacer.http import PacerSession

logger = make_default_logger()

# Keys that this script appends to each case in the input JSON.
RESULT_KEYS = ("docket_number", "pacer_case_id", "title")


def make_session():
    """Build and log in to a PACER session using environment credentials.

    :return: A logged-in ``PacerSession``.
    """
    username = os.environ.get("PACER_USERNAME")
    password = os.environ.get("PACER_PASSWORD")
    if not (username and password):
        sys.exit(
            "Please set the PACER_USERNAME and PACER_PASSWORD environment "
            "variables before running this script."
        )

    session = PacerSession(username=username, password=password)
    logger.info("Logging in to PACER as %s", username)
    session.login()
    return session


def lookup_case(session, court_id, docket_no_slug, case_name=None):
    """Query the possible_case_numbers API for a single case.

    :param session: A logged-in ``PacerSession``.
    :param court_id: The PACER court identifier (e.g. ``nyed``).
    :param docket_no_slug: The docket number to search for (e.g. ``24-08650``).
    :param case_name: Optional case name used to disambiguate when the API
        returns more than one matching case.
    :return: A dict with ``docket_number``, ``pacer_case_id``, and ``title``,
        or ``None`` if no match was found or the lookup failed.
    """
    report = PossibleCaseNumberApi(court_id, session)
    report.query(docket_no_slug)
    return report.data(case_name=case_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        help="Path to the input JSON file (a list of case objects).",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Path to write the augmented JSON. Defaults to the input path "
            "with a '.with_pacer.json' suffix."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to sleep between PACER requests (default: 0).",
    )
    args = parser.parse_args()

    output_path = args.output or (
        os.path.splitext(args.input)[0] + ".with_pacer.json"
    )

    with open(args.input) as f:
        cases = json.load(f)

    if not isinstance(cases, list):
        sys.exit("Expected the input JSON to be a list of case objects.")

    session = make_session()

    total = len(cases)
    found = 0
    skipped = 0
    errored = 0

    for i, case in enumerate(cases, start=1):
        court_id = case.get("court_id")
        docket_no_slug = case.get("docket_no_slug")
        case_name = case.get("CaseName")

        # Initialize the result keys so every record has a consistent schema.
        for key in RESULT_KEYS:
            case.setdefault(key, None)

        if not (court_id and docket_no_slug):
            logger.info(
                "[%s/%s] Skipping case %s: missing court_id or "
                "docket_no_slug.",
                i,
                total,
                case.get("CaseID"),
            )
            skipped += 1
            continue

        logger.info(
            "[%s/%s] Looking up %s in %s (%s)",
            i,
            total,
            docket_no_slug,
            court_id,
            case_name,
        )
        try:
            data = lookup_case(session, court_id, docket_no_slug, case_name)
        except ParsingException as e:
            # Raised when multiple results come back with no way to choose, or
            # when the XML content is unexpected. Record and move on.
            logger.warning(
                "[%s/%s] Could not resolve %s in %s: %s",
                i,
                total,
                docket_no_slug,
                court_id,
                e,
            )
            errored += 1
            data = None
        except Exception as e:
            # Network errors, unexpected responses, etc. Keep going so one bad
            # case doesn't lose the whole run.
            logger.warning(
                "[%s/%s] Error looking up %s in %s: %s",
                i,
                total,
                docket_no_slug,
                court_id,
                e,
            )
            errored += 1
            data = None

        if data:
            for key in RESULT_KEYS:
                case[key] = data.get(key)
            found += 1
        else:
            logger.info(
                "[%s/%s] No case found for %s in %s",
                i,
                total,
                docket_no_slug,
                court_id,
            )

        if args.delay:
            time.sleep(args.delay)

    with open(output_path, "w") as f:
        json.dump(cases, f, indent=2, ensure_ascii=False)

    logger.info(
        "Done. %s found, %s not found/errored, %s skipped out of %s. "
        "Wrote results to %s",
        found,
        errored,
        skipped,
        total,
        output_path,
    )
    print(
        f"Done. {found} found, {errored} not found/errored, {skipped} "
        f"skipped out of {total}. Wrote results to {output_path}"
    )


if __name__ == "__main__":
    main()
