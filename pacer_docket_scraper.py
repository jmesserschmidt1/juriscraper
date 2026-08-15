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
(the codes shown at the top, e.g. ECF, LEAD), its ``lead_case`` /
``member_cases`` / ``related_cases`` references (each with docket number,
court, and pacer_case_id), and its ``cases_in_other_courts`` (court name and
docket number for cases in another court system, which have no pacer_case_id).
Each docket entry also carries its source row under an ``html`` key (the entry
as it appears in the parsed, script-stripped tree), for testing, validation, or
later re-parsing;
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

The script runs in one of three modes, chosen by the options you pass:

* Live fetch (default) -- search PACER and download+parse both reports::

      export PACER_USERNAME=your_username
      export PACER_PASSWORD=your_password
      python pacer_docket_scraper.py --court cand \
          --docket-number 4:06-cv-07294 --output-dir ./pacer_output

      # With MFA: you'll be prompted for the one-time passcode, or pass it in:
      python pacer_docket_scraper.py --court cand \
          --docket-number 4:06-cv-07294 --otp 123456

* Local parse -- turn already-downloaded HTML into JSON, no PACER login::

      python pacer_docket_scraper.py --court nysd \
          --docket-html ./nysd_1-25-cv-09596_dkt.html \
          --history-html ./nysd_1-25-cv-09596_hist.html

* Bulk CSV -- process many cases from a CSV with columns ``court``,
  ``docket_number``, ``docket_html``, ``history_html``. A row with an html
  path is parsed locally; a row with ``court`` + ``docket_number`` is fetched
  from PACER (login happens once, on the first live row, so an all-local CSV
  needs no credentials). A failing row is logged and skipped::

      python pacer_docket_scraper.py --csv ./cases.csv --output-dir ./out

Credentials may also be supplied with ``--username`` / ``--password``. When the
password or OTP is omitted and the script is run interactively, it prompts for
them (the password without echo) rather than reading them off the command line.

NOTE: PACER is a paid service. Running this against the live system incurs
charges for the docket pages it retrieves. There is no free "test" mode here;
point it at a case you actually intend to purchase.
"""

import argparse
import csv
import datetime
import getpass
import json
import logging
import os
import re
import sys

from lxml.html import tostring

from juriscraper.lib.log_tools import make_default_logger
from juriscraper.lib.string_utils import (
    clean_string,
    convert_date_string,
    force_unicode,
)
from juriscraper.pacer import (
    DocketHistoryReport,
    DocketReport,
    PossibleCaseNumberApi,
)
from juriscraper.pacer.http import PacerSession
from juriscraper.pacer.utils import get_pacer_doc_id_from_doc1_url

logger = make_default_logger()


class CaseNotFound(Exception):
    """Raised when a docket number can't be resolved to a PACER case."""


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
        # the docket sheet's flag codes and its case-reference fields.
        if data and "flags" not in data:
            data["flags"] = self._parse_flags()
            lead_case, member_cases, related_cases = (
                self._parse_case_references()
            )
            data["lead_case"] = lead_case
            data["member_cases"] = member_cases
            data["related_cases"] = related_cases
            data["cases_in_other_courts"] = self._parse_other_court_cases()
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

    @staticmethod
    def _classify_case_link(anchor):
        """Classify a caption case link as lead / related / member.

        The caption labels each case link ("Lead case:", "Related Case:",
        "Member case:") in the text just before the link -- inline for lead
        cases, in a preceding table cell for related/member. We therefore walk
        the link's preceding text nearest-first and take the first label we
        find. (PACER writes these labels with non-breaking spaces, so we
        normalize those first.)

        :param anchor: The lxml ``<a>`` element.
        :return: ``"lead"``, ``"related"``, ``"member"``, or ``None``.
        """
        for text in reversed(anchor.xpath("./preceding::text()")):
            s = text.replace("\xa0", " ")
            if "Lead case" in s:
                return "lead"
            if "Related" in s and "Case" in s:
                return "related"
            if "Member case" in s or "Member Case" in s:
                return "member"
        return None

    def _parse_case_references(self):
        """Parse the caption's lead / member / related case links.

        Each appears near the case caption as a link to another case's docket
        report (``DktRpt.pl?<pacer_case_id>``), with the sibling case's docket
        number as the link text. We scope to links inside the caption cell (the
        one carrying "Assigned to") so docket-entry links are ignored, and
        classify each by its nearest preceding label. The "(View Member Case)"
        link uses a different endpoint (``AsccaseDisplay.pl``) and so is
        naturally skipped.

        :return: A three-tuple ``(lead_case, member_cases, related_cases)``.
        ``lead_case`` is a single dict or ``None``; the others are lists. Every
        entry has ``docket_number``, ``court_id``, and ``pacer_case_id``.
        """
        buckets = {"lead": [], "member": [], "related": []}
        seen = {"lead": set(), "member": set(), "related": set()}

        anchors = self.tree.xpath(
            '//a[contains(@href, "DktRpt.pl?")]'
            '[ancestor::td[contains(., "Assigned to")]]'
        )
        for anchor in anchors:
            href = anchor.get("href", "")
            # A case link's query is exactly the pacer_case_id (all digits),
            # which also excludes the "Select all / clear" buttons whose query
            # is a session id like "100591727900924-L_1_0-1".
            id_match = re.search(r"DktRpt\.pl\?(\d+)(?:[#&]|$)", href)
            raw_number = clean_string(anchor.text_content())
            if not id_match or not raw_number:
                continue
            category = self._classify_case_link(anchor)
            if category is None:
                continue

            # The link text carries the judge initials (e.g.
            # "1:22-cv-06339-AS"); keep just the docket number itself.
            number_match = self.docket_number_dist_regex.search(raw_number)
            docket_number = (
                number_match.group(1) if number_match else raw_number
            )
            pacer_case_id = id_match.group(1)
            if pacer_case_id in seen[category]:
                continue
            seen[category].add(pacer_case_id)

            court_match = re.search(r"ecf\.([^.]+)\.uscourts\.gov", href)
            buckets[category].append(
                {
                    "docket_number": docket_number,
                    "court_id": court_match.group(1)
                    if court_match
                    else self.court_id,
                    "pacer_case_id": pacer_case_id,
                }
            )

        lead_case = buckets["lead"][0] if buckets["lead"] else None
        return lead_case, buckets["member"], buckets["related"]

    def _parse_other_court_cases(self):
        """Parse the caption's "Case in other court" entries.

        These reference cases in a different court system, so PACER shows them
        as plain text (a court name and docket number, no link), e.g.
        "Ohio Southern, 2:22-cv-02371". The label sits in one table cell with
        the value in the next; the docket number is the last comma-separated
        piece and the court name is what precedes it.

        :return: A list of dicts with ``court`` and ``docket_number`` (no
        ``pacer_case_id``, since the case lives in another court's system).
        """
        result = []
        for label_td in self.tree.xpath("//td"):
            label = clean_string(label_td.text_content().replace("\xa0", " "))
            if not label.startswith("Case in other court"):
                continue
            value_tds = label_td.xpath("./following-sibling::td[1]")
            if not value_tds:
                continue
            value = clean_string(
                value_tds[0].text_content().replace("\xa0", " ")
            )
            # Multiple entries may be separated by newlines or semicolons.
            for entry in re.split(r"[;\n]+", value):
                entry = entry.strip().strip(",").strip()
                if not entry:
                    continue
                court, _, docket_number = entry.rpartition(",")
                result.append(
                    {
                        "court": court.strip() or entry,
                        "docket_number": docket_number.strip() or None,
                    }
                )
        return result

    @property
    def docket_entries(self):
        entries = super().docket_entries
        self._attach_inline_attachments(entries)
        self._attach_entry_html(entries)
        return entries

    def _attach_inline_attachments(self, entries):
        """Fill in attachments the stock parser dropped (plain-report links).

        :param entries: The list of parsed docket-entry dicts (mutated in
        place).
        :return: None
        """
        # If the stock parser already populated structured attachments from a
        # "view multiple documents" table, prefer those and do nothing.
        if any(de.get("attachments") for de in entries):
            return
        attachments_by_doc_id = self._parse_inline_attachments()
        for de in entries:
            if de.get("attachments"):
                continue
            attachments = attachments_by_doc_id.get(de.get("pacer_doc_id"))
            if attachments:
                de["attachments"] = attachments

    def _attach_entry_html(self, entries):
        """Attach each docket entry's source HTML under an ``html`` key.

        This captures the original ``<tr>`` for each entry (as it appears in
        the parsed, script-stripped tree the parser works on) so entries can be
        re-parsed, diffed, or validated later. The rows are collected with the
        same acceptance logic the stock parser uses, so they line up one-to-one
        with the parsed entries; if for some reason they don't, the HTML is
        left off rather than risk mis-pairing.

        :param entries: The list of parsed docket-entry dicts (mutated in
        place).
        :return: None
        """
        row_htmls = self._entry_row_htmls()
        if len(row_htmls) != len(entries):
            logger.warning(
                "Docket entry row/entry count mismatch (%s rows vs %s "
                "entries); skipping per-entry HTML.",
                len(row_htmls),
                len(entries),
            )
            return
        for de, html in zip(entries, row_htmls):
            de["html"] = html

    def _entry_row_htmls(self):
        """Return each accepted docket-entry row's HTML, in parse order.

        Mirrors ``DocketReport.docket_entries``' row selection so the results
        pair positionally with the parsed entries (attachment sub-rows and
        blank/continuation rows are skipped, as there).

        :return: A list of HTML strings, one per accepted entry row.
        """
        rows = self._get_docket_entry_rows()[1:]  # Skip the header row.
        view_multiple_documents = bool(
            self.tree.xpath("//form[@name='view_multi_docs']")
        )
        htmls = []
        for row in rows:
            cells = row.xpath("./td[not(./input)]")
            if view_multiple_documents and len(cells) == 4:
                if not cells[2].text_content().strip():
                    del cells[2]
            if view_multiple_documents and len(cells) == 5:
                del cells[3]
            if len(cells) == 0:
                continue
            if len(cells) == 4:
                del cells[1]
            if not force_unicode(cells[0].text_content()).strip():
                # Blank date cell -> attachment sub-row or continuation.
                continue
            document_number = self._get_document_number(cells[1])
            if document_number is not None and not document_number.isdigit():
                # e.g. courts that use "doc" instead of a number; the stock
                # parser skips these, so we do too.
                continue
            htmls.append(tostring(row, encoding="unicode"))
        return htmls

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
    :raises CaseNotFound: if the case cannot be found.
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
        raise CaseNotFound(
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
    return _parse_report_data(
        report_class, court_id, read_html_file(html_path)
    )


def read_html_file(path):
    """Read a local HTML report file into text, tolerating common encodings.

    PACER pages are variously served as UTF-8, Windows-1252, or Latin-1, so we
    try each in turn; that way a report saved by any tool can still be parsed.

    :param path: Path to the HTML file.
    :return: The file's contents as a unicode string.
    """
    with open(path, "rb") as f:
        raw = f.read()
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_report_data(report_class, court_id, html_text):
    """Parse report HTML text into a JSON-ready dict.

    :param report_class: A report class such as ``AttachmentDocketReport``.
    :param court_id: The Juriscraper court id the HTML came from.
    :param html_text: The report HTML as a unicode string.
    :return: The parsed ``.data`` dict.
    """
    report = report_class(court_id)
    report._parse_text(html_text)
    return report.data


def process_docket_html(
    court_id,
    html_text,
    output_dir,
    fallback_docket_number=None,
    save_source_html=False,
):
    """Parse docket-report HTML into JSON and save it (optionally the HTML too).

    :param court_id: The Juriscraper court id the HTML came from.
    :param html_text: The docket report HTML as a unicode string.
    :param output_dir: Directory to write outputs to.
    :param fallback_docket_number: Docket number to use in the filename if the
    report itself doesn't yield one.
    :param save_source_html: Whether to also write the source HTML next to the
    JSON (used for live fetches; skipped when parsing an existing local file).
    :return: The parsed docket data dict.
    """
    data = _parse_report_data(AttachmentDocketReport, court_id, html_text)
    base = report_basename(
        court_id, data.get("docket_number"), fallback_docket_number, "dkt"
    )
    if save_source_html:
        save_html(html_text, os.path.join(output_dir, f"{base}.html"))
    save_json(data, os.path.join(output_dir, f"{base}.json"))
    logger.info(
        "Docket report parsed: %s entries, %s parties, flags=%s, "
        "%s member / %s related cases",
        len(data.get("docket_entries", [])),
        len(data.get("parties", [])),
        data.get("flags", []),
        len(data.get("member_cases", [])),
        len(data.get("related_cases", [])),
    )
    return data


def process_history_html(
    court_id,
    html_text,
    output_dir,
    fallback_docket_number=None,
    save_source_html=False,
):
    """Parse docket-history HTML into JSON and save it (optionally the HTML too).

    :param court_id: The Juriscraper court id the HTML came from.
    :param html_text: The docket history report HTML as a unicode string.
    :param output_dir: Directory to write outputs to.
    :param fallback_docket_number: Docket number to use in the filename if the
    report itself doesn't yield one.
    :param save_source_html: Whether to also write the source HTML next to the
    JSON (used for live fetches; skipped when parsing an existing local file).
    :return: The parsed docket history data dict.
    """
    data = _parse_report_data(
        TerminationDocketHistoryReport, court_id, html_text
    )
    base = report_basename(
        court_id, data.get("docket_number"), fallback_docket_number, "hist"
    )
    if save_source_html:
        save_html(html_text, os.path.join(output_dir, f"{base}.html"))
    save_json(data, os.path.join(output_dir, f"{base}.json"))
    logger.info(
        "Docket history report parsed: %s entries",
        len(data.get("docket_entries", [])),
    )
    return data


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
    # Save the fetched HTML alongside the JSON (files are named after the
    # court and the report's own docket number, e.g. nysd_1-25-cv-09596_dkt).
    return process_docket_html(
        court_id,
        report.response.text,
        output_dir,
        fallback_docket_number,
        save_source_html=True,
    )


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
    return process_history_html(
        court_id,
        report.response.text,
        output_dir,
        fallback_docket_number,
        save_source_html=True,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Search PACER for a case by docket number and court, download the "
            "Docket Report and Docket History Report as HTML, and parse each "
            "into JSON."
        ),
        epilog=(
            "Modes (chosen automatically by the options you pass):\n"
            "  Live fetch (default): --court and --docket-number search PACER,\n"
            "      download both reports, and parse them (requires login).\n"
            "  Local parse: --docket-html and/or --history-html parse existing\n"
            "      HTML files into JSON (no login). --court is still required.\n"
            "  Bulk CSV: --csv processes many cases from a CSV with columns\n"
            "      court, docket_number, docket_html, history_html. A row with\n"
            "      an html path is parsed locally; a row with court+docket_number\n"
            "      is fetched live (login happens once, on the first live row).\n"
        ),
    )
    parser.add_argument(
        "--court",
        default=None,
        help="Juriscraper court id (e.g. 'cand', 'nysd', 'txsd').",
    )
    parser.add_argument(
        "--docket-number",
        default=None,
        help="Docket number to search for (e.g. '4:06-cv-07294').",
    )
    parser.add_argument(
        "--docket-html",
        default=None,
        help=(
            "Path to a local docket report HTML file to parse into JSON "
            "(no PACER login). Requires --court."
        ),
    )
    parser.add_argument(
        "--history-html",
        default=None,
        help=(
            "Path to a local docket history report HTML file to parse into "
            "JSON (no PACER login). Requires --court."
        ),
    )
    parser.add_argument(
        "--csv",
        default=None,
        help=(
            "Path to a CSV for bulk processing. Recognized columns: court, "
            "docket_number, docket_html, history_html. Rows with an html path "
            "are parsed locally; rows with court+docket_number are fetched "
            "from PACER."
        ),
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


def login_from_args(args):
    """Resolve credentials from parsed args and log into PACER.

    :param args: The parsed argparse namespace.
    :return: A logged-in ``MfaPacerSession``.
    :raises SystemExit: if a username or password can't be obtained.
    """
    if not args.username:
        sys.exit(
            "A PACER username is required for live fetches. Set PACER_USERNAME "
            "or pass --username."
        )
    password = resolve_password(args.password)
    if not password:
        sys.exit(
            "A PACER password is required for live fetches. Set PACER_PASSWORD, "
            "pass --password, or run interactively to be prompted."
        )
    # Acquire the OTP (if any) as late as possible so a rotating authenticator
    # code is still valid at login.
    otp_code = resolve_otp(args.otp)
    return build_session(args.username, password, args.client_code, otp_code)


def fetch_case(session, court_id, pacer_case_id, docket_number, output_dir):
    """Fetch, save, and parse both reports for one case from PACER.

    :param session: A logged-in ``PacerSession``.
    :param court_id: The Juriscraper court id.
    :param pacer_case_id: The internal PACER case id.
    :param docket_number: The docket number (used for filenames/logging).
    :param output_dir: Directory to write outputs to.
    :return: None
    """
    scrape_docket(
        session, court_id, pacer_case_id, output_dir, docket_number
    )
    scrape_docket_history(
        session, court_id, pacer_case_id, output_dir, docket_number
    )


def run_live(args):
    """Live mode: search PACER for one case and process both reports."""
    if not args.court:
        sys.exit("--court is required for a live PACER fetch.")
    if not (args.docket_number or args.pacer_case_id):
        sys.exit(
            "--docket-number (or --pacer-case-id) is required for a live "
            "PACER fetch."
        )

    session = login_from_args(args)
    if args.pacer_case_id:
        pacer_case_id = args.pacer_case_id
        logger.info("Using supplied pacer_case_id=%s", pacer_case_id)
    else:
        try:
            pacer_case_id = get_pacer_case_id(
                session, args.court, args.docket_number
            )
        except CaseNotFound as exc:
            sys.exit(str(exc))
    fetch_case(
        session,
        args.court,
        pacer_case_id,
        args.docket_number,
        args.output_dir,
    )


def run_local(args):
    """Local mode: parse existing HTML file(s) into JSON, no PACER login."""
    if not args.court:
        sys.exit(
            "--court is required to parse local HTML (it drives doc-id "
            "prefixes and the output filenames)."
        )
    if args.docket_html:
        process_docket_html(
            args.court,
            read_html_file(args.docket_html),
            args.output_dir,
            args.docket_number,
        )
    if args.history_html:
        process_history_html(
            args.court,
            read_html_file(args.history_html),
            args.output_dir,
            args.docket_number,
        )


def run_csv(args):
    """Bulk mode: process each row of a CSV (local parse and/or live fetch).

    Recognized columns (case-insensitive): ``court``, ``docket_number``,
    ``docket_html``, ``history_html``. A row with an html path is parsed
    locally; a row with ``court`` + ``docket_number`` is fetched from PACER.
    Login happens lazily on the first live row, so a CSV of only local files
    needs no credentials. A failing row is logged and skipped so one bad case
    doesn't abort the whole run.
    """
    session = None  # Created lazily on the first row that needs PACER.
    processed = 0
    # utf-8-sig transparently strips a BOM if the CSV has one.
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit(f"CSV '{args.csv}' appears to be empty.")
        for line_number, raw_row in enumerate(reader, start=2):
            row = {
                (k or "").strip().lower(): (v or "").strip()
                for k, v in raw_row.items()
            }
            court = row.get("court", "")
            docket_number = row.get("docket_number", "")
            docket_html = row.get("docket_html", "")
            history_html = row.get("history_html", "")
            try:
                if docket_html or history_html:
                    if not court:
                        logger.warning(
                            "Row %s: 'court' is required to parse local HTML; "
                            "skipping.",
                            line_number,
                        )
                        continue
                    if docket_html:
                        process_docket_html(
                            court,
                            read_html_file(docket_html),
                            args.output_dir,
                            docket_number or None,
                        )
                    if history_html:
                        process_history_html(
                            court,
                            read_html_file(history_html),
                            args.output_dir,
                            docket_number or None,
                        )
                    processed += 1
                elif court and docket_number:
                    if session is None:
                        session = login_from_args(args)
                    pacer_case_id = get_pacer_case_id(
                        session, court, docket_number
                    )
                    fetch_case(
                        session,
                        court,
                        pacer_case_id,
                        docket_number,
                        args.output_dir,
                    )
                    processed += 1
                else:
                    logger.warning(
                        "Row %s: need 'court'+'docket_number' (live) or a "
                        "'docket_html'/'history_html' path; skipping.",
                        line_number,
                    )
            except SystemExit:
                # Missing credentials on the first live row is fatal.
                raise
            except Exception as exc:
                logger.error("Row %s failed: %s", line_number, exc)
    logger.info("CSV run complete: processed %s row(s).", processed)


def main(argv=None):
    args = parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    os.makedirs(args.output_dir, exist_ok=True)

    # Pick the mode from the options provided: bulk CSV, local file(s), or a
    # live single-case fetch (the default).
    if args.csv:
        run_csv(args)
    elif args.docket_html or args.history_html:
        run_local(args)
    else:
        run_live(args)

    logger.info("Done. Outputs written to %s", os.path.abspath(args.output_dir))


if __name__ == "__main__":
    main()
