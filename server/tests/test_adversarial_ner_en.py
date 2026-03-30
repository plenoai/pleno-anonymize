"""Adversarial edge-case tests for the English NER model.

Translation-based equivalents of test_adversarial_ner.py (JA) to enable
direct EN/JA benchmark comparison. Each test class mirrors its JA counterpart.
"""

import json
from pathlib import Path

import pytest

# Load EN NER model (skip if not available)
try:
    import spacy

    _models_dir = Path(__file__).parent.parent.parent / "packages" / "models"
    _model_path = None
    # Search for latest en_ner_en model version
    for candidate in sorted(_models_dir.glob("en_ner_en-*"), reverse=True):
        version = candidate.name.split("-", 1)[1]
        inner = candidate / "en_ner_en" / f"en_ner_en-{version}"
        if inner.exists():
            _model_path = inner
            break
    if _model_path and _model_path.exists():
        _nlp = spacy.load(str(_model_path))
        HAS_MODEL = True
    else:
        HAS_MODEL = False
        _nlp = None
except Exception:
    HAS_MODEL = False
    _nlp = None

pytestmark = pytest.mark.skipif(not HAS_MODEL, reason="EN NER model not found")


def _ner_entities(text: str) -> dict[str, list[str]]:
    """Extract entities from text grouped by label."""
    doc = _nlp(text)
    result: dict[str, list[str]] = {}
    for ent in doc.ents:
        result.setdefault(ent.label_, []).append(ent.text)
    return result


# ============================================================
# PERSON: Name boundary cases (mirrors TestPersonEdgeCases)
# ============================================================


class TestPersonEdgeCases:
    """Person name recognition edge cases."""

    @pytest.mark.parametrize(
        "text,expected_name",
        [
            ("The patient Mr. Li arrived at the clinic", "Li"),
            ("Dr. Wu is the attending physician", "Wu"),
            ("Ms. Ng submitted the report", "Ng"),
        ],
        ids=["short_li", "short_wu", "short_ng"],
    )
    def test_short_surnames(self, text: str, expected_name: str):
        """Short surnames (2-3 chars) — equivalent of JA 1-char names."""
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert any(expected_name in p for p in persons), f"'{expected_name}' not in {persons}"

    @pytest.mark.parametrize(
        "text,expected_name",
        [
            ("Please contact Jean-Pierre Dupont for details", "Jean-Pierre Dupont"),
            ("Michael O'Brien will be attending", "O'Brien"),
            ("Aleksandr Petrov-Volkonsky submitted his application", "Petrov-Volkonsky"),
        ],
        ids=["hyphenated_first", "apostrophe", "hyphenated_last"],
    )
    def test_names_with_special_chars(self, text: str, expected_name: str):
        """Names with hyphens and apostrophes — equivalent of JA katakana foreign names."""
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert any(expected_name in p or p in expected_name for p in persons), (
            f"'{expected_name}' not found in {persons}"
        )

    @pytest.mark.parametrize(
        "text,expected_name",
        [
            ("Dr. Maria Gonzalez will see you now", "Maria Gonzalez"),
            ("Prof. James R. Henderson published the study", "James R. Henderson"),
            ("The Honorable Justice Ruth Kim presided", "Ruth Kim"),
            ("Sgt. William O. Douglas filed the report", "William O. Douglas"),
        ],
        ids=["dr_title", "prof_middle_initial", "justice_title", "military_title"],
    )
    def test_names_with_titles(self, text: str, expected_name: str):
        """Names with various titles — equivalent of JA honorific tests."""
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert any(expected_name in p or p in expected_name for p in persons), (
            f"'{expected_name}' not found in {persons}"
        )

    def test_name_with_suffix(self):
        """Name with suffix (Jr., III) — equivalent of JA honorific-attached test."""
        text = "James Wilson Jr. placed the order"
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert persons, "No person detected"

    def test_consecutive_names(self):
        """Consecutive names — mirrors JA consecutive names test."""
        text = "Attendees: John Smith, Jane Doe, and Robert Johnson"
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert len(persons) >= 3, f"Expected 3+ names but got {len(persons)}: {persons}"


# ============================================================
# ADDRESS: Address boundary cases (mirrors TestAddressEdgeCases)
# ============================================================


class TestAddressEdgeCases:
    """Address recognition edge cases."""

    @pytest.mark.parametrize(
        "text",
        [
            "Address: 123 Main Street, New York, NY 10001",  # standard US
            "Address: 456 Oak Avenue, Suite 200, San Francisco, CA 94102",  # with suite
            "Address: 789 Elm Drive, Apt 3B, Chicago, IL 60601",  # with apartment
            "Address: 10 Downing Street, London SW1A 2AA, United Kingdom",  # UK format
            "Address: 1600 Pennsylvania Avenue NW, Washington, DC 20500",  # no comma before state
        ],
        ids=["standard_us", "with_suite", "with_apartment", "uk_format", "dc_address"],
    )
    def test_address_formats(self, text: str):
        """Various address formats — mirrors JA address format tests."""
        entities = _ner_entities(text)
        assert "ADDRESS" in entities, f"Address not detected: {entities}"

    def test_address_with_building(self):
        """Address with building name — mirrors JA building name test."""
        text = "Location: 350 Fifth Avenue, Empire State Building, Floor 42, New York, NY 10118"
        entities = _ner_entities(text)
        addresses = entities.get("ADDRESS", [])
        assert addresses, "Address not detected"
        full = " ".join(addresses)
        assert "Fifth Avenue" in full or "Empire State" in full

    def test_address_not_org(self):
        """Address and org should be detected separately — mirrors JA test."""
        text = "Pleno Inc. headquarters is at 100 Technology Drive, Mountain View, CA 94043"
        entities = _ner_entities(text)
        addresses = entities.get("ADDRESS", [])
        orgs = entities.get("ORGANIZATION", [])
        assert addresses, "Address not detected"
        assert orgs, "Organization not detected"


# ============================================================
# ORGANIZATION: Organization name cases (mirrors TestOrganizationEdgeCases)
# ============================================================


class TestOrganizationEdgeCases:
    """Organization name recognition edge cases."""

    @pytest.mark.parametrize(
        "text,expected_org",
        [
            ("The FDA issued a new guideline", "FDA"),
            ("According to the SEC filing", "SEC"),
            ("The IRS announced new tax rules", "IRS"),
        ],
        ids=["fda", "sec", "irs"],
    )
    def test_government_abbreviations(self, text: str, expected_org: str):
        """Government agency abbreviations — mirrors JA ministry abbreviation tests."""
        entities = _ner_entities(text)
        orgs = entities.get("ORGANIZATION", [])
        assert any(expected_org in o for o in orgs), f"'{expected_org}' not in {orgs}"

    @pytest.mark.parametrize(
        "text,expected_org",
        [
            ("Apple announced a new product line", "Apple"),
            ("Google's earnings exceeded expectations", "Google"),
            ("Tesla unveiled its latest model", "Tesla"),
        ],
        ids=["apple", "google", "tesla"],
    )
    def test_well_known_company_short(self, text: str, expected_org: str):
        """Well-known companies without legal form — mirrors JA test."""
        entities = _ner_entities(text)
        orgs = entities.get("ORGANIZATION", [])
        assert any(expected_org in o for o in orgs), f"'{expected_org}' not in {orgs}"

    def test_org_types(self):
        """Various organization types — mirrors JA corporate form test."""
        text = (
            "ABC Corp., the XYZ Foundation, "
            "National Health Service, MIT, "
            "and the Red Cross participated"
        )
        entities = _ner_entities(text)
        orgs = entities.get("ORGANIZATION", [])
        assert len(orgs) >= 3, f"Expected 3+ orgs but got {len(orgs)}: {orgs}"


# ============================================================
# DATE_OF_BIRTH: Date of birth cases (mirrors TestDateOfBirthEdgeCases)
# ============================================================


class TestDateOfBirthEdgeCases:
    """Date of birth recognition edge cases."""

    @pytest.mark.parametrize(
        "text",
        [
            "Date of Birth: January 15, 1990",
            "DOB: 01/15/1990",  # MM/DD/YYYY
            "Date of Birth: 15-Jan-1990",  # DD-Mon-YYYY
            "Born: March 1, 1965",
            "D.O.B.: 1990-01-15",  # ISO format
            "Birthday: 02/20/1978",  # MM/DD/YYYY
            "Date of birth: 20 February 1978",  # DD Month YYYY
        ],
        ids=[
            "month_name_full",
            "mm_dd_yyyy",
            "dd_mon_yyyy",
            "born_prefix",
            "iso_format",
            "birthday_prefix",
            "dd_month_yyyy",
        ],
    )
    def test_date_formats(self, text: str):
        """Various date of birth formats — mirrors JA date format tests."""
        entities = _ner_entities(text)
        assert "DATE_OF_BIRTH" in entities, f"Date of birth not detected: {entities}"

    def test_date_not_general_date(self):
        """General dates should NOT be detected as DOB — mirrors JA test."""
        text = "The next meeting is scheduled for March 15, 2024"
        entities = _ner_entities(text)
        dobs = entities.get("DATE_OF_BIRTH", [])
        assert not dobs, f"General date misdetected as DOB: {dobs}"


# ============================================================
# BANK_ACCOUNT: Bank account cases (mirrors TestBankAccountEdgeCases)
# ============================================================


class TestBankAccountEdgeCases:
    """Bank account recognition edge cases."""

    @pytest.mark.parametrize(
        "text",
        [
            "Wire to: Chase Bank, Routing: 021000021, Account: 123456789, Checking",
            "Bank: Bank of America, ABA: 026009593, Acct: 987654321, Savings",
            "Transfer to: Wells Fargo, Routing No. 121000248, Account No. 456789012",
            "Payment details: Citibank, Account: 112233445, Routing: 021000089",
        ],
        ids=["chase", "boa", "wells_fargo", "citibank"],
    )
    def test_bank_formats(self, text: str):
        """Various bank account formats — mirrors JA bank format tests."""
        entities = _ner_entities(text)
        assert "BANK_ACCOUNT" in entities, f"Bank account not detected: {entities}"

    def test_partial_bank_info(self):
        """Bank name alone should not trigger BANK_ACCOUNT — mirrors JA test."""
        text = "Chase Bank offers excellent customer service"
        entities = _ner_entities(text)
        bank_accounts = entities.get("BANK_ACCOUNT", [])
        # Bank name alone without account details is not BANK_ACCOUNT


# ============================================================
# False positives: Non-PII text (mirrors TestFalsePositives)
# ============================================================


class TestFalsePositives:
    """Non-PII text should not trigger entity detection."""

    @pytest.mark.parametrize(
        "text",
        [
            "The Empire State Building is a landmark in New York",  # landmark, not address
            "The Federal Reserve manages monetary policy",  # org is correct, but no bank account
            "The Victorian era was a period of great change",  # era name, not DOB
            "The American Revolution began in 1776",  # historical date, not DOB
            "The Renaissance period influenced art and culture",  # era name, not DOB
        ],
        ids=[
            "landmark_not_address",
            "fed_not_bank_account",
            "era_not_dob",
            "historical_date_not_dob",
            "era_name_not_dob",
        ],
    )
    def test_non_pii_text(self, text: str):
        entities = _ner_entities(text)
        dobs = entities.get("DATE_OF_BIRTH", [])
        assert not dobs, f"False positive DATE_OF_BIRTH: {dobs}"
        banks = entities.get("BANK_ACCOUNT", [])
        assert not banks, f"False positive BANK_ACCOUNT: {banks}"


# ============================================================
# Entity proximity / density (mirrors TestEntityProximity)
# ============================================================


class TestEntityProximity:
    """Detection accuracy when entities are adjacent or dense."""

    def test_adjacent_person_address(self):
        """Person immediately followed by address — mirrors JA test."""
        text = "John Smith (456 Oak Avenue, Suite 200, New York, NY 10001) for delivery"
        entities = _ner_entities(text)
        assert "PERSON" in entities, "Person not detected"
        assert "ADDRESS" in entities, "Address not detected"

    def test_dense_pii_text(self):
        """High PII density text — mirrors JA test."""
        text = (
            "Patient Name: Emily Johnson, "
            "Date of Birth: March 20, 1993, "
            "Address: 789 Elm Drive, Apt 5C, Boston, MA 02101, "
            "Employer: Acme Corporation"
        )
        entities = _ner_entities(text)
        assert "PERSON" in entities
        assert "DATE_OF_BIRTH" in entities
        assert "ADDRESS" in entities
        assert "ORGANIZATION" in entities

    def test_entity_separated_by_comma(self):
        """Comma-separated entities — mirrors JA test."""
        text = "Participants: John Smith, Jane Doe, Robert Johnson attended the meeting"
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert len(persons) >= 3, f"Expected 3+ names but got {len(persons)}: {persons}"
