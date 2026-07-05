"""License-clean EN NER training data via Faker.

Generates synthetic documents in the text styles common to real-world PII
exposure (structured forms, emails, forum threads, narratives, support
tickets) and labels spans at COMPONENT granularity: given name and surname
are separate PERSON spans, and each address part (street / city / state /
postcode / building / country / secondary) is its own ADDRESS span. This
matches how downstream evaluators score partial-name/address matches
(char-IoU per component), and how the JA pipeline already annotates.

No third-party dataset text is used — all strings come from Faker plus
hand-written templates, so the output and any model trained on it can ship
under Apache-2.0.
"""

import argparse
import json
import random

from faker import Faker

LOCALES = ["en_GB", "en_US", "en_IE", "en_AU"]


class DocBuilder:
    def __init__(self):
        self.parts: list[str] = []
        self.entities: list[dict] = []
        self.length = 0

    def text(self, s: str):
        self.parts.append(s)
        self.length += len(s)

    def ent(self, s: str, label: str):
        self.entities.append(
            {"start": self.length, "end": self.length + len(s), "label": label}
        )
        self.text(s)

    def build(self) -> dict:
        return {"text": "".join(self.parts), "entities": self.entities}


def person_name(f: Faker, b: DocBuilder, *, title_prob=0.2, middle_prob=0.15):
    if random.random() < title_prob:
        b.ent(f.prefix(), "PERSON")
        b.text(" ")
    b.ent(f.first_name(), "PERSON")
    b.text(" ")
    if random.random() < middle_prob:
        b.ent(f.first_name()[0] + ".", "PERSON")
        b.text(" ")
    b.ent(f.last_name(), "PERSON")


def address_block(f: Faker, b: DocBuilder, sep=", "):
    b.ent(str(f.building_number()), "ADDRESS")
    b.text(sep)
    b.ent(f.street_name(), "ADDRESS")
    b.text(sep)
    b.ent(f.city(), "ADDRESS")
    if random.random() < 0.6:
        b.text(sep)
        b.ent(f.postcode(), "ADDRESS")
    if random.random() < 0.4:
        b.text(sep)
        b.ent(f.current_country(), "ADDRESS")


def a_date(f: Faker) -> str:
    fmt = random.choice(
        ["%d/%m/%Y", "%Y-%m-%d", "%B %d, %Y", "%d %B %Y", "%m/%d/%y", "%B/%y", "%d.%m.%Y"]
    )
    return f.date_object().strftime(fmt)


def a_time(f: Faker) -> str:
    fmt = random.choice(["%H:%M", "%I:%M %p", "%H:%M:%S"])
    return f.time_object().strftime(fmt)


def tpl_form(f: Faker) -> dict:
    b = DocBuilder()
    heads = random.choice(
        [
            ("**Student Information:**", "Student"),
            ("Applicant Details:", "Applicant"),
            ("--- Participant Record ---", "Participant"),
            ("Customer Profile", "Customer"),
            ("Employee Onboarding Form", "Employee"),
        ]
    )
    b.text(heads[0] + "\n")
    n = random.randint(1, 3)
    for i in range(1, n + 1):
        if n > 1:
            b.text(f"\n{i}. {heads[1]} {i}:\n")
        b.text("   - First Name: ")
        if random.random() < 0.08:
            b.text("Not Applicable")
        else:
            b.ent(f.first_name(), "PERSON")
        b.text("\n   - Last Name: ")
        b.ent(f.last_name(), "PERSON")
        if random.random() < 0.5:
            b.text("\n   - Date of Birth: ")
            b.ent(a_date(f), "DATE_OF_BIRTH")
        b.text("\n   - Country: ")
        b.ent(f.current_country_code(), "ADDRESS")
        b.text("\n   - Building Number: ")
        b.ent(str(f.building_number()), "ADDRESS")
        b.text("\n   - Street: ")
        b.ent(f.street_name(), "ADDRESS")
        b.text("\n   - City: ")
        b.ent(f.city(), "ADDRESS")
        if random.random() < 0.6:
            b.text("\n   - State: ")
            b.ent(f.state_abbr() if hasattr(f, "state_abbr") else "ENG", "ADDRESS")
        b.text("\n   - Postcode: ")
        b.ent(f.postcode(), "ADDRESS")
        if random.random() < 0.3:
            b.text("\n   - Secondary Address: ")
            b.ent(f.secondary_address() if hasattr(f, "secondary_address") else "Flat 2", "ADDRESS")
        if random.random() < 0.3:
            b.text("\n   - Registered: ")
            b.ent(a_date(f), "DATE")
            b.text(" at ")
            b.ent(a_time(f), "DATE")
        b.text("\n")
    return b.build()


def tpl_email(f: Faker) -> dict:
    b = DocBuilder()
    b.text(f"Subject: {f.catch_phrase()}\n\nDear ")
    person_name(f, b, title_prob=0.5)
    b.text(",\n\n")
    b.text(random.choice([
        "I hope this message finds you well. ",
        "Thank you for your continued cooperation. ",
        "We are writing to confirm the details below. ",
    ]))
    b.text("Your appointment is scheduled for ")
    b.ent(a_date(f), "DATE")
    b.text(" at ")
    b.ent(a_time(f), "DATE")
    b.text(".\nThe meeting will take place at ")
    address_block(f, b)
    b.text(".\n\n")
    if random.random() < 0.5:
        b.text("Please confirm your date of birth (")
        b.ent(a_date(f), "DATE_OF_BIRTH")
        b.text(") when checking in.\n\n")
    b.text("Kind regards,\n")
    person_name(f, b, title_prob=0.3)
    b.text(f"\n{f.company()}\n")
    return b.build()


def tpl_narrative(f: Faker) -> dict:
    b = DocBuilder()
    b.text(random.choice([
        "In an online forum discussion about ",
        "During the community webinar on ",
        "At the review meeting concerning ",
    ]))
    b.text(f.bs() + ", ")
    person_name(f, b)
    b.text(" mentioned relocating to ")
    b.ent(str(f.building_number()), "ADDRESS")
    b.text(" ")
    b.ent(f.street_name(), "ADDRESS")
    b.text(" in ")
    b.ent(f.city(), "ADDRESS")
    b.text(" on ")
    b.ent(a_date(f), "DATE")
    b.text(". Born on ")
    b.ent(a_date(f), "DATE_OF_BIRTH")
    b.text(", they had lived in ")
    b.ent(f.current_country(), "ADDRESS")
    b.text(" until ")
    b.ent(a_date(f), "DATE")
    b.text(". ")
    if random.random() < 0.5:
        person_name(f, b)
        b.text(" replied at ")
        b.ent(a_time(f), "DATE")
        b.text(" agreeing with the summary. ")
    return b.build()


def tpl_ticket(f: Faker) -> dict:
    b = DocBuilder()
    b.text("Ticket #" + str(random.randint(1000, 99999)) + "\n")
    b.text("Reported: ")
    b.ent(a_date(f), "DATE")
    b.text(" ")
    b.ent(a_time(f), "DATE")
    b.text("\nCustomer: ")
    person_name(f, b)
    b.text("\nShipping address: ")
    address_block(f, b)
    b.text("\nIssue: " + f.sentence() + "\n")
    if random.random() < 0.4:
        b.text("Verified DOB ")
        b.ent(a_date(f), "DATE_OF_BIRTH")
        b.text(" over the phone.\n")
    b.text("Assigned to: ")
    person_name(f, b)
    b.text("\n")
    return b.build()


def tpl_negative(f: Faker) -> dict:
    """Documents with no labeled spans — keeps precision honest."""
    b = DocBuilder()
    b.text(random.choice([
        f"The quarterly report on {f.bs()} highlighted steady progress across all divisions. ",
        f"Server maintenance is planned for the weekend; services may be briefly unavailable. ",
        f"The committee approved the new curriculum after a long deliberation. ",
        f"{f.company()} announced a partnership focused on {f.bs()}. ",
        "Please refer to the attached guidelines for the submission process. ",
    ]))
    b.text(f.paragraph())
    return b.build()


TEMPLATES = [
    (tpl_form, 0.30),
    (tpl_email, 0.25),
    (tpl_narrative, 0.20),
    (tpl_ticket, 0.15),
    (tpl_negative, 0.10),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    random.seed(args.seed)
    Faker.seed(args.seed)
    fakers = [Faker(loc) for loc in LOCALES]

    fns = [t for t, _ in TEMPLATES]
    weights = [w for _, w in TEMPLATES]
    docs = []
    for _ in range(args.count):
        f = random.choice(fakers)
        tpl = random.choices(fns, weights=weights, k=1)[0]
        docs.append(tpl(f))

    n_ents = sum(len(d["entities"]) for d in docs)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(docs, fh, ensure_ascii=False)
    print(f"wrote {len(docs)} docs, {n_ents} entities to {args.output}")


if __name__ == "__main__":
    main()
