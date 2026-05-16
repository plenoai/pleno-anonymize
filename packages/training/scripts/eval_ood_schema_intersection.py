"""OOD eval restricted to gold labels that v2's 28-label vocab can semantically reach.

Excludes HEALTH_INSURANCE and ORGANIZATION from gold (no analogue in v2's
output vocabulary). Eval is still label-agnostic IoU >= 0.5.
"""
from __future__ import annotations
import json, time, sys
from pathlib import Path
import numpy as np

DATA = Path("/tmp/v2-ood-extended.jsonl")
MODEL = "packages/training/output/ja-ner-supervised-v2/model-best"
# Labels in v1 (pleno) test set that have NO semantic analogue in v2's schema
EXCLUDED = {"HEALTH_INSURANCE", "ORGANIZATION", "MY_NUMBER", "MY_NUMBER_CORPORATE", "RESIDENCE_CARD", "URL", "IP_ADDRESS"}

def iou(a,b):
    s=max(a[0],b[0]); e=min(a[1],b[1])
    inter=max(0,e-s); union=(a[1]-a[0])+(b[1]-b[0])-inter
    return inter/union if union else 0.0

def decode(offs, labs):
    spans=[]; cur_l=cur_s=cur_e=None
    for off,p in zip(offs,labs):
        if tuple(off)==(0,0): continue
        if p=="O":
            if cur_l is not None: spans.append((cur_s,cur_e,cur_l)); cur_l=cur_s=cur_e=None
            continue
        bio,_,lab=p.partition("-")
        if bio=="B" or cur_l!=lab:
            if cur_l is not None: spans.append((cur_s,cur_e,cur_l))
            cur_l=lab; cur_s=off[0]; cur_e=off[1]
        else: cur_e=off[1]
    if cur_l is not None: spans.append((cur_s,cur_e,cur_l))
    return spans

import torch
from transformers import AutoModelForTokenClassification, AutoTokenizer
tok=AutoTokenizer.from_pretrained(MODEL); mdl=AutoModelForTokenClassification.from_pretrained(MODEL).eval()
id2lab=mdl.config.id2label

per_doc=[]; per_label_tp={}; per_label_fn={}
for line in DATA.read_text(encoding="utf-8").splitlines():
    if not line.strip(): continue
    r=json.loads(line)
    text=r["text"]
    gold=[(int(e["start"]),int(e["end"]),str(e["label"])) for e in r["entities"] if str(e["label"]) not in EXCLUDED]
    enc=tok(text, return_offsets_mapping=True, truncation=True, max_length=512, return_tensors="pt")
    offs=enc.pop("offset_mapping")[0].tolist()
    with torch.inference_mode():
        logits=mdl(**{k:v.to(mdl.device) for k,v in enc.items()}).logits[0]
    labs=[id2lab[i] for i in logits.argmax(-1).tolist()]
    pred=decode(offs, labs)
    matched=set(); tp=fn=0
    for g_s,g_e,g_l in gold:
        per_label_tp.setdefault(g_l,0); per_label_fn.setdefault(g_l,0)
        best_i,best_iou=-1,0.0
        for i,p in enumerate(pred):
            if i in matched: continue
            v=iou((g_s,g_e),(p[0],p[1]))
            if v>best_iou: best_iou=v; best_i=i
        if best_i>=0 and best_iou>=0.5:
            tp+=1; per_label_tp[g_l]+=1; matched.add(best_i)
        else:
            fn+=1; per_label_fn[g_l]+=1
    fp=len(pred)-len(matched)
    per_doc.append((tp,fp,fn))

arr=np.array(per_doc); n=len(arr)
s=arr.sum(axis=0); tp,fp,fn=s
P=tp/(tp+fp) if tp+fp else 0; R=tp/(tp+fn) if tp+fn else 0; F=2*P*R/(P+R) if P+R else 0

rng=np.random.default_rng(42); boot=[]
for _ in range(1000):
    idx=rng.integers(0,n,size=n)
    ss=arr[idx].sum(axis=0); t,f,fn2=ss
    p=t/(t+f) if t+f else 0; r=t/(t+fn2) if t+fn2 else 0
    boot.append(2*p*r/(p+r) if p+r else 0)
boot=np.array(boot)
print(json.dumps({
    "n_docs": int(n), "n_gold": int(tp+fn), "excluded_labels": sorted(EXCLUDED),
    "point": {"P": round(P,4), "R": round(R,4), "F1": round(F,4)},
    "f1_ci_95": [round(float(np.percentile(boot,2.5)),4), round(float(np.percentile(boot,97.5)),4)],
    "per_label_recall": {l: round(per_label_tp[l]/max(per_label_tp[l]+per_label_fn[l],1),4) for l in per_label_tp},
}, ensure_ascii=False, indent=2))
