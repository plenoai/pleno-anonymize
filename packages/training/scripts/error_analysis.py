"""Analyze ORG errors on benchmark data."""
import spacy
from spacy.tokens import DocBin
from collections import Counter

nlp = spacy.load("output/ja-v02/model-best")
db = DocBin().from_disk("data/benchmark/v0.4.0/ja/test.spacy")
docs_gold = list(db.get_docs(nlp.vocab))

fn_orgs = []
fp_orgs = []
tp_orgs = []

for gold_doc in docs_gold:
    text = gold_doc.text
    pred_doc = nlp(text)

    gold_orgs = {(e.start_char, e.end_char, e.text) for e in gold_doc.ents if e.label_ == "ORGANIZATION"}
    pred_orgs = {(e.start_char, e.end_char, e.text) for e in pred_doc.ents if e.label_ == "ORGANIZATION"}

    for g in gold_orgs:
        if any(abs(g[0]-p[0]) <= 2 and abs(g[1]-p[1]) <= 2 for p in pred_orgs):
            tp_orgs.append(g[2])
        else:
            fn_orgs.append((g[2], text[:120]))

    for p in pred_orgs:
        if not any(abs(g[0]-p[0]) <= 2 and abs(g[1]-p[1]) <= 2 for g in gold_orgs):
            fp_orgs.append((p[2], text[:120]))

print(f"TP: {len(tp_orgs)}, FN: {len(fn_orgs)}, FP: {len(fp_orgs)}")
print(f"Recall: {len(tp_orgs)/(len(tp_orgs)+len(fn_orgs)):.3f}")
print(f"Precision: {len(tp_orgs)/(len(tp_orgs)+len(fp_orgs)):.3f}")

print(f"\n=== Top 30 FN (missed ORGs) ===")
fn_texts = Counter([f[0] for f in fn_orgs])
for text, count in fn_texts.most_common(30):
    print(f"  {count}x: {text}")

print(f"\n=== Sample FN contexts (first 15) ===")
for org_text, ctx in fn_orgs[:15]:
    print(f"  MISSED: [{org_text}] in: {ctx}...")

print(f"\n=== Top 20 FP (false ORGs) ===")
fp_texts = Counter([f[0] for f in fp_orgs])
for text, count in fp_texts.most_common(20):
    print(f"  {count}x: {text}")
