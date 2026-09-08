## PHASE 0 (Current)

- ✅ Regex-based term extraction from structured templates
- ✅ Levenshtein + Double-Metaphone fuzzy matching
- ✅ Radiologist approval for all variants
- ✅ Single PDF upload (templates only)
- ✅ S3/S4/S3-Pass2 loop with manual approval

---

## PHASE 1 (Post-Launch) — High Priority

### 1. Lightweight LLM for Unstructured Template Extraction

**Why:** Current regex handles structured PDFs (95% of templates), but fails on free-form, handwritten, or poorly formatted documents.

**Implementation Strategy:**
```
Current (Phase 0):
  Template PDF → Regex → 60% extraction on unstructured

Phase 1:
  Template PDF → Regex (fast path)
    ├─ confidence > 0.8? → Use regex result ✅
    └─ confidence < 0.8? → Send to LLM fallback
      ↓
    LLM (lightweight) → 90% extraction on unstructured
```

**Why NOT Claude?**
- Claude: $0.01–0.03 per document (expensive at scale)
- Overkill for simple term extraction
- 3–5 sec response time (poor UX)
- Rate limits on enterprise plans

**Why Lightweight LLM (Qwen, Gemini Nano)?**
- Qwen 2.5 7B: $0.0001 per call (100x cheaper than Claude!)
- Gemini Nano: Free on-device (Google Cloud)
- Latency: 200–500ms (acceptable UX)
- Accuracy: 88–92% (good enough + radiologist review)
- Can self-host (no vendor lock-in)

**Cost Comparison:**
```
Phase 0: 20 templates/month × $0.00 (regex) = $0.00

Phase 1A (Claude fallback):  20 × $0.01 = $0.20
Phase 1B (Qwen fallback):    20 × $0.0001 = $0.002  ✅ 100x cheaper!
Phase 1C (Gemini Nano):      20 × $0.00 = $0.00 (free)
```

**Accuracy Improvement:**
```
Regex only (Phase 0):     60–95% depending on format
Regex + Qwen (Phase 1):   92–98% ← 7% improvement on hard cases
Regex + Claude (Phase 1): 95–99% ← 4% improvement at 100x cost
```

**Recommendation:** Start with **Qwen 2.5 7B** (self-host) or **Gemini Nano** (Google Cloud).

**TODO:**
- [ ] Evaluate Qwen 2.5 7B vs Gemini Nano (cost/accuracy/latency)
- [ ] Implement regex + Qwen fallback for unstructured templates
- [ ] Set up Qwen self-hosting or Gemini Nano integration
- [ ] Test with 3 labs (Lab A, B, C)
- [ ] Add confidence thresholds (< 0.8 → LLM)
- [ ] Measure: accuracy before/after

---

### 2. Support Shorthand Reference PDF Upload

**Current Problem:** Shorthands often in separate reference documents for transcriptionists.

**Implementation:**
```
Phase 0 (Current):
  Upload: template.pdf only
  Result: S4 ASR biased on formal terms only

Phase 1 (Proposed):
  Upload: template.pdf + shorthand_reference.pdf
  Parser extracts:
    ├─ Template → "Left Lower Lobe", "Heart Size"
    └─ Reference → "LLL" → "Left Lower Lobe"
  Merge: keyterm_set = {formal names} + {shorthand → formal}
  Result: S4 ASR biased on BOTH formal + shorthand
```

**Impact on ASR Accuracy:**
```
Without shorthand:  Radiologist: "LLL shows PNA" → Whisper: "eel ell ell shows pee-en-ay" ❌ (40% error)
With shorthand:     Same audio → Whisper (biased): "LLL shows PNA" ✅ (5% error)
Improvement:        ~35% on shorthand-heavy dicts
```

**UI Changes:**
```
S3 Upload Form:
  ├─ [Upload Template PDFs] ✅ (required)
  └─ [Upload Shorthand Reference PDFs] (optional)
       └─ Parser auto-extracts shorthand mappings
```

**Shorthand Extraction Patterns:**
```
Pattern: "LLL = Left Lower Lobe"
Pattern: "RLL → Right Lower Lobe"
Pattern: "PNA | Pneumonia"
Pattern: "RUL/LUL/RLL/LLL" (with context lookup)
```

**TODO:**
- [ ] Add file upload field for shorthand PDFs on the admin panel's onboarding page
- [ ] Create shorthand extraction regex patterns (5 patterns minimum)
- [ ] Merge shorthand_map into keyterm_set before S4
- [ ] Test with 5 different hospital reference formats
- [ ] Add audit trail for shorthand source → keyterm mapping

---

### 3. Semantic Matching for Synonym Terms

**Current Problem:** "consolidation" vs "infiltrate" = same meaning, but fuzzy match fails.

**Implementation:**
```
Phase 0:
  "consolidation" vs "infiltrate" → confidence: 0.0 ❌

Phase 1:
  "consolidation" vs "infiltrate"
    ↓
  Check medical synonym database (RadLex or SNOMED CT)
    ↓
  Match found: Both = "SNOMED:39607008" (lung consolidation)
    ↓
  Confidence: 0.95 ✅
```

**Tools:**
- Use **RadLex API** (free, standard in radiology)
- Or local **SNOMED CT** UMLS database
- Or lightweight query: "Are these medical synonyms?"

**Accuracy Boost:** +4–6% on synonym-heavy templates

**TODO:**
- [ ] Integrate RadLex API for term lookup
- [ ] Build local SNOMED CT concept mapper
- [ ] Test on known synonym pairs (50+ pairs)
- [ ] Document synonyms used for future updates

---

### 4. Auto-Detect Unrecognized Terms During Live Usage

**User Feedback:** YES! Brilliant idea — continuous lexicon evolution.

**Implementation:**
```
Every radiologist edit → edit_event created

Check: Is term in lab-a-lexicon?

If NO:
  ├─ Log to potential_lexicon_term table
  └─ Track frequency

Weekly: Show radiologist approval UI:
  ├─ "You used 'ground glass opacity' 5 times"
  ├─ "Not in your lexicon — approve adding it?"
  └─ Radiologist: ☑️ YES
     ↓
     Create lexicon_set version 2 (auto)
```

**New Table:**
```sql
CREATE TABLE potential_lexicon_term (
  id UUID PRIMARY KEY,
  tenant_id UUID FK(tenant.id),
  surface_text TEXT,
  frequency INT,
  first_seen_at TIMESTAMP,
  last_seen_at TIMESTAMP,
  approved BOOLEAN DEFAULT false,
  approved_at TIMESTAMP,
  approved_by UUID FK(app_user.id),
  lexicon_set_version_id UUID (after approval)
);
```

**Radiologist Dashboard:**
```
"Approve New Terms for Lab A"
┌─────────────────────────────────────────┐
│ Term                    │ Frequency │ Act │
├─────────────────────────────────────────┤
│ ground glass opacity    │ 5         │ ✅  │
│ mosaic perfusion        │ 3         │ ✅  │
│ crazy paving pattern    │ 2         │ ✅  │
│ unusual finding (typo?) │ 1         │ ❌  │
└─────────────────────────────────────────┘
```

**Benefit:** Lexicon evolves with real-world usage (continuous improvement).

**TODO:**
- [ ] Create potential_lexicon_term table
- [ ] Add background job: aggregate edit_events → find new terms
- [ ] Build a lab-side radiologist approval UI (the approvals stay out of the admin panel)
- [ ] Auto-create lexicon_set version on approval
- [ ] Test workflow with Lab A (1 month of live data)

---

### 5. Confidence Score Thresholds & Auto-Approval

**Current:** All matches shown to radiologist (low-confidence clutter).

**Implementation:**
```
Phase 0:
  confidence > 0.0 → Show to radiologist ❌ (cluttered)

Phase 1:
  confidence > 0.85 → Auto-approve ✅
  0.60–0.85 → Show to radiologist (review only)
  < 0.60 → Hide (too risky)
  
  UI: "Unsure about 'tree and bud' ↔ 'tree-in-bud'?"
      [Confidence: 0.72]
      ☐ Yes, same  ☐ No, different  ☐ Unsure
```

**Impact:** Reduces radiologist burden from 100% review to ~20% review.

**TODO:**
- [ ] Define confidence thresholds per term type
- [ ] A/B test thresholds with radiologist
- [ ] Auto-approve high-confidence matches (log for audit)
- [ ] Track radiologist overrides (improve algorithm)

---

### 6. Multi-Language Support for Medical Terms

**Current:** English only.
**Problem:** Lab A is in India; mix of English + Hindi medical terms.

**Implementation:**
```
Phase 1:
  Support: English, Hindi, French, Spanish
  
  Hindi example:
    "फेफड़े की सूजन" (pneumonia in Hindi)
      ↓
    Normalize to English: "pneumonia"
      ↓
    Link to English lexicon
  
  Use: Google Translate API or local Indic models
```

**TODO:**
- [ ] Support Hindi term input (Latin script + native script)
- [ ] Add translation layer (Hindi → English)
- [ ] Test with Lab A Hindi-speaking radiologists
- [ ] Support 3–5 languages based on customer demand

---

## PHASE 2 (Future) — Lower Priority

### 7. Active Learning: Fine-Tune Custom Extraction Models

```
Use data from Phase 1 radiologist approvals
  ↓
Build custom Qwen fine-tune on YOUR templates
  ↓
Extraction accuracy improves to 96–98%
  ↓
Deploy self-hosted Qwen for all labs

Cost: One-time $500 fine-tune, then $0.00 per call
Accuracy: 96–98% (vs 88–92% base Qwen)
```

**TODO:**
- [ ] Collect Phase 1 radiologist approval data
- [ ] Fine-tune Qwen 7B on collected templates
- [ ] Test fine-tuned model vs base Qwen
- [ ] Deploy self-hosted fine-tuned Qwen
- [ ] Measure accuracy improvement

---

## Accuracy Projections

```
┌─────────────────────┬──────┬──────┬──────┬─────────┐
│ Approach            │ P0   │ P1   │ P2   │ Comments│
├─────────────────────┼──────┼──────┼──────┼─────────┤
│ Regex only          │ 85%  │ -    │ -    │ Base    │
│ + Qwen fallback     │ -    │ 92%  │ -    │ Cheap   │
│ + RadLex synonyms   │ -    │ +4%  │ -    │ Medical │
│ + Fine-tuned Qwen   │ -    │ -    │ 98%  │ Custom  │
│ Final (all stacked) │ 85%  │ 96%  │ 98%  │ Best    │
└─────────────────────┴──────┴──────┴──────┴─────────┘
```

---

## Whisper ASR Reliability Dependencies

**Question:** Does reliability depend ONLY on audio quality?

**Answer:** ~70% audio quality + ~30% other factors.

**Breakdown:**
```
1. AUDIO QUALITY (70%)
   ├─ Noise floor
   ├─ Clarity of speech
   ├─ Background sounds
   └─ Codec quality (FLAC > MP3)

2. SPEAKER FACTORS (15%)
   ├─ Accent (Indian English: 90%, Native: 95%)
   ├─ Pace (fast dictation: lower accuracy)
   ├─ Fatigue (end of day: 85% vs 92% morning)
   └─ Medical jargon familiarity

3. DOMAIN FACTORS (10%)
   ├─ Keyterm_set quality (good vocab: 92% vs bad: 85%)
   ├─ Whisper model version (large-v3 > base)
   └─ Temperature setting (0.0 = deterministic)

4. SYSTEM FACTORS (5%)
   ├─ Beam search width
   ├─ Decoding strategy
   └─ Language model weighting
```

**Example:**
```
Studio audio + native speaker + good keyterm_set:
  Whisper accuracy: 98% ✅

Hospital PA system + diverse accents + minimal keyterm_set:
  Whisper accuracy: 70% ❌

CONCLUSION: Audio quality important, but NOT the only factor.
Keyterm_set quality and speaker proficiency matter significantly.
```

---

## Cost Impact Summary

```
Current (Phase 0):  $0 (all regex + radiologist)
Phase 1 estimate:   +$2–3K upfront, then $0.0001 per template
Phase 2 estimate:   +$500 fine-tune, then $0 per template (self-hosted)

Payback: 2–3 labs × 20 templates/quarter × 10 quarters = 600 templates
  Phase 0 cost:  $0
  Phase 1 cost:  $2K + $0.06 = $2.06
  Phase 2 cost:  $2.5K + $0 = $2.5K (break-even by month 12)

Radiologist time saved per lab per year: 10 hours = $2–3K value
ROI: Positive by month 6–9 ✅
```

---

