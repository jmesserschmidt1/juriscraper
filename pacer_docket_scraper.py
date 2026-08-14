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

The docket report is requested with "view multiple documents" enabled, so
PACER returns a structured attachment table (with page counts and file sizes)
for every entry in a single billable request -- more efficient and more
accurate than fetching a separate attachment page per document. Two small
subclasses extend the stock parsers where they still drop data this script
wants: ``AttachmentDocketReport`` keeps Juriscraper's rich structured
attachments and, for a plain report that lacks them, falls back to the inline
``(Attachments: ...)`` links, and it also adds the docket sheet's ``flags``
(the codes shown at the top, e.g. ECF, LEAD) and its ``member_cases`` /
``related_cases`` lists (each with docket number, court, and pacer_case_id);
``TerminationDocketHistoryReport`` adds the per-entry ``Terminated:`` date the
history report shows on some motions. Both only add keys -- all of
Juriscraper's existing parsed output is preserved untouched.

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
import re
import sys

from juriscraper.lib.log_tools import make_default_logger
from juriscraper.lib.string_utils import clean_string, convert_date_string
from juriscraper.pacer import (
    DocketHistoryReport,
    DocketReport,
    PossibleCaseNumberApi,
)
from juriscraper.pacer.http import PacerSession
from juriscraper.pacer.utils import get_pacer_doc_id_from_doc1_url

logger = make_default_logger()


def _parse_attachments_from_cell(cell):
    """Parse a docket entry's inline "(Attachments: ...)" links from its cell.

    A docket-text cell can carry both cross-reference links (``re: 10
    MOTION...``) and an attachment list (``(Attachments: # 1 Exhibit A, # 2
    Exhibit B)``). Only the anchors inside the ``(Attachments:`` parenthetical
    are attachments. We flatten the cell into a string with each anchor replaced
    by a placeholder (so its href survives), isolate the ``(Attachments: ... )``
    region by balanced-parenthesis matching -- descriptions themselves can
    contain parentheses, e.g. "Good Standing (Florida, Georgia, Texas)" -- and
    read each ``# N Description`` item out of it.

    :param cell: The lxml ``<td>`` element holding the docket text.
    :return: A list of dicts with ``attachment_number``, ``pacer_doc_id``, and
    ``description`` keys, in document order. Empty if the entry has none.
    """
    # Flatten the cell into text, replacing each anchor with a placeholder
    # token (\x00<index>\x00) so we can recover its href/label afterwards.
    anchors = []
    parts = []

    def append_node_text(node):
        parts.append(node.text_content())

    if cell.text:
        parts.append(cell.text)
    for child in cell.iterchildren():
        if not isinstance(child.tag, str):
            # Comment / processing-instruction node (e.g. <!--SB-->): no anchor,
            # but keep any trailing text that belongs to the docket text.
            pass
        elif child.tag == "a":
            parts.append(f"\x00{len(anchors)}\x00")
            anchors.append((child.get("href", ""), child.text_content()))
        else:
            append_node_text(child)
        if child.tail:
            parts.append(child.tail)
    flat = "".join(parts)

    start = flat.find("(Attachments")
    if start == -1:
        return []

    # Walk from the opening paren, tracking depth, to find its match. Anchor
    # placeholders contain no parens, so they don't affect the balance.
    depth = 0
    end = None
    for i in range(start, len(flat)):
        if flat[i] == "(":
            depth += 1
        elif flat[i] == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    # Inner text between "(Attachments:" and the matching ")".
    inner = flat[flat.index(":", start) + 1 : end if end is not None else None]

    attachments = []
    # Each item is "# <placeholder> description", up to the next ", #" item.
    item_re = re.compile(
        r"\x00(\d+)\x00\s*(.*?)(?=,\s*#\s*\x00\d+\x00|$)", re.DOTALL
    )
    for match in item_re.finditer(inner):
        href, anchor_text = anchors[int(match.group(1))]
        pacer_doc_id = (
            get_pacer_doc_id_from_doc1_url(href) if "/doc1/" in href else None
        )
        number = anchor_text.strip()
        attachments.append(
            {
                "attachment_number": (
                    int(number) if number.isdigit() else number
                ),
                "pacer_doc_id": pacer_doc_id,
                "description": clean_string(match.group(2)),
            }
        )
    return attachments


class AttachmentDocketReport(DocketReport):
    """A ``DocketReport`` that reliably captures each entry's attachments.

    A docket entry's attachments can reach us two ways:

    * When the report is pulled with "view multiple documents" *and* "view all
      attachments" enabled (see ``scrape_docket``), PACER renders a structured
      attachment table per entry, and Juriscraper's stock ``DocketReport``
      already parses it into a rich ``attachments`` list -- attachment number,
      description, ``pacer_doc_id``, ``page_count``, ``file_size_str``, and
      ``file_size_bytes`` -- and also merges the main document's page count and
      size onto the entry itself. This is the preferred, most accurate source.

    * A plain docket report only lists attachments inline in the docket text
      (``(Attachments: # 1 Exhibit A, # 2 Exhibit B)``), which the stock parser
      ignores. For those entries this subclass falls back to parsing the inline
      links, yielding attachment number, ``pacer_doc_id``, and description (no
      page counts or sizes -- that data simply isn't present in that view).

    The structured table is preferred whenever present; the inline fallback
    only fills entries the stock parser left without attachments, so requesting
    the richer report is a strict upgrade and this class still works on a plain
    one.
    """

    def query(self, *args, show_all_attachments=True, **kwargs):
        """Query the docket report, also requesting "view all attachments".

        Juriscraper's ``DocketReport.query`` can enable "view multiple
        documents" (``show_multiple_docs``) but has no flag for its "view all
        attachments" sub-option. That sub-option's form field is
        ``view_all_attachments=on`` (confirmed from PACER's docket report query
        page), and it is what makes PACER render the per-entry structured
        attachment tables -- with ``view_multi_docs=on`` alone you get the
        multi-select checkboxes but no attachment rows. We add that field by
        briefly wrapping the session's ``post`` so it rides along with the
        request the parent builds, and only when "view multiple documents" is
        actually enabled (the field has no effect otherwise, and PACER warns it
        may add cost).

        :param show_all_attachments: Whether to also send
        ``view_all_attachments=on`` alongside a multi-document request.
        """
        if not show_all_attachments:
            return super().query(*args, **kwargs)

        original_post = self.session.post

        def post_with_all_attachments(url, data=None, **post_kwargs):
            if isinstance(data, dict) and data.get("view_multi_docs") == "on":
                data = {**data, "view_all_attachments": "on"}
            return original_post(url, data=data, **post_kwargs)

        self.session.post = post_with_all_attachments
        try:
            return super().query(*args, **kwargs)
        finally:
            self.session.post = original_post

    @property
    def metadata(self):
        data = super().metadata
        # Enrich the (cached) metadata once with data the stock parser drops:
        # the docket sheet's flag codes and its member / related case lists.
        if data and "flags" not in data:
            data["flags"] = self._parse_flags()
            member_cases, related_cases = self._parse_case_references()
            data["member_cases"] = member_cases
            data["related_cases"] = related_cases
        return data

    def _parse_flags(self):
        """Parse the flag codes shown at the top of the docket sheet.

        PACER renders them as ``<span>`` codes in a right-aligned cell just
        above the case caption, e.g. ``ECF``, ``LEAD``.

        :return: A list of flag code strings (empty if the case has none).
        """
        spans = self.tree.xpath(
            '//h3/preceding-sibling::table[1]//span/text()'
        )
        return [flag for flag in (clean_string(s) for s in spans) if flag]

    def _parse_case_references(self):
        """Parse the caption's "Member case" and "Related Case" links.

        Both appear near the case caption as links to other cases' docket
        reports (``DktRpt.pl?<pacer_case_id>``), with the sibling case's docket
        number as the link text. A link is a related case when its nearest
        enclosing table is the "Related Case" table (note PACER writes that
        label with a non-breaking space); otherwise it is a member case. Only
        links within the caption (which carries the "Member" / "Related" labels)
        are considered, so docket-entry links are ignored.

        :return: A two-tuple ``(member_cases, related_cases)``, each a list of
        dicts with ``docket_number``, ``court_id``, and ``pacer_case_id``.
        """
        member_cases, related_cases = [], []
        seen_member, seen_related = set(), set()
        for anchor in self.tree.xpath('//a[contains(@href, "DktRpt.pl?")]'):
            href = anchor.get("href", "")
            # A case link's query is exactly the pacer_case_id (all digits).
            # This excludes the "Select all / clear" buttons, whose query is a
            # session id like "100591727900924-L_1_0-1".
            id_match = re.search(r"DktRpt\.pl\?(\d+)(?:[#&]|$)", href)
            docket_number = clean_string(anchor.text_content())
            if not id_match or not docket_number:
                continue

            tables_text = " ".join(
                t.text_content() for t in anchor.xpath("./ancestor::table")
            ).replace("\xa0", " ")
            # Only caption links carry these labels; skip anything else.
            if "Member" not in tables_text and "Related" not in tables_text:
                continue

            court_match = re.search(r"ecf\.([^.]+)\.uscourts\.gov", href)
            entry = {
                "docket_number": docket_number,
                "court_id": court_match.group(1)
                if court_match
                else self.court_id,
                "pacer_case_id": id_match.group(1),
            }

            nearest_tables = anchor.xpath("./ancestor::table")
            nearest_text = (
                nearest_tables[-1].text_content().replace("\xa0", " ")
                if nearest_tables
                else ""
            )
            if "Related" in nearest_text:
                if entry["pacer_case_id"] not in seen_related:
                    seen_related.add(entry["pacer_case_id"])
                    related_cases.append(entry)
            elif entry["pacer_case_id"] not in seen_member:
                seen_member.add(entry["pacer_case_id"])
                member_cases.append(entry)
        return member_cases, related_cases

    @property
    def docket_entries(self):
        entries = super().docket_entries
        # Only fall back to inline parsing for entries the stock parser did not
        # already populate from a structured "view multiple documents" table.
        if any(de.get("attachments") for de in entries):
            return entries
        attachments_by_doc_id = self._parse_inline_attachments()
        for de in entries:
            if de.get("attachments"):
                continue
            attachments = attachments_by_doc_id.get(de.get("pacer_doc_id"))
            if attachments:
                de["attachments"] = attachments
        return entries

    def _parse_inline_attachments(self):
        """Map each entry's main pacer_doc_id to its inline attachments.

        :return: ``{pacer_doc_id: [attachment, ...]}`` for entries that have
        inline attachment links.
        """
        result = {}
        for row in self._get_docket_entry_rows()[1:]:  # Skip the header row.
            doc_anchors = row.xpath(".//a[contains(@href, '/doc1/')]")
            if not doc_anchors:
                continue
            # The first doc1 link in the row is the entry's own document (the
            # "#" column); it precedes any attachment links in the text cell.
            main_doc_id = get_pacer_doc_id_from_doc1_url(
                doc_anchors[0].xpath("./@href")[0]
            )
            desc_cells = [
                td
                for td in row.xpath("./td")
                if "Attachments:" in td.text_content()
            ]
            if not desc_cells:
                continue
            attachments = _parse_attachments_from_cell(desc_cells[0])
            if attachments:
                result[main_doc_id] = attachments
        return result


class TerminationDocketHistoryReport(DocketHistoryReport):
    """A ``DocketHistoryReport`` that also captures per-entry termination dates.

    In a docket history report the "Dates" column can carry a ``Terminated:``
    date alongside ``Filed`` / ``Entered`` (typically on motions later resolved,
    e.g. lead-plaintiff or pro hac vice motions). Juriscraper's stock parser
    reads only the filed and entered dates, so this subclass adds a
    ``date_terminated`` field to every docket entry (``None`` when the entry has
    no termination date), matched by the entry's document number.
    """

    @property
    def docket_entries(self):
        entries = super().docket_entries
        terminated_by_number = self._parse_terminated_dates()
        for de in entries:
            de["date_terminated"] = terminated_by_number.get(
                de.get("document_number")
            )
        return entries

    def _parse_terminated_dates(self):
        """Map document_number -> terminated date for entries that have one.

        :return: ``{document_number: date}`` for entries with a Terminated date.
        """
        result = {}
        docket_header = './/th/text()[contains(., "Description")]'
        rows = self.tree.xpath(f"//table[{docket_header}]/tbody/tr")[1:]
        for row in rows:
            cells = row.xpath("./td")
            if len(cells) != 3:
                # Only the entry's first (3-cell) row holds the number + dates;
                # the second row is the wrapped "Docket Text".
                continue
            document_number = clean_string(cells[0].text_content())
            if not document_number:
                continue
            terminated = self._get_date_terminated(cells[1])
            if terminated:
                result[document_number] = terminated
        return result

    def _get_date_terminated(self, cell):
        """Extract a ``Terminated:`` date from a "Dates" cell, or None."""
        s = clean_string(cell.text_content())
        match = self.date_terminated_regex.search(s)
        if match:
            return convert_date_string(match.group(1))
        return None


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


def docket_number_slug(docket_number):
    """Turn a docket number into a filename-safe slug.

    PACER docket numbers put a colon after the office number
    (``1:25-cv-09596``); we render that as all hyphens so the value is both
    safe and readable in a filename: ``1-25-cv-09596``.

    :param docket_number: A docket number string.
    :return: A filename-safe slug.
    """
    slug = re.sub(r"[\s:/]+", "-", docket_number.strip())
    # Drop anything else that isn't filename-safe.
    return re.sub(r"[^A-Za-z0-9._-]", "", slug)


def report_basename(court_id, docket_number, fallback_docket_number, suffix):
    """Build an output basename like ``nysd_1-25-cv-09596_dkt``.

    :param court_id: The Juriscraper court id.
    :param docket_number: The docket number parsed from the report (preferred,
    since it is normalized), or None/empty.
    :param fallback_docket_number: The docket number to use if the report did
    not yield one (e.g. the value the user searched for).
    :param suffix: A short tag for the report type: ``"dkt"`` or ``"hist"``.
    :return: The basename, without directory or extension.
    """
    docket_number = docket_number or fallback_docket_number
    if docket_number:
        return f"{court_id}_{docket_number_slug(docket_number)}_{suffix}"
    return f"{court_id}_{suffix}"


def scrape_docket(
    session, court_id, pacer_case_id, output_dir, fallback_docket_number=None
):
    """Fetch, save, and parse the full Docket Report.

    :param session: A logged-in ``PacerSession``.
    :param court_id: The Juriscraper court id.
    :param pacer_case_id: The internal PACER case id.
    :param output_dir: Directory to write outputs to.
    :param fallback_docket_number: Docket number to use in the filename if the
    report itself doesn't yield one (e.g. the value the user searched for).
    :return: The parsed docket data dict.
    """
    logger.info("Fetching docket report for pacer_case_id=%s", pacer_case_id)
    report = AttachmentDocketReport(court_id, session)
    report.query(
        pacer_case_id,
        show_parties_and_counsel=True,
        show_terminated_parties=True,
        show_list_of_member_cases=True,
        # Request the "view multiple documents" report; AttachmentDocketReport
        # also sends its "view all attachments" sub-option
        # (view_all_attachments=on), which is what makes PACER render a
        # structured attachment table (with page counts and file sizes) for
        # each entry -- all in a single billable request, more efficient and
        # more accurate than fetching a separate attachment page per document.
        show_multiple_docs=True,
    )

    # Name files after the court and the report's own (normalized) docket
    # number, e.g. nysd_1-25-cv-09596_dkt.{html,json}.
    base = report_basename(
        court_id,
        report.metadata.get("docket_number"),
        fallback_docket_number,
        "dkt",
    )
    html_path = os.path.join(output_dir, f"{base}.html")
    json_path = os.path.join(output_dir, f"{base}.json")

    save_html(report.response.text, html_path)
    # Parse from the saved HTML file to keep download and parse independent.
    data = parse_html_file(AttachmentDocketReport, court_id, html_path)
    save_json(data, json_path)

    logger.info(
        "Docket report parsed: %s docket entries, %s parties",
        len(data.get("docket_entries", [])),
        len(data.get("parties", [])),
    )
    return data


def scrape_docket_history(
    session, court_id, pacer_case_id, output_dir, fallback_docket_number=None
):
    """Fetch, save, and parse the Docket History Report.

    :param session: A logged-in ``PacerSession``.
    :param court_id: The Juriscraper court id.
    :param pacer_case_id: The internal PACER case id.
    :param output_dir: Directory to write outputs to.
    :param fallback_docket_number: Docket number to use in the filename if the
    report itself doesn't yield one (e.g. the value the user searched for).
    :return: The parsed docket history data dict.
    """
    logger.info(
        "Fetching docket history report for pacer_case_id=%s", pacer_case_id
    )
    report = TerminationDocketHistoryReport(court_id, session)
    report.query(
        pacer_case_id,
        query_type="History",
        order_by="asc",
        show_de_descriptions=True,
    )

    # Name files after the court and the report's own (normalized) docket
    # number, e.g. nysd_1-25-cv-09596_hist.{html,json}.
    base = report_basename(
        court_id,
        report.metadata.get("docket_number"),
        fallback_docket_number,
        "hist",
    )
    html_path = os.path.join(output_dir, f"{base}.html")
    json_path = os.path.join(output_dir, f"{base}.json")

    save_html(report.response.text, html_path)
    # Parse from the saved HTML file to keep download and parse independent.
    data = parse_html_file(
        TerminationDocketHistoryReport, court_id, html_path
    )
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
    scrape_docket(
        session,
        args.court,
        pacer_case_id,
        args.output_dir,
        fallback_docket_number=args.docket_number,
    )

    # 4. Docket History Report -> HTML -> JSON.
    scrape_docket_history(
        session,
        args.court,
        pacer_case_id,
        args.output_dir,
        fallback_docket_number=args.docket_number,
    )

    logger.info("Done. Outputs written to %s", os.path.abspath(args.output_dir))


if __name__ == "__main__":
    main()
