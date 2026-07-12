"""Unit tests for the deterministic fake step services (SPEC 9)."""

from kyc_agent.llm.fake import FakeEvaluator, FakeExtractor, FakeRouter
from kyc_agent.rules.ids import RuleId
from kyc_agent.schemas.case import CustomerType
from kyc_agent.schemas.documents import DocumentType, InputDocument

ID_CARD_TEXT = """REPUBLIC OF FINLAND
IDENTITY CARD
Full name: Anna Virtanen
Date of birth: 14.03.1991
Document number: FIN-8842517
Date of expiry: 01.05.2030
Nationality: FI
"""

INVOICE_TEXT = """HELEN OY
ELECTRICITY INVOICE
Customer: Anna Virtanen
Service address: Kalevankatu 12 A 5, 00100 Helsinki
Invoice date: 10.05.2026
Issuer: Helen Oy
"""

REGISTRATION_TEXT = """REPUBLIC OF ESTONIA — COMMERCIAL REGISTER
CERTIFICATE OF REGISTRATION
Company name: Meridian Trade OU
Registration number: EE-1447291
Registration date: 01.02.2019
Legal form: Osauhing (private limited company)
Registered office: Tartu mnt 25, 10117 Tallinn
"""

UBO_TEXT = """UBO DECLARATION
Company name: Meridian Trade OU
Beneficial owners:
- Karl Tamm; born 12.07.1978; ownership 60%
- Liis Kukk; born 25.09.1985; ownership 40%
"""

GARBAGE_TEXT = "@@#%%!!~~ scan_error_0x00 \x00\x01 ]]]===[[[ 9f8a7b6c partial bytes lost"


def doc(text: str, doc_id: str = "d1") -> InputDocument:
    return InputDocument(document_id=doc_id, file_name=f"{doc_id}.pdf", text_content=text)


class TestFakeRouter:
    async def test_classifies_all_types(self) -> None:
        docs = [
            doc(ID_CARD_TEXT, "a"),
            doc(INVOICE_TEXT, "b"),
            doc(REGISTRATION_TEXT, "c"),
            doc(UBO_TEXT, "d"),
        ]
        result = await FakeRouter().classify(docs, CustomerType.INDIVIDUAL)
        assert [c.doc_type for c in result] == [
            DocumentType.ID_DOCUMENT,
            DocumentType.PROOF_OF_ADDRESS,
            DocumentType.BUSINESS_REGISTRATION,
            DocumentType.UBO_DECLARATION,
        ]
        assert all(c.classifier_confidence > 0.9 for c in result)

    async def test_garbage_is_unknown_with_low_confidence(self) -> None:
        result = await FakeRouter().classify([doc(GARBAGE_TEXT)], CustomerType.INDIVIDUAL)
        assert result[0].doc_type is DocumentType.UNKNOWN
        assert result[0].classifier_confidence < 0.5


class TestFakeExtractor:
    async def test_extracts_id_fields_with_iso_dates(self) -> None:
        result = await FakeExtractor().extract(doc(ID_CARD_TEXT), DocumentType.ID_DOCUMENT)
        assert result.extraction_error is None
        assert result.fields == {
            "full_name": "Anna Virtanen",
            "date_of_birth": "1991-03-14",
            "document_number": "FIN-8842517",
            "expiry_date": "2030-05-01",
            "nationality": "FI",
        }
        assert all(c >= 0.9 for c in result.field_confidence.values())

    async def test_extracts_proof_of_address(self) -> None:
        result = await FakeExtractor().extract(doc(INVOICE_TEXT), DocumentType.PROOF_OF_ADDRESS)
        assert result.fields["full_name"] == "Anna Virtanen"
        assert result.fields["address"] == "Kalevankatu 12 A 5, 00100 Helsinki"
        assert result.fields["issue_date"] == "2026-05-10"

    async def test_extracts_ubo_owners(self) -> None:
        result = await FakeExtractor().extract(doc(UBO_TEXT), DocumentType.UBO_DECLARATION)
        owners = result.fields["beneficial_owners"]
        assert [o["full_name"] for o in owners] == ["Karl Tamm", "Liis Kukk"]
        assert owners[0]["date_of_birth"] == "1978-07-12"
        assert owners[1]["ownership_percent"] == 40.0

    async def test_garbage_yields_extraction_error(self) -> None:
        result = await FakeExtractor().extract(doc(GARBAGE_TEXT), DocumentType.ID_DOCUMENT)
        assert result.extraction_error is not None
        assert result.fields == {}

    async def test_glitch_marker_injects_hallucination(self) -> None:
        text = (
            ID_CARD_TEXT.replace("Full name: Anna Virtanen", "Full name: Anja Wirtanen")
            + "\n[OCR-GLITCH:full_name=Anna Virtanen]"
        )
        result = await FakeExtractor().extract(doc(text), DocumentType.ID_DOCUMENT)
        assert result.fields["full_name"] == "Anna Virtanen"  # not what the doc says


class TestFakeEvaluator:
    async def test_grounded_extraction_has_no_flags(self) -> None:
        document = doc(ID_CARD_TEXT)
        extraction = await FakeExtractor().extract(document, DocumentType.ID_DOCUMENT)
        assert await FakeEvaluator().verify_grounding(document, extraction) == []

    async def test_hallucinated_value_is_flagged(self) -> None:
        text = (
            ID_CARD_TEXT.replace("Full name: Anna Virtanen", "Full name: Anja Wirtanen")
            + "\n[OCR-GLITCH:full_name=Anna Virtanen]"
        )
        document = doc(text)
        extraction = await FakeExtractor().extract(document, DocumentType.ID_DOCUMENT)
        flags = await FakeEvaluator().verify_grounding(document, extraction)
        assert len(flags) == 1
        assert flags[0].rule_id == RuleId.EVALUATOR_DISCREPANCY
        assert "full_name" in flags[0].details

    async def test_ungrounded_beneficial_owner_is_flagged(self) -> None:
        document = doc(UBO_TEXT)
        extraction = await FakeExtractor().extract(document, DocumentType.UBO_DECLARATION)
        extraction.fields["beneficial_owners"][0]["full_name"] = "Ghost Owner"
        flags = await FakeEvaluator().verify_grounding(document, extraction)
        assert any("Ghost Owner" in f.details for f in flags)
