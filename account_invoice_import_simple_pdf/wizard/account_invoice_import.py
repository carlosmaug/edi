# Copyright 2021 Akretion France (http://www.akretion.com/)
# @author: Alexis de Lattre <alexis.delattre@akretion.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from tempfile import NamedTemporaryFile

from odoo import _, api, models
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)

try:
    import fitz
except ImportError:
    logger.debug("Cannot import PyMuPDF")
try:
    import regex
except ImportError:
    logger.debug("Cannot import regex")


class AccountInvoiceImport(models.TransientModel):
    _inherit = "account.invoice.import"

    @api.model
    def fallback_parse_pdf_invoice(self, file_data):
        """This method must be inherited by additional modules with
        the same kind of logic as the account_bank_statement_import_*
        modules"""
        return self.simple_pdf_parse_invoice(file_data)

    @api.model
    def simple_pdf_text_extraction(self, file_data, test_info):
        res = {}
        fileobj = NamedTemporaryFile("wb", prefix="odoo-simple-pdf-", suffix=".pdf")
        fileobj.write(file_data)
        # Extract text from PDF
        # Very interesting reading:
        # https://github.com/erfelipe/PDFtextExtraction
        res["all"] = ""
        doc = fitz.open(fileobj.name)
        for page in doc:
            res["all"] += page.getText("text")
        res["first"] = doc[0].getText("text")
        res["all_no_space"] = regex.sub(
            "%s+" % test_info["space_pattern"], "", res["all"]
        )
        res["first_no_space"] = regex.sub(
            "%s+" % test_info["space_pattern"], "", res["first"]
        )
        fileobj.close()
        return res

    @api.model
    def simple_pdf_match_partner(self, raw_text_no_space, test_results=None):
        if test_results is None:
            test_results = []
        partner_id = False
        rpo = self.env["res.partner"]
        # Warning: invoices have the VAT number of the supplier, but they often
        # also have the VAT number of the customer (i.e. the VAT number of our company)
        # So we exclude it from the search
        partners = rpo.search_read(
            [
                "|",
                ("vat", "!=", False),
                ("simple_pdf_keyword", "!=", False),
                ("parent_id", "=", False),
                ("is_company", "=", True),
                ("id", "!=", self.env.company.partner_id.id),
            ]
        )
        for partner in partners:
            if partner["simple_pdf_keyword"] and partner["simple_pdf_keyword"].strip():
                keywords = partner["simple_pdf_keyword"].replace(" ", "").split("|")
                found_res = [keyword in raw_text_no_space for keyword in keywords]
                if all(found_res):
                    partner_id = partner["id"]
                    result_label = _("Successful match on %d keywords (%s)") % (
                        len(keywords),
                        ", ".join(keywords),
                    )
                    test_results.append("<li>%s</li>" % result_label)
                    break
            elif partner["vat"]:
                if partner["vat"] in raw_text_no_space:
                    partner_id = partner["id"]
                    result_label = (
                        _("Successful match on VAT number '%s'") % partner["vat"]
                    )
                    test_results.append("<li>%s</li>" % result_label)
                    break
        return partner_id

    @api.model
    def _get_space_pattern(self):
        # https://en.wikipedia.org/wiki/Whitespace_character
        space_ints = [
            32,
            160,
            8192,
            8193,
            8194,
            8195,
            8196,
            8197,
            8198,
            8199,
            8200,
            8201,
            8202,
            8239,
            8287,
        ]
        return "[%s]" % "".join([chr(x) for x in space_ints])

    @api.model
    def _simple_pdf_update_test_info(self, test_info):
        aiispfo = self.env["account.invoice.import.simple.pdf.fields"]
        test_info.update(
            {
                "date_format_sel": dict(
                    aiispfo.fields_get("date_format", "selection")["date_format"][
                        "selection"
                    ]
                ),
                "field_name_sel": dict(
                    aiispfo.fields_get("name", "selection")["name"]["selection"]
                ),
                "extract_rule_sel": dict(
                    aiispfo.fields_get("extract_rule", "selection")["extract_rule"][
                        "selection"
                    ]
                ),
                "space_pattern": self._get_space_pattern(),
            }
        )

    @api.model
    def simple_pdf_parse_invoice(self, file_data, test_info=None):
        if test_info is None:
            test_info = {"test_mode": False}
        self._simple_pdf_update_test_info(test_info)
        rpo = self.env["res.partner"]
        logger.info("Trying to analyze PDF invoice with simple pdf module")
        raw_text_dict = self.simple_pdf_text_extraction(file_data, test_info)
        partner_id = self.simple_pdf_match_partner(raw_text_dict["all_no_space"])
        if not partner_id:
            raise UserError(_("Simple PDF Import: count not find Vendor."))
        partner = rpo.browse(partner_id)
        raw_text = (
            partner.simple_pdf_pages == "first"
            and raw_text_dict["first"]
            or raw_text_dict["all"]
        )
        logger.info(
            "Simple pdf import found partner %s ID %d", partner.display_name, partner_id
        )
        partner_config = partner._simple_pdf_partner_config()
        parsed_inv = {
            "partner": {"recordset": partner},
            "currency": {"recordset": partner_config["currency"]},
            "failed_fields": [],
            "chatter_msg": [],
        }

        # Check field config
        for field in partner.simple_pdf_field_ids:
            logger.debug("Working on field %s", field.name)
            if field.name.startswith("date"):
                field._get_date(parsed_inv, raw_text, partner_config, test_info)
            elif field.name.startswith("amount_"):
                field._get_amount(parsed_inv, raw_text, partner_config, test_info)
            elif field.name == "invoice_number":
                field._get_invoice_number(
                    parsed_inv, raw_text, partner_config, test_info
                )
            elif field.name == "description":
                field._get_description(parsed_inv, raw_text, partner_config, test_info)

        failed_fields = parsed_inv.pop("failed_fields")
        if failed_fields:
            parsed_inv["chatter_msg"].append(
                _("<b>Failed</b> to extract the following field(s): %s.")
                % ", ".join(
                    [
                        "<b>%s</b>" % test_info["field_name_sel"][failed_field]
                        for failed_field in failed_fields
                    ]
                )
            )

        logger.info("simple pdf parsed_inv=%s", parsed_inv)
        return parsed_inv
