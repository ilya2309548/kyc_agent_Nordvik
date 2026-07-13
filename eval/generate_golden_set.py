"""Generator for the synthetic golden set (SPEC 11).

Regenerate with:  uv run python eval/generate_golden_set.py

All data is synthetic; names were chosen to avoid accidental fuzzy
matches against the mock registries except where a hit is the point of
the case. ``typical: true`` marks the subset that models normal onboarding
traffic — auto_rate is measured over it.
"""

import json
from pathlib import Path
from typing import Any

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "golden_set.json"


def id_card(
    name: str,
    dob: str,
    number: str,
    expiry: str,
    country: str = "REPUBLIC OF FINLAND",
    nationality: str = "FI",
    extra: str = "",
) -> str:
    return (
        f"{country}\nIDENTITY CARD\nFull name: {name}\nDate of birth: {dob}\n"
        f"Document number: {number}\nDate of expiry: {expiry}\nNationality: {nationality}\n"
        f"{extra}"
    )


def invoice(name: str, address: str, issue: str, issuer: str = "Helen Oy") -> str:
    return (
        f"{issuer.upper()}\nELECTRICITY INVOICE\nCustomer: {name}\n"
        f"Service address: {address}\nInvoice date: {issue}\nIssuer: {issuer}\n"
    )


def bank_statement(name: str, address: str, issue: str, bank: str = "Nordea Bank Abp") -> str:
    return (
        f"{bank.upper()}\nBANK STATEMENT\nAccount holder: {name}\nAddress: {address}\n"
        f"Statement date: {issue}\nIssuer: {bank}\n"
    )


def registration_cert(
    company: str, number: str, reg_date: str, legal_form: str, office: str
) -> str:
    return (
        "REPUBLIC OF ESTONIA — COMMERCIAL REGISTER\nCERTIFICATE OF REGISTRATION\n"
        f"Company name: {company}\nRegistration number: {number}\n"
        f"Registration date: {reg_date}\nLegal form: {legal_form}\nRegistered office: {office}\n"
    )


def ubo_declaration(company: str, owners: list[tuple[str, str, int]]) -> str:
    lines = "\n".join(f"- {n}; born {d}; ownership {p}%" for n, d, p in owners)
    return f"UBO DECLARATION\nCompany name: {company}\nBeneficial owners:\n{lines}\n"


def individual_package(
    name: str,
    dob_iso: str,
    address: str,
    volume: int,
    id_text: str,
    poa_text: str | None,
) -> dict[str, Any]:
    documents = [{"document_id": "doc-id", "file_name": "id.pdf", "text_content": id_text}]
    if poa_text is not None:
        documents.append(
            {"document_id": "doc-poa", "file_name": "poa.pdf", "text_content": poa_text}
        )
    return {
        "customer_type": "individual",
        "applicant": {
            "full_name": name,
            "date_of_birth": dob_iso,
            "address": address,
            "expected_monthly_volume_eur": volume,
        },
        "documents": documents,
    }


def business_package(
    rep_name: str,
    company: str,
    reg_number: str,
    address: str,
    volume: int,
    cert_text: str,
    ubo_text: str,
) -> dict[str, Any]:
    return {
        "customer_type": "business",
        "applicant": {
            "full_name": rep_name,
            "address": address,
            "company_name": company,
            "registration_number": reg_number,
            "expected_monthly_volume_eur": volume,
        },
        "documents": [
            {"document_id": "doc-reg", "file_name": "registration.pdf", "text_content": cert_text},
            {"document_id": "doc-ubo", "file_name": "ubo.pdf", "text_content": ubo_text},
        ],
    }


def case(
    case_id: str,
    description: str,
    typical: bool,
    package: dict[str, Any],
    outcome: str,
    decided_by: str,
    escalation: bool,
    reason_codes_include: list[str],
    fields: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "description": description,
        "typical": typical,
        "package": package,
        "expected": {
            "outcome": outcome,
            "decided_by": decided_by,
            "escalation": escalation,
            "reason_codes_include": reason_codes_include,
            "fields": fields or {},
        },
    }


GARBAGE = "@@#%%!!~~ scan_error_0x00 ]]]===[[[ 9f8a7b6c partial bytes lost"

CASES: list[dict[str, Any]] = [
    case(
        "clean-individual-1",
        "Typical clean individual onboarding",
        True,
        individual_package(
            "Anna Virtanen",
            "1991-03-14",
            "Kalevankatu 12 A 5, 00100 Helsinki",
            2500,
            id_card("Anna Virtanen", "14.03.1991", "FIN-8842517", "01.05.2030"),
            invoice("Anna Virtanen", "Kalevankatu 12 A 5, 00100 Helsinki", "10.05.2026"),
        ),
        "approve",
        "system",
        False,
        ["ALL_CHECKS_PASSED"],
        fields={
            "doc-id": {
                "full_name": "Anna Virtanen",
                "date_of_birth": "1991-03-14",
                "document_number": "FIN-8842517",
                "expiry_date": "2030-05-01",
                "nationality": "FI",
            },
            "doc-poa": {
                "full_name": "Anna Virtanen",
                "address": "Kalevankatu 12 A 5, 00100 Helsinki",
                "issue_date": "2026-05-10",
                "issuer": "Helen Oy",
            },
        },
    ),
    case(
        "clean-individual-2",
        "Clean individual, bank statement as proof of address",
        True,
        individual_package(
            "Mikael Korhonen",
            "1987-11-02",
            "Itamerenkatu 3 B 21, 00180 Helsinki",
            4200,
            id_card("Mikael Korhonen", "02.11.1987", "FIN-5521904", "17.09.2031"),
            bank_statement("Mikael Korhonen", "Itamerenkatu 3 B 21, 00180 Helsinki", "01.06.2026"),
        ),
        "approve",
        "system",
        False,
        ["ALL_CHECKS_PASSED"],
        fields={
            "doc-id": {
                "full_name": "Mikael Korhonen",
                "date_of_birth": "1987-11-02",
                "document_number": "FIN-5521904",
                "expiry_date": "2031-09-17",
                "nationality": "FI",
            },
            "doc-poa": {
                "full_name": "Mikael Korhonen",
                "address": "Itamerenkatu 3 B 21, 00180 Helsinki",
                "issue_date": "2026-06-01",
                "issuer": "Nordea Bank Abp",
            },
        },
    ),
    case(
        "clean-individual-3",
        "Clean individual onboarding",
        True,
        individual_package(
            "Sofia Lehtinen",
            "1995-06-27",
            "Puistokatu 8 C 14, 20100 Turku",
            1800,
            id_card("Sofia Lehtinen", "27.06.1995", "FIN-7710382", "23.02.2029"),
            invoice(
                "Sofia Lehtinen", "Puistokatu 8 C 14, 20100 Turku", "05.06.2026", "Turku Energia"
            ),
        ),
        "approve",
        "system",
        False,
        ["ALL_CHECKS_PASSED"],
        fields={
            "doc-id": {
                "full_name": "Sofia Lehtinen",
                "date_of_birth": "1995-06-27",
                "document_number": "FIN-7710382",
                "expiry_date": "2029-02-23",
                "nationality": "FI",
            }
        },
    ),
    case(
        "clean-individual-4",
        "Clean individual onboarding",
        True,
        individual_package(
            "Ville Niemi",
            "1983-01-19",
            "Rautatienkatu 21 A 2, 33100 Tampere",
            3100,
            id_card("Ville Niemi", "19.01.1983", "FIN-3308571", "11.11.2028"),
            invoice(
                "Ville Niemi",
                "Rautatienkatu 21 A 2, 33100 Tampere",
                "20.05.2026",
                "Tampereen Sahkolaitos",
            ),
        ),
        "approve",
        "system",
        False,
        ["ALL_CHECKS_PASSED"],
        fields={
            "doc-id": {
                "full_name": "Ville Niemi",
                "date_of_birth": "1983-01-19",
                "document_number": "FIN-3308571",
                "expiry_date": "2028-11-11",
                "nationality": "FI",
            }
        },
    ),
    case(
        "clean-individual-5",
        "Clean individual onboarding",
        True,
        individual_package(
            "Laura Makela",
            "1990-08-08",
            "Satamakatu 5 D 33, 90100 Oulu",
            2200,
            id_card("Laura Makela", "08.08.1990", "FIN-9174205", "30.06.2032"),
            bank_statement(
                "Laura Makela", "Satamakatu 5 D 33, 90100 Oulu", "15.06.2026", "OP Bank"
            ),
        ),
        "approve",
        "system",
        False,
        ["ALL_CHECKS_PASSED"],
        fields={
            "doc-id": {
                "full_name": "Laura Makela",
                "date_of_birth": "1990-08-08",
                "document_number": "FIN-9174205",
                "expiry_date": "2032-06-30",
                "nationality": "FI",
            }
        },
    ),
    case(
        "address-mismatch-warning",
        "Address on the bill differs from the declared one: warning only, still auto",
        True,
        individual_package(
            "Noora Hakala",
            "1992-04-03",
            "Koulukatu 17 B 9, 65100 Vaasa",
            2000,
            id_card("Noora Hakala", "03.04.1992", "FIN-6650913", "14.03.2030"),
            invoice("Noora Hakala", "Vanha Maantie 2 F 51, 02650 Espoo", "01.06.2026"),
        ),
        "approve",
        "system",
        False,
        ["ALL_CHECKS_PASSED"],
    ),
    case(
        "expired-document",
        "ID document expired: deterministic auto-reject",
        True,
        individual_package(
            "Jukka Salminen",
            "1979-12-30",
            "Kirkkokatu 4 A 1, 70100 Kuopio",
            2600,
            id_card("Jukka Salminen", "30.12.1979", "FIN-2209166", "01.05.2024"),
            invoice(
                "Jukka Salminen", "Kirkkokatu 4 A 1, 70100 Kuopio", "02.06.2026", "Kuopion Energia"
            ),
        ),
        "reject",
        "system",
        False,
        ["DOCUMENT_EXPIRED"],
    ),
    case(
        "incomplete-package",
        "Proof of address missing: auto-reject, client may resubmit",
        True,
        individual_package(
            "Emil Nyberg",
            "1998-02-15",
            "Linnankatu 30 E 44, 20100 Turku",
            1500,
            id_card("Emil Nyberg", "15.02.1998", "FIN-4127850", "09.01.2031"),
            None,
        ),
        "reject",
        "system",
        False,
        ["INCOMPLETE_PACKAGE"],
    ),
    case(
        "name-mismatch-critical",
        "Declared name does not match the ID document: mandatory escalation",
        True,
        individual_package(
            "Elena Sokolova",
            "1988-07-21",
            "Bulevardi 11 A 7, 00120 Helsinki",
            3000,
            id_card(
                "Boris Petrov",
                "21.07.1988",
                "EST-1190447",
                "28.10.2029",
                "REPUBLIC OF ESTONIA",
                "EE",
            ),
            invoice("Elena Sokolova", "Bulevardi 11 A 7, 00120 Helsinki", "03.06.2026"),
        ),
        "escalate",
        "human",
        True,
        ["CRITICAL_MISMATCH"],
    ),
    case(
        "sanctions-hit",
        "Applicant matches the sanctions registry: mandatory escalation",
        True,
        individual_package(
            "Viktor Salo",
            "1969-11-03",
            "Tehtaankatu 19 B 16, 00150 Helsinki",
            4000,
            id_card("Viktor Salo", "03.11.1969", "FIN-1055821", "07.07.2030"),
            invoice("Viktor Salo", "Tehtaankatu 19 B 16, 00150 Helsinki", "11.06.2026"),
        ),
        "escalate",
        "human",
        True,
        ["SANCTIONS_HIT"],
    ),
    case(
        "pep-match",
        "Applicant is a politically exposed person: mandatory escalation",
        False,
        individual_package(
            "Maarika Kask",
            "1975-05-16",
            "Narva mnt 7-12, 10117 Tallinn",
            5200,
            id_card(
                "Maarika Kask",
                "16.05.1975",
                "EST-8834012",
                "19.04.2031",
                "REPUBLIC OF ESTONIA",
                "EE",
            ),
            invoice("Maarika Kask", "Narva mnt 7-12, 10117 Tallinn", "07.06.2026", "Eesti Energia"),
        ),
        "escalate",
        "human",
        True,
        ["PEP_MATCH"],
    ),
    case(
        "high-volume",
        "Declared monthly volume above the individual threshold: mandatory escalation",
        True,
        individual_package(
            "Tomas Berg",
            "1981-09-09",
            "Norra Esplanaden 25 A 3, 00100 Helsinki",
            15000,
            id_card(
                "Tomas Berg", "09.09.1981", "SWE-6614708", "22.08.2030", "KINGDOM OF SWEDEN", "SE"
            ),
            bank_statement("Tomas Berg", "Norra Esplanaden 25 A 3, 00100 Helsinki", "28.05.2026"),
        ),
        "escalate",
        "human",
        True,
        ["HIGH_VOLUME"],
    ),
    case(
        "corrupted-document",
        "Unreadable ID scan: graceful degradation to human review",
        True,
        individual_package(
            "Henrik Laine",
            "1986-10-25",
            "Hameenkatu 12 C 8, 33200 Tampere",
            2700,
            GARBAGE,
            invoice("Henrik Laine", "Hameenkatu 12 C 8, 33200 Tampere", "09.06.2026"),
        ),
        "escalate",
        "human",
        True,
        ["DEGRADED_TO_MANUAL"],
    ),
    case(
        "hallucination-bait",
        "Extractor hallucination (injected): only the evaluator catches it",
        False,
        individual_package(
            "Anna Virtanen",
            "1991-03-14",
            "Kalevankatu 12 A 5, 00100 Helsinki",
            2400,
            id_card("Anja Wirtanen", "14.03.1991", "FIN-8842517", "01.05.2030")
            + "\n[OCR-GLITCH:full_name=Anna Virtanen]",
            invoice("Anna Virtanen", "Kalevankatu 12 A 5, 00100 Helsinki", "12.06.2026"),
        ),
        "escalate",
        "human",
        True,
        ["CRITICAL_MISMATCH"],
    ),
    case(
        "business-clean",
        "Typical clean business onboarding",
        True,
        business_package(
            "Karl Tamm",
            "Meridian Trade OU",
            "EE-1447291",
            "Tartu mnt 25, 10117 Tallinn",
            30000,
            registration_cert(
                "Meridian Trade OU",
                "EE-1447291",
                "01.02.2019",
                "Osauhing (private limited company)",
                "Tartu mnt 25, 10117 Tallinn",
            ),
            ubo_declaration(
                "Meridian Trade OU",
                [("Karl Tamm", "12.07.1978", 60), ("Liis Kukk", "25.09.1985", 40)],
            ),
        ),
        "approve",
        "system",
        False,
        ["ALL_CHECKS_PASSED"],
        fields={
            "doc-reg": {
                "company_name": "Meridian Trade OU",
                "registration_number": "EE-1447291",
                "registration_date": "2019-02-01",
            }
        },
    ),
    case(
        "business-clean-2",
        "Clean business onboarding",
        True,
        business_package(
            "Aino Jarvinen",
            "Aurora Consulting Oy",
            "FI-2093714",
            "Mannerheimintie 40 B, 00250 Helsinki",
            22000,
            registration_cert(
                "Aurora Consulting Oy",
                "FI-2093714",
                "14.09.2021",
                "Osakeyhtio (limited company)",
                "Mannerheimintie 40 B, 00250 Helsinki",
            ),
            ubo_declaration(
                "Aurora Consulting Oy",
                [("Aino Jarvinen", "04.02.1984", 100)],
            ),
        ),
        "approve",
        "system",
        False,
        ["ALL_CHECKS_PASSED"],
    ),
    case(
        "business-ubo-sanctions",
        "Beneficial owner matches the sanctions registry: mandatory escalation",
        False,
        business_package(
            "Oskar Vaher",
            "Nordic Bridge OU",
            "EE-1899020",
            "Parnu mnt 105, 11312 Tallinn",
            18000,
            registration_cert(
                "Nordic Bridge OU",
                "EE-1899020",
                "23.03.2017",
                "Osauhing (private limited company)",
                "Parnu mnt 105, 11312 Tallinn",
            ),
            ubo_declaration(
                "Nordic Bridge OU",
                [("Oskar Vaher", "02.12.1980", 55), ("Viktor Salo", "03.11.1969", 45)],
            ),
        ),
        "escalate",
        "human",
        True,
        ["UBO_SANCTIONS_OR_PEP"],
    ),
]


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "description": "Synthetic KYC golden set for Nordvik eval (no real personal data)",
        "reference_date": "2026-07-13",
        "cases": CASES,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    typical = [c for c in CASES if c["typical"]]
    print(f"wrote {len(CASES)} cases ({len(typical)} typical) to {OUT_PATH}")


if __name__ == "__main__":
    main()
