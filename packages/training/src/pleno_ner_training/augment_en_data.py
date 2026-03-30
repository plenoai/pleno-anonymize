"""EN training data augmentation.

Addresses quality gaps in generated data:
- DATE_OF_BIRTH: adds spelled-out month formats and context prefixes
- BANK_ACCOUNT: filters out bare-number entries, adds structured bank info
- ORGANIZATION: adds abbreviations (FDA, SEC) and standalone company names
- PERSON: adds short surnames (Li, Wu, Ng) and diverse formats
- Negative examples: general dates that should NOT be DATE_OF_BIRTH
"""

import json
import random
import copy
from pathlib import Path

random.seed(42)

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTH_ABBREV = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

DOB_PREFIXES = [
    "Date of Birth: ",
    "DOB: ",
    "D.O.B.: ",
    "Born: ",
    "Birthday: ",
    "Date of birth: ",
]

BANKS = [
    ("Chase Bank", "021000021"),
    ("Bank of America", "026009593"),
    ("Wells Fargo", "121000248"),
    ("Citibank", "021000089"),
    ("US Bank", "091000019"),
    ("PNC Bank", "043000096"),
    ("Capital One", "051000017"),
    ("TD Bank", "031101266"),
]

ACCOUNT_TYPES = ["Checking", "Savings"]

BANK_FORMATS = [
    "{bank}, Routing: {routing}, Account: {acct}, {type}",
    "{bank}, ABA: {routing}, Acct: {acct}, {type}",
    "{bank}, Routing No. {routing}, Account No. {acct}",
    "{bank}, RT# {routing}, Account# {acct}",
    "{bank} {type} Account {acct}, Routing {routing}",
]


def _random_date() -> tuple[int, int, int]:
    year = random.randint(1950, 2005)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return year, month, day


def _format_dob_varied(year: int, month: int, day: int) -> str:
    """Generate a varied DOB format string."""
    fmt = random.choice([
        "month_name_full",
        "month_name_abbrev",
        "mm_dd_yyyy",
        "dd_mon_yyyy",
        "iso",
        "mm_dd_yyyy_slash",
    ])
    if fmt == "month_name_full":
        return f"{MONTH_NAMES[month-1]} {day}, {year}"
    elif fmt == "month_name_abbrev":
        return f"{day} {MONTH_ABBREV[month-1]} {year}"
    elif fmt == "mm_dd_yyyy":
        return f"{month:02d}/{day:02d}/{year}"
    elif fmt == "dd_mon_yyyy":
        return f"{day:02d}-{MONTH_ABBREV[month-1]}-{year}"
    elif fmt == "iso":
        return f"{year}-{month:02d}-{day:02d}"
    else:
        return f"{month:02d}-{day:02d}-{year}"


def _random_acct() -> str:
    return str(random.randint(100000000, 999999999))


def _format_bank_varied() -> str:
    bank, routing = random.choice(BANKS)
    acct = _random_acct()
    acct_type = random.choice(ACCOUNT_TYPES)
    fmt = random.choice(BANK_FORMATS)
    return fmt.format(bank=bank, routing=routing, acct=acct, type=acct_type)


PERSON_NAMES = [
    "John Smith", "Emily R. Johnson", "Michael O'Brien", "Sarah Williams",
    "James Wilson Jr.", "Maria Gonzalez", "Robert Chen", "Jennifer Davis",
    "David Martinez", "Lisa Anderson", "Jean-Pierre Dupont", "Catherine Lee",
    "Thomas Brown", "Alexandra Kim", "William Taylor", "Rachel Moore",
]

SHORT_SURNAME_NAMES = [
    "Mr. Li", "Dr. Wu", "Ms. Ng", "Prof. Yu", "Mrs. Xu", "Dr. Ho",
    "Mr. Le", "Ms. Ma", "Dr. Hu", "Mrs. Ye", "Prof. Ko", "Mr. Su",
]

GOV_ORGS = [
    "FDA", "SEC", "IRS", "FBI", "CIA", "EPA", "NASA", "OSHA",
    "DOJ", "NIH", "FEMA", "DOD", "DOE", "HHS", "USDA",
]

WELL_KNOWN_COMPANIES = [
    "Apple", "Google", "Microsoft", "Amazon", "Tesla", "Meta",
    "Netflix", "Uber", "Spotify", "Airbnb", "Samsung", "Intel",
    "IBM", "Oracle", "Salesforce", "Adobe", "PayPal", "Stripe",
]

FULL_ORG_NAMES = [
    "Acme Corporation", "Global Health Services", "First National Bank",
    "Metro Medical Center", "Pacific Insurance Group", "Atlantic Financial",
    "Summit Healthcare", "Pinnacle Consulting", "Harbor Technologies",
    "National Health Service", "Red Cross", "World Health Organization",
    "MIT", "Stanford University", "Johns Hopkins Hospital",
]

NON_DOB_DATE_TEXTS = [
    "The next meeting is scheduled for March 15, 2024",
    "Our quarterly report for Q2 2023 will be published on July 1",
    "The project deadline is December 31, 2024",
    "The conference will be held on September 20, 2025",
    "The contract expires on January 1, 2026",
    "Revenue for the fiscal year ending March 2024 increased by 15%",
    "The election is scheduled for November 5, 2024",
    "The product launch date is set for April 15, 2025",
    "We will review the budget on February 28, 2024",
    "The warranty period runs from June 1, 2023 to May 31, 2024",
]

ADDRESSES = [
    "123 Main Street, New York, NY 10001",
    "456 Oak Avenue, Suite 200, San Francisco, CA 94102",
    "789 Elm Drive, Apt 3B, Chicago, IL 60601",
    "321 Pine Road, Boston, MA 02101",
    "654 Maple Lane, Austin, TX 78701",
    "987 Cedar Court, Seattle, WA 98101",
]

ORGS = [
    "Acme Corporation", "Global Health Services", "First National Bank",
    "Metro Medical Center", "Pacific Insurance Group", "Atlantic Financial",
    "Summit Healthcare", "Pinnacle Consulting", "Harbor Technologies",
]

DOB_TEMPLATES = [
    "Patient Name: {person}\n{dob_prefix}{dob}\nAddress: {address}\nEmployer: {org}",
    "Account Holder: {person}\nDate of Birth: {dob}\nMailing Address: {address}",
    "Employee: {person}\nDOB: {dob}\nHome Address: {address}\nCompany: {org}",
    "Name: {person}\nBorn: {dob}\nResidence: {address}",
    "Applicant: {person}\nD.O.B.: {dob}\nAddress: {address}\nOrganization: {org}",
    "Customer: {person}\nBirthday: {dob}\nShipping Address: {address}",
    "Insured: {person}\nDate of birth: {dob}\nAddress: {address}\nProvider: {org}",
]

BANK_TEMPLATES = [
    "Account Holder: {person}\nBank Details: {bank}\nAddress: {address}",
    "Wire Transfer Authorization\nBeneficiary: {person}\nWire to: {bank}\nAddress: {address}",
    "Direct Deposit Setup\nEmployee: {person}\nBank Account: {bank}\nCompany: {org}",
    "Refund Details\nCustomer: {person}\nRefund to: {bank}\nOrganization: {org}",
    "Payment Information\nPayee: {person}\nPayment Account: {bank}\nAddress: {address}",
]

COMBINED_TEMPLATES = [
    "Patient Name: {person}\nDate of Birth: {dob}\nAddress: {address}\nInsurance: {org}\nPayment: {bank}",
    "Employee: {person}\nDOB: {dob}\nHome Address: {address}\nEmployer: {org}\nDirect Deposit: {bank}",
    "Account Holder: {person}\nBorn: {dob}\nMailing Address: {address}\nBank Details: {bank}",
    "Applicant Information\nName: {person}\nD.O.B.: {dob}\nAddress: {address}\nOrganization: {org}\nBank Account: {bank}",
]

ORG_TEMPLATES = [
    "According to {org}, the new regulation takes effect immediately",
    "The report was filed with {org} last quarter",
    "{org} announced updated guidelines for compliance",
    "Representatives from {org} attended the summit",
    "The investigation by {org} revealed several violations",
    "{org} issued a statement regarding the incident",
    "As mandated by {org}, all firms must comply by year-end",
    "The grant was awarded by {org} to support research",
]

SHORT_NAME_TEMPLATES = [
    "Patient {person} was admitted to {org}",
    "{person} submitted the quarterly report to {org}",
    "The appointment for {person} is confirmed at {org}",
    "Dr. assigned {person} to the new project at {org}",
    "{person} signed the agreement with {org}",
    "The referral from {person} was received by {org}",
]


def _build_doc(template: str, **kwargs) -> dict | None:
    """Build a document with proper entity offsets from a template."""
    entities = []
    text_parts = []
    remaining = template

    # Simple tag-based approach: replace {key} with value and track offsets
    import re

    tag_map = {
        "person": ("PERSON", kwargs.get("person", "")),
        "dob": ("DATE_OF_BIRTH", kwargs.get("dob", "")),
        "address": ("ADDRESS", kwargs.get("address", "")),
        "org": ("ORGANIZATION", kwargs.get("org", "")),
        "bank": ("BANK_ACCOUNT", kwargs.get("bank", "")),
    }

    # First pass: handle dob_prefix (not an entity)
    if "dob_prefix" in kwargs:
        remaining = remaining.replace("{dob_prefix}", kwargs["dob_prefix"])

    # Build text with entity tracking
    result = ""
    for tag_name, (label, value) in tag_map.items():
        placeholder = "{" + tag_name + "}"
        if placeholder not in remaining:
            continue
        if not value:
            continue
        parts = remaining.split(placeholder, 1)
        result += parts[0]
        start = len(result)
        result += value
        end = len(result)
        entities.append({
            "start": start,
            "end": end,
            "label": label,
            "text": value,
        })
        remaining = parts[1] if len(parts) > 1 else ""

    result += remaining

    if not entities:
        return None

    return {"text": result, "entities": entities}


def generate_augmented_docs(count: int = 500) -> list[dict]:
    """Generate augmented documents focusing on weak entity types."""
    docs = []

    # DOB-focused documents (20%)
    for _ in range(int(count * 0.20)):
        year, month, day = _random_date()
        dob = _format_dob_varied(year, month, day)
        prefix = random.choice(DOB_PREFIXES) if random.random() < 0.3 else ""
        template = random.choice(DOB_TEMPLATES)
        doc = _build_doc(
            template,
            person=random.choice(PERSON_NAMES),
            dob=dob,
            dob_prefix=prefix,
            address=random.choice(ADDRESSES),
            org=random.choice(ORGS),
        )
        if doc:
            docs.append(doc)

    # BANK_ACCOUNT-focused documents (15%)
    for _ in range(int(count * 0.15)):
        bank = _format_bank_varied()
        template = random.choice(BANK_TEMPLATES)
        doc = _build_doc(
            template,
            person=random.choice(PERSON_NAMES),
            bank=bank,
            address=random.choice(ADDRESSES),
            org=random.choice(ORGS),
        )
        if doc:
            docs.append(doc)

    # Combined DOB + BANK documents (15%)
    for _ in range(int(count * 0.15)):
        year, month, day = _random_date()
        dob = _format_dob_varied(year, month, day)
        bank = _format_bank_varied()
        template = random.choice(COMBINED_TEMPLATES)
        doc = _build_doc(
            template,
            person=random.choice(PERSON_NAMES),
            dob=dob,
            address=random.choice(ADDRESSES),
            org=random.choice(ORGS),
            bank=bank,
        )
        if doc:
            docs.append(doc)

    # ORGANIZATION-focused: abbreviations and standalone names (20%)
    for _ in range(int(count * 0.20)):
        org_name = random.choice(GOV_ORGS + WELL_KNOWN_COMPANIES + FULL_ORG_NAMES)
        template = random.choice(ORG_TEMPLATES)
        doc = _build_doc(template, org=org_name)
        if doc:
            docs.append(doc)

    # SHORT SURNAME focused (10%)
    for _ in range(int(count * 0.10)):
        name = random.choice(SHORT_SURNAME_NAMES)
        org = random.choice(FULL_ORG_NAMES + GOV_ORGS + WELL_KNOWN_COMPANIES)
        template = random.choice(SHORT_NAME_TEMPLATES)
        doc = _build_doc(template, person=name, org=org)
        if doc:
            docs.append(doc)

    # NEGATIVE examples: dates that are NOT DOB (20%)
    for text in NON_DOB_DATE_TEXTS * (int(count * 0.20) // len(NON_DOB_DATE_TEXTS) + 1):
        docs.append({"text": text, "entities": []})
        if len([d for d in docs if not d["entities"]]) >= int(count * 0.20):
            break

    return docs


def clean_bank_accounts(data: list[dict]) -> list[dict]:
    """Remove documents where BANK_ACCOUNT is just a bare number (no bank name)."""
    cleaned = []
    removed = 0
    for doc in data:
        new_entities = []
        for ent in doc["entities"]:
            if ent["label"] == "BANK_ACCOUNT":
                text = ent["text"].strip()
                # Keep only if it contains alphabetic chars (bank name, routing label, etc.)
                if any(c.isalpha() for c in text):
                    new_entities.append(ent)
                else:
                    removed += 1
            else:
                new_entities.append(ent)
        if new_entities:
            doc_copy = copy.deepcopy(doc)
            doc_copy["entities"] = new_entities
            cleaned.append(doc_copy)
        elif doc["entities"]:
            # All entities were bare bank numbers - skip doc
            pass
        else:
            cleaned.append(doc)

    print(f"Removed {removed} bare-number BANK_ACCOUNT entities")
    return cleaned


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="EN data augmentation")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parents[2] / "data" / "raw" / "en" / "generated.json",
    )
    parser.add_argument("--augment-count", type=int, default=500)
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()

    print(f"Loading {args.input}...")
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    print(f"Original: {len(data)} documents")

    if not args.no_clean:
        data = clean_bank_accounts(data)
        print(f"After cleaning: {len(data)} documents")

    augmented = generate_augmented_docs(args.augment_count)
    print(f"Generated {len(augmented)} augmented documents")

    data.extend(augmented)
    print(f"Total: {len(data)} documents")

    # Stats
    from collections import Counter
    labels = Counter()
    for doc in data:
        for ent in doc["entities"]:
            labels[ent["label"]] += 1
    print("\nEntity counts:")
    for label, count in sorted(labels.items()):
        print(f"  {label}: {count}")

    output = args.input
    with open(output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to {output}")


if __name__ == "__main__":
    main()
