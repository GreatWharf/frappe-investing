"""Native form dashboard for Portfolio, mirroring Lending's Loan dashboard.

Groups the doctypes that link directly to Portfolio. Both link fields are
named `portfolio`, so no `non_standard_fieldnames` overrides are needed.
Investment Event reaches a portfolio only through its account, so it is
deliberately excluded — counting it here would match on the wrong field.
"""


def get_data():
    return {
        "fieldname": "portfolio",
        "transactions": [
            {
                "label": "Accounts",
                "items": ["Investment Account"],
            },
            {
                "label": "Valuation",
                "items": ["Portfolio Snapshot"],
            },
        ],
    }
