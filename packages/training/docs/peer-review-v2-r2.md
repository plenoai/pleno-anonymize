# Peer Review (R2)

Verdict: Accept
Confidence: 4
Decision driver: All three R1 blocking concerns are resolved cleanly (label-aware merge with documented equivalence classes, 3-seed variance reported with std on every cell, real-text eval added on stockmark Wikipedia), and the doc now states an unfavourable real-text result against its own headline. The remaining gaps are explicitly enumerated and downgrade-to-Minor.

## Resolved blocking items

1. R1-C1 (label-blind merge): Fixed. `eval_ood_span_merged.py` lines 26-46 implement explicit equivalence classes; lines 86-100 enforce same-coarse-class merging only; the post-merge re-lookup bug (already-promoted coarse names) is handled at line 41. Cross-class adjacency is preserved as distinct spans. Methodologically defensible.

2. R1-C2 (no real text): Resolved as "real but off-domain". The stockmark Wikipedia eval is real Japanese text, and the doc reports v2 losing to spaCy by 0.10 F1 on the PII subset without spin. Hand-annotated PII-context data would be better, and the doc names this as the top open follow-up; the unfavourable Wikipedia result combined with explicit production-expectation calibration ("~0.47, not ~0.85") is sufficient honesty for a venue acceptance.

3. R1-C3 (single training run): Resolved. 3 seeds × 4 eval sets; std reported on every cell; std is small (0.002-0.014) and well below bootstrap CI widths. The seed=1337 OOD-merged drop to 0.833 is shown rather than hidden.

## Remaining open items (Minor, acceptable with current disclosure)

- No PII-context real-text eval (hand-annotated chat/form/email JP). Acknowledged as top follow-up; production-expectation calibration in the doc is appropriately conservative.
- Library versions not pinned in pyproject.toml.
- Only one classic baseline (spaCy); GiNZA natural second.
- AI4Privacy upstream split-protocol still partially opaque (mitigated by the 0.4 % char-level overlap probe).
- v1↔v2 epoch confound (2 vs 3 epochs).

## Answer to the explicit R2 question

The stockmark Wikipedia eval is sufficient as "real-text" for Accept, specifically because the doc reports an unfavourable result and calibrates production expectations to the real-text number rather than the synthetic OOD number. If the doc had concealed or spin-corrected the stockmark loss, it would not be sufficient. Hand-annotated PII-context samples remain the highest-priority follow-up but do not block this revision.
