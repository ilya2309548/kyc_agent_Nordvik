"""Unit tests for deterministic validation rules (SPEC 7.1, 7.2)."""

from datetime import date

from kyc_agent.rules.ids import RuleId
from kyc_agent.rules.validation import (
    MatchThresholds,
    check_document_expiry,
    check_package_completeness,
    fuzzy_ratio,
    run_validation_rules,
)
from kyc_agent.schemas.case import ApplicantDeclared, CustomerType
from kyc_agent.schemas.decisions import ExtractionResult, Severity
from kyc_agent.schemas.documents import DocumentType

TODAY = date(2026, 7, 12)


def make_applicant(**overrides: object) -> ApplicantDeclared:
    defaults: dict[str, object] = {
        "full_name": "Anna Virtanen",
        "date_of_birth": date(1991, 3, 14),
        "address": "Kalevankatu 12 A 5, 00100 Helsinki",
        "expected_monthly_volume_eur": 2500,
    }
    defaults.update(overrides)
    return ApplicantDeclared.model_validate(defaults)


def make_id_extraction(**field_overrides: object) -> ExtractionResult:
    fields: dict[str, object] = {
        "full_name": "Anna Virtanen",
        "date_of_birth": "1991-03-14",
        "document_number": "FIN-8842517",
        "expiry_date": "2030-05-01",
        "nationality": "FI",
    }
    fields.update(field_overrides)
    return ExtractionResult(
        document_id="doc-1",
        doc_type=DocumentType.ID_DOCUMENT,
        fields=fields,
        field_confidence={k: 0.99 for k in fields},
    )


class TestPackageCompleteness:
    def test_complete_individual_package(self) -> None:
        flag = check_package_completeness(
            CustomerType.INDIVIDUAL,
            {DocumentType.ID_DOCUMENT, DocumentType.PROOF_OF_ADDRESS},
        )
        assert flag is None

    def test_missing_proof_of_address(self) -> None:
        flag = check_package_completeness(CustomerType.INDIVIDUAL, {DocumentType.ID_DOCUMENT})
        assert flag is not None
        assert flag.rule_id == RuleId.INCOMPLETE_PACKAGE
        assert "proof_of_address" in flag.details

    def test_business_requires_registration_and_ubo(self) -> None:
        flag = check_package_completeness(
            CustomerType.BUSINESS, {DocumentType.BUSINESS_REGISTRATION}
        )
        assert flag is not None
        assert "ubo_declaration" in flag.details


class TestDocumentExpiry:
    def test_valid_document(self) -> None:
        assert check_document_expiry(date(2030, 1, 1), TODAY) is None

    def test_expired_document(self) -> None:
        flag = check_document_expiry(date(2024, 1, 1), TODAY)
        assert flag is not None
        assert flag.rule_id == RuleId.DOC_EXPIRED
        assert flag.severity is Severity.CRITICAL

    def test_unknown_expiry_is_not_flagged_here(self) -> None:
        # Missing expiry surfaces as EXTRACTION_INCOMPLETE, not DOC_EXPIRED.
        assert check_document_expiry(None, TODAY) is None


class TestNameMatching:
    def test_exact_match_passes(self) -> None:
        checks, flags = run_validation_rules(make_applicant(), [make_id_extraction()], TODAY)
        assert all(c.match for c in checks)
        assert flags == []

    def test_case_and_spacing_are_normalized(self) -> None:
        _, flags = run_validation_rules(
            make_applicant(), [make_id_extraction(full_name="ANNA  VIRTANEN")], TODAY
        )
        assert flags == []

    def test_slight_typo_is_warning(self) -> None:
        # "Anna Virtanen" vs "Anna Virtanem": one letter off => warning band.
        _, flags = run_validation_rules(
            make_applicant(), [make_id_extraction(full_name="Anna Virtanem")], TODAY
        )
        assert [f.rule_id for f in flags] == [RuleId.NAME_MISMATCH]
        assert flags[0].severity is Severity.WARNING

    def test_different_person_is_critical(self) -> None:
        _, flags = run_validation_rules(
            make_applicant(), [make_id_extraction(full_name="Boris Petrov")], TODAY
        )
        assert [f.rule_id for f in flags] == [RuleId.NAME_MISMATCH]
        assert flags[0].severity is Severity.CRITICAL

    def test_fuzzy_ratio_bounds(self) -> None:
        assert fuzzy_ratio("Anna Virtanen", "anna virtanen") == 1.0
        assert fuzzy_ratio("Anna Virtanen", "Zzz Qqq") < 0.5


class TestDobAndExpiry:
    def test_dob_mismatch_is_critical(self) -> None:
        _, flags = run_validation_rules(
            make_applicant(), [make_id_extraction(date_of_birth="1990-01-01")], TODAY
        )
        assert RuleId.DOB_MISMATCH in [f.rule_id for f in flags]

    def test_expired_document_flagged(self) -> None:
        _, flags = run_validation_rules(
            make_applicant(), [make_id_extraction(expiry_date="2025-01-01")], TODAY
        )
        assert RuleId.DOC_EXPIRED in [f.rule_id for f in flags]


class TestAddress:
    def test_address_minor_variation_passes(self) -> None:
        extraction = ExtractionResult(
            document_id="doc-2",
            doc_type=DocumentType.PROOF_OF_ADDRESS,
            fields={
                "full_name": "Anna Virtanen",
                "address": "Kalevankatu 12 A 5, Helsinki 00100",
                "issue_date": "2026-05-10",
                "issuer": "Helen Oy",
            },
        )
        _, flags = run_validation_rules(make_applicant(), [extraction], TODAY)
        assert RuleId.ADDRESS_MISMATCH not in [f.rule_id for f in flags]

    def test_totally_different_address_is_warning(self) -> None:
        extraction = ExtractionResult(
            document_id="doc-2",
            doc_type=DocumentType.PROOF_OF_ADDRESS,
            fields={"full_name": "Anna Virtanen", "address": "Baker Street 221b, London"},
        )
        _, flags = run_validation_rules(make_applicant(), [extraction], TODAY)
        address_flags = [f for f in flags if f.rule_id == RuleId.ADDRESS_MISMATCH]
        assert len(address_flags) == 1
        assert address_flags[0].severity is Severity.WARNING


class TestBusinessFields:
    def test_reg_number_mismatch_is_critical(self) -> None:
        applicant = make_applicant(
            company_name="Meridian Trade OU",
            registration_number="EE-1447291",
        )
        extraction = ExtractionResult(
            document_id="doc-3",
            doc_type=DocumentType.BUSINESS_REGISTRATION,
            fields={
                "company_name": "Meridian Trade OU",
                "registration_number": "EE-9999999",
                "registration_date": "2019-02-01",
            },
        )
        _, flags = run_validation_rules(applicant, [extraction], TODAY)
        assert RuleId.REG_NUMBER_MISMATCH in [f.rule_id for f in flags]


class TestExtractionCompleteness:
    def test_missing_required_field_is_flagged(self) -> None:
        _, flags = run_validation_rules(
            make_applicant(), [make_id_extraction(document_number=None)], TODAY
        )
        incomplete = [f for f in flags if f.rule_id == RuleId.EXTRACTION_INCOMPLETE]
        assert len(incomplete) == 1
        assert "document_number" in incomplete[0].details

    def test_failed_extraction_is_skipped(self) -> None:
        broken = ExtractionResult(
            document_id="doc-x",
            doc_type=DocumentType.ID_DOCUMENT,
            extraction_error="unreadable document",
        )
        checks, flags = run_validation_rules(make_applicant(), [broken], TODAY)
        assert checks == []
        assert flags == []


class TestThresholdOverrides:
    def test_custom_thresholds_change_severity(self) -> None:
        # With a permissive critical threshold the typo becomes a warning-free pass.
        loose = MatchThresholds(name_critical=0.5, name_warning=0.8)
        _, flags = run_validation_rules(
            make_applicant(),
            [make_id_extraction(full_name="Anna Virtanem")],
            TODAY,
            thresholds=loose,
        )
        assert flags == []
