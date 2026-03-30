| Feature | Description |
| --- | --- |
| **Name** | `en_ner_en` |
| **Version** | `0.2.0` |
| **spaCy** | `>=3.8.13,<3.9.0` |
| **Default Pipeline** | `tok2vec`, `ner` |
| **Components** | `tok2vec`, `ner` |
| **Vectors** | 684830 keys, 20000 unique vectors (300 dimensions) |
| **Sources** | n/a |
| **License** | n/a |
| **Author** | [n/a]() |

### Label Scheme

<details>

<summary>View label scheme (5 labels for 1 components)</summary>

| Component | Labels |
| --- | --- |
| **`ner`** | `ADDRESS`, `BANK_ACCOUNT`, `DATE_OF_BIRTH`, `ORGANIZATION`, `PERSON` |

</details>

### Accuracy

| Type | Score |
| --- | --- |
| `ENTS_F` | 97.20 |
| `ENTS_P` | 96.06 |
| `ENTS_R` | 98.37 |
| `TOK2VEC_LOSS` | 86327.16 |
| `NER_LOSS` | 54367.02 |