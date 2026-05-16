"""OOD eval with adjacent-span merging — fairer comparison when gold uses coarser labels."""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import torch
from transformers import AutoModelForTokenClassification, AutoTokenizer

DATA = Path("/tmp/v2-ood-extended.jsonl")
MODEL = "packages/training/output/ja-ner-supervised-v2/model-best"

def iou(a,b):
    s=max(a[0],b[0]); e=min(a[1],b[1])
    inter=max(0,e-s); union=(a[1]-a[0])+(b[1]-b[0])-inter
    return inter/union if union else 0.0

tok=AutoTokenizer.from_pretrained(MODEL); mdl=AutoModelForTokenClassification.from_pretrained(MODEL).eval()
id2l=mdl.config.id2label

def predict_merged(text, gap_chars=2):
    enc=tok(text, return_offsets_mapping=True, truncation=True, max_length=512, return_tensors='pt')
    offs=enc.pop('offset_mapping')[0].tolist()
    with torch.inference_mode():
        logits=mdl(**{k:v.to(mdl.device) for k,v in enc.items()}).logits[0]
    labs=[id2l[i] for i in logits.argmax(-1).tolist()]
    spans=[]; cur=None
    for o,p in zip(offs,labs):
        if tuple(o)==(0,0): continue
        if p=='O':
            if cur: spans.append(cur); cur=None
            continue
        if cur is None: cur=[o[0], o[1]]
        else: cur[1]=o[1]  # always extend regardless of label
    if cur: spans.append(cur)
    # Optionally merge spans separated by <= gap_chars whitespace
    merged=[]
    for s in spans:
        if merged and s[0]-merged[-1][1] <= gap_chars:
            merged[-1][1]=s[1]
        else:
            merged.append(list(s))
    return [(s[0], s[1], "X") for s in merged]

per_doc=[]
for line in DATA.read_text(encoding='utf-8').splitlines():
    if not line.strip(): continue
    r=json.loads(line)
    gold=[(int(e['start']),int(e['end']),str(e['label'])) for e in r['entities']]
    pred=predict_merged(r['text'])
    matched=set(); tp=fn=0
    for g_s,g_e,_ in gold:
        best_i,best_iou=-1,0.0
        for i,p in enumerate(pred):
            if i in matched: continue
            v=iou((g_s,g_e),(p[0],p[1]))
            if v>best_iou: best_iou=v; best_i=i
        if best_i>=0 and best_iou>=0.5:
            tp+=1; matched.add(best_i)
        else:
            fn+=1
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
    "n_docs": int(n),
    "point": {"P": round(P,4), "R": round(R,4), "F1": round(F,4)},
    "f1_ci_95": [round(float(np.percentile(boot,2.5)),4), round(float(np.percentile(boot,97.5)),4)],
}, ensure_ascii=False, indent=2))
