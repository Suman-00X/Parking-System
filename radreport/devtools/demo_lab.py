"""Fills a lab with demo data by calling the product's own HTTP API, the way an admin, radiologists and a transcriptionist would.

Order: sign in once per role (Session) -> onboard: templates, a shorthand sheet and a report corpus,
then the mining steps (onboard) -> the radiologist approves the templates, merges, collisions,
corpus mappings, critical rules and normals (approve) -> consents, studies and dictations; a
worker turns them into drafts (capture) -> reviews, signatures, addenda, grades and alerts (review)
-> transcripts, sound-alike mining and the new-terms scan (lexicon). Every step goes through the
write routes, so a run also checks them; each response is checked and the summary printed at the
end. Refused outside developer machines.

    python -m radreport.devtools.demo_lab --base-url http://127.0.0.1:8000 --lab sunrise
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from radreport.devtools.synthetic import synth_audio, synth_template_docx, with_spoken_text

CREDENTIALS = Path("local-credentials.md")

TEMPLATES: dict[str, tuple[tuple[str, bool], ...]] = {
    "usg_abdomen.docx": (("USG Abdomen", True), ("FINDINGS", True), ("Liver: Normal in size (__ cm) and [normal/increased] echotexture", False), ("Gallbladder: Well distended, no calculi [present/absent]", False), ("CBD: __ mm", False), ("Pancreas: Normal", False), ("Spleen: __ cm, normal echotexture", False), ("Right kidney: __ cm, no hydronephrosis", False), ("Left kidney: __ cm, no hydronephrosis", False), ("Urinary bladder: Normal", False), ("IMPRESSION", True), ("Impression: Normal study", False)),
    "ct_chest_plain.docx": (("CT Chest Plain", True), ("FINDINGS", True), ("Lungs: [clear/consolidation/ground glass]", False), ("Pleura: No effusion [present/absent]", False), ("Mediastinum: Largest node 3.2 cm in short axis", False), ("Heart: [normal/enlarged]", False), ("Airways: Patent", False), ("IMPRESSION", True), ("Impression: Unremarkable study", False)),
    "xray_chest_pa.docx": (("X-ray Chest PA", True), ("FINDINGS", True), ("Trachea: Central", False), ("Cardiothoracic ratio: __ [normal/increased]", False), ("Lung fields: [clear/opacity]", False), ("Costophrenic angles: [sharp/blunted]", False), ("Bony thorax: Intact", False), ("IMPRESSION", True), ("Impression: Normal chest radiograph", False)),
    "ct_kub.docx": (("CT KUB", True), ("FINDINGS", True), ("Right kidney: __ cm, calculus [present/absent]", False), ("Left kidney: __ cm, calculus [present/absent]", False), ("Ureters: [dilated/not dilated]", False), ("Urinary bladder: [normal/wall thickening]", False), ("IMPRESSION", True), ("Impression: No calculus", False)),
}
SHORTHAND = """Sunrise Imaging transcription shortcuts
LLL = Left lower lobe
RLL = Right lower lobe
RUL = Right upper lobe
GGO => ground glass opacity
CBD: common bile duct
IVC - inferior vena cava
HSM = Hepatosplenomegaly
SOL: space occupying lesion
CTR = Cardiothoracic ratio
KUB = Kidneys ureters bladder
"""
FINDINGS = {
    "usg": ["The liver is normal in size and echotexture. No focal lesion is seen.", "The liver is enlarged with increased echotexture, suggestive of fatty liver.", "The gallbladder is distended with a {n} mm calculus in the neck.", "The CBD measures {n} mm.", "The spleen is normal in size.", "The right kidney measures {k} cm. No hydronephrosis or calculus.", "The left kidney measures {k} cm with a {n} mm calculus.", "The urinary bladder is normal.", "Mild hepatosplenomegaly."],
    "ct": ["Both lungs are clear.", "Ground glass opacity in the right lower lobe.", "Consolidation in the left lower lobe with air bronchograms.", "No pleural effusion.", "Small right pleural effusion.", "Mediastinal lymph node measuring {n} mm.", "The heart is normal in size.", "Mosaic perfusion in both lungs.", "Tree-in-bud nodules in the right upper lobe."],
    "xr": ["Trachea is central.", "Cardiothoracic ratio is within normal limits.", "Both lung fields are clear.", "Opacity in the right lower zone.", "Costophrenic angles are sharp.", "Blunted left costophrenic angle.", "Bony thorax is intact."],
    "kub": ["The right kidney measures {k} cm with a {n} mm calculus.", "The left kidney is normal.", "No hydroureteronephrosis.", "The urinary bladder is normal.", "Mild right hydronephrosis."],
}
#: Part of the parser's proposed spoken code -> (template code, the spoken code a radiologist approves).
STUDY_CODES = {"kub": ("CT_KUB", "c t k u b"), "abdomen": ("US_ABDOMEN", "ultrasound abdomen"), "x-ray": ("XR_CHEST", "x ray chest"), "x ray": ("XR_CHEST", "x ray chest"), "xray": ("XR_CHEST", "x ray chest"), "chest": ("CT_CHEST", "c t chest")}
#: What each demo dictation says: the spoken study code first, then findings; one in seven has a critical finding.
DICTATIONS = [
    ("US", "abdomen", "USG Abdomen", "study type ultrasound abdomen. liver is normal in size and echotexture. gallbladder shows a 9 mm calculus in the neck. c b d measures 5 mm. spleen is normal. right kidney measures 10.2 cm no hydronephrosis. left kidney measures 10.6 cm. impression cholelithiasis."),
    ("CT", "chest", "CT Chest Plain", "study type c t chest. ground glass opacity in the right lower lobe. no pleural effusion. mediastinal node measuring 8 mm. heart is normal in size. impression likely infective ground glass opacity."),
    ("XR", "chest", "X-ray Chest PA", "study type x ray chest. trachea is central. cardiothoracic ratio is normal. opacity in the right lower zone. costophrenic angles are sharp. impression right lower zone pneumonia."),
    ("CT", "abdomen", "CT KUB", "study type c t k u b. right kidney measures 11 cm with a 6 mm calculus at the lower pole. left kidney is normal. ureters not dilated. urinary bladder is normal. impression right renal calculus."),
    ("CT", "chest", "CT Chest Plain", "study type c t chest. large right pneumothorax with mediastinal shift. no pleural effusion. impression right tension pneumothorax, urgent."),
    ("US", "abdomen", "USG Abdomen", "study type ultrasound abdomen. liver is enlarged with increased echotexture sorry with coarse echotexture. gallbladder is normal. spleen measures 14 cm. impression hepatosplenomegaly with fatty liver."),
    ("XR", "chest", "X-ray Chest PA", "study type x ray chest. both lung fields are clear. blunted left costophrenic angle. cardiothoracic ratio is increased. impression cardiomegaly with small left effusion."),
]
#: Edits radiologists make that add vocabulary the lexicon lacks, so the new-terms scan finds some.
NEW_TERM_EDITS = ["Bilateral ground glass opacity with mosaic perfusion and crazy paving pattern.", "Septal thickening with honeycombing at the bases.", "Ground glass opacity with interlobular septal thickening.", "Periportal oedema and gallbladder wall thickening.", "Mosaic perfusion with air trapping on expiration."]


class Failed(RuntimeError):
    pass


def _credentials(path: Path) -> dict[str, list[tuple[str, str]]]:
    if not path.exists():
        raise SystemExit(f"{path} not found: run `make seed-local` first, which creates the demo accounts and writes their passwords there")
    found: dict[str, list[tuple[str, str]]] = {}
    for role, email, password in re.findall(r"\| (\w+) \| [^|]+\| `([^`]+@[^`]+)` \| `([^`]+)` \|", path.read_text()):
        found.setdefault(role, []).append((email, password))
    return found


@dataclass
class Session:
    base_url: str
    lab_slug: str
    creds: dict[str, list[tuple[str, str]]]
    calls: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    _clients: dict[str, httpx.Client] = field(default_factory=dict)
    _sign_ins: list[float] = field(default_factory=list)

    def _pace(self) -> None:
        """Sign-in is limited to five a minute per address; wait rather than be refused."""
        now = time.monotonic()
        self._sign_ins = [t for t in self._sign_ins if now - t < 61]
        if len(self._sign_ins) >= 5:
            wait = 61 - (now - self._sign_ins[0])
            print(f"  (waiting {wait:.0f}s for the sign-in rate limit)")
            time.sleep(max(0.0, wait))
        self._sign_ins.append(time.monotonic())

    def client(self, who: str) -> httpx.Client:
        """`admin`, or a lab role, optionally `radiologist:1` for the second radiologist."""
        if who in self._clients:
            return self._clients[who]
        role, _, index = who.partition(":")
        email, password = self.creds[role if role != "admin" else "product_admin"][int(index or 0)]
        client = httpx.Client(base_url=self.base_url, headers={"Origin": self.base_url}, timeout=60, follow_redirects=False)
        self._pace()
        if role == "admin":
            response = client.post("/admin/login", data={"email": email, "password": password})
            if response.status_code != 303:
                raise Failed(f"admin sign-in: {response.status_code}")
        else:
            response = client.post("/auth/login", json={"lab": self.lab_slug, "email": email, "password": password})
            if response.status_code != 200:
                raise Failed(f"{who} sign-in: {response.status_code} {response.text[:200]}")
            client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        self._clients[who] = client
        return client

    def call(self, who: str, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kwargs: Any) -> Any:
        response = self.client(who).request(method, path, **kwargs)
        for _attempt in range(3):
            if response.status_code != 429:
                break
            # A per-user limit; the demo runs faster than a person would.
            time.sleep(20)
            response = self.client(who).request(method, path, **kwargs)
        key = f"{method} {re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27}', '{id}', path)}"
        self.calls[key] = self.calls.get(key, 0) + 1
        if response.status_code not in ok:
            message = f"{key} as {who}: {response.status_code} {response.text[:300]}"
            self.failures.append(message)
            print(f"  ! {message}")
            return None
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text


def onboard(s: Session, lab: str) -> dict[str, Any]:
    """Templates and the shorthand sheet go in; a radiologist approves the templates, which go live."""
    print("1/6 templates and shorthand")
    files = [("files", (name, synth_template_docx(body), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")) for name, body in TEMPLATES.items()]
    upload = s.call("admin", "POST", f"/admin/api/labs/{lab}/onboarding/templates", files=files) or {}
    s.call("admin", "POST", f"/admin/api/labs/{lab}/onboarding/shorthand", files=[("files", ("sunrise_shortcuts.txt", SHORTHAND.encode(), "text/plain"))])
    batch = upload.get("batch_id")
    approved = 0
    for candidate in s.call("radiologist", "GET", "/onboarding/templates/candidates", params={"batch_id": batch, "page_size": 100}) or []:
        proposed = (candidate.get("proposed_spoken_study_code") or "").lower()
        code, spoken = next(((c, w) for key, (c, w) in STUDY_CODES.items() if key in proposed), (candidate["proposed_code"], proposed))
        body = {"decision": "approved", "code": code, "spoken_study_code": spoken}
        if s.call("radiologist", "POST", f"/onboarding/templates/candidates/{candidate['id']}/review", json=body):
            approved += 1
    proposals = s.call("admin", "POST", f"/admin/api/labs/{lab}/onboarding/batches/{batch}/merge-proposals") or {}
    for proposal in proposals.get("items", []):
        s.call("radiologist", "POST", f"/onboarding/merge-proposals/{proposal['id']}/decide", json={"decision": "keep_separate"})
    applied = s.call("radiologist", "POST", f"/onboarding/batches/{batch}/apply") if batch else None
    return {"candidates_approved": approved, "applied": applied}


def corpus(s: Session, lab: str) -> dict[str, Any]:
    """Past reports, the mining steps over them, and the radiologist's checks of what they found."""
    print("2/6 report corpus and mining")
    rng = random.Random(7)
    titles = {"usg": "USG Abdomen", "ct": "CT Chest Plain", "xr": "X-ray Chest PA", "kub": "CT KUB"}
    records = []
    for i in range(240):
        kind = rng.choice(list(FINDINGS))
        lines = [f.format(n=rng.randint(3, 14), k=round(rng.uniform(8.5, 12.0), 1)) for f in rng.sample(FINDINGS[kind], k=min(4, len(FINDINGS[kind])))]
        records.append({"report_text": f"{titles[kind]}\nFINDINGS:\n" + "\n".join(lines) + "\nIMPRESSION:\n" + lines[0], "external_report_id": f"SUN-HIST-{i:04d}", "radiologist_employee_code": ("SUN-R01", "SUN-R02")[i % 2], "patient_sex": rng.choice("MF"), "patient_age_years": rng.randint(18, 85), "is_deidentified": True})
    loaded = s.call("admin", "POST", f"/admin/api/labs/{lab}/onboarding/corpus", json={"records": records})
    steps = {step: s.call("admin", "POST", f"/admin/api/labs/{lab}/onboarding/steps/{step}", json={}) for step in ("derive-map", "lexicon-mine", "collision-audit", "boilerplate-mine")}
    verified = 0
    for mapping in s.call("radiologist", "GET", "/onboarding/corpus/mappings", params={"page_size": 100}) or []:
        if verified >= 60:
            break
        if s.call("radiologist", "POST", f"/onboarding/corpus/mappings/{mapping['mapping_id']}/verify", json={}):
            verified += 1
    resolved = 0
    for finding in s.call("radiologist", "GET", "/onboarding/collision-findings", params={"page_size": 100}) or []:
        if s.call("radiologist", "POST", f"/onboarding/collision-findings/{finding['finding_id']}/resolve", json={"resolution": "accepted_with_margin_guard"}):
            resolved += 1
    promoted = 0
    export = (s.call("radiologist", "GET", "/onboarding/boilerplate/export") or {}).get("csv", "")
    for row in list(csv.reader(io.StringIO(export)))[1:6]:
        if row and "CRITICAL" not in row[4] and s.call("radiologist", "POST", f"/onboarding/boilerplate/{row[0]}/promote", json={"enable_auto_fill": False}):
            promoted += 1
    seeded = s.call("admin", "POST", f"/admin/api/labs/{lab}/onboarding/steps/critical-rules-seed", json={}) or {}
    rules = 0
    for rule in (seeded.get("candidates") or [])[:6]:
        pattern = {"PNEUMOTHORAX": "pneumothorax", "AORTIC_DISSECTION": "aortic dissection|dissection flap", "INTRACRANIAL_HAEMORRHAGE": "intracranial haemorrhage|intracranial hemorrhage", "PULMONARY_EMBOLISM": "pulmonary embolism|filling defect", "FREE_AIR": "free air|pneumoperitoneum", "ECTOPIC_PREGNANCY": "ectopic pregnancy"}.get(rule["code"], rule["finding_label"].lower())
        made = s.call("radiologist", "POST", "/onboarding/critical-rules", json={"code": rule["code"], "finding_label": rule["finding_label"], "pattern": pattern, "pattern_type": "lexical", "severity": rule["severity"], "sla_minutes": 30 if rule["severity"] == "red" else 120, "negation_sensitive": True, "requires_ack": True, "escalation_path": [{"notify": "referring_doctor", "within_minutes": 30 if rule["severity"] == "red" else 120}, {"notify": "lab_admin", "within_minutes": 60 if rule["severity"] == "red" else 240}]})
        rule_id = (made or {}).get("rule_id") or (made or {}).get("id")
        if rule_id and s.call("radiologist:1", "POST", f"/onboarding/critical-rules/{rule_id}/approve"):
            rules += 1
    return {"corpus": loaded, "steps": {k: {kk: vv for kk, vv in (v or {}).items() if not isinstance(vv, list)} for k, v in steps.items()}, "mappings_verified": verified, "collisions_resolved": resolved, "normals_promoted": promoted, "critical_rules_approved": rules}


def capture(s: Session, lab: str, *, count: int, run_worker: bool) -> dict[str, Any]:
    """Consents, studies and dictations; a worker turns each upload into a draft."""
    print("3/6 consents, studies and dictations")
    users = s.call("admin", "GET", f"/admin/api/labs/{lab}/users", params={"page_size": 100}) or []
    radiologists = [u for u in users if "radiologist" in u["roles"] and u["employee_code"].startswith("SUN-") and u.get("radiologist_profile_id")]
    for index, user in enumerate(radiologists):
        who = "radiologist" if index == 0 else "radiologist:1"
        s.call(who, "POST", f"/onboarding/radiologists/{user['radiologist_profile_id']}/training-consent", json={"consent_ref": f"SUN-CONSENT-{user['employee_code']}"})
        rng = random.Random(user["employee_code"])
        s.call(who, "POST", f"/onboarding/radiologists/{user['radiologist_profile_id']}/voice-enrollment", json={"consent_ref": f"SUN-VOICE-{user['employee_code']}", "embedding": [round(rng.uniform(-1, 1), 4) for _ in range(192)]}, ok=(200, 201, 409))
    uploaded = []
    for i in range(count):
        modality, part, description, words = DICTATIONS[i % len(DICTATIONS)]
        radiologist = radiologists[i % len(radiologists)]
        who = "radiologist" if i % len(radiologists) == 0 else "radiologist:1"
        study = s.call(who, "POST", "/ingest/studies", json={"mrn": f"SYNTH-SUN-{1000 + i // 2}", "accession_number": f"SUN-ACC-{20261000 + i}", "modality": modality, "body_part_examined": part, "study_description": description, "priority": "urgent" if "urgent" in words else "routine", "sex": "MF"[i % 2], "age_years": 30 + i % 50}, ok=(200, 201))
        if not study:
            continue
        audio = with_spoken_text(synth_audio(seconds=12.0 + i % 5, seed=1000 + i, audio_format="wav", snr_db=24.0, silence_ratio=0.15), words)
        recording = s.call(who, "POST", "/ingest/recordings", data={"study_id": study["study_id"], "radiologist_id": radiologist["radiologist_profile_id"], "capture_device_class": "dictation_mic_ptt", "is_push_to_talk": "true"}, files={"file": (f"dictation-{i}.wav", audio, "audio/wav")}, ok=(201, 409))
        if recording:
            uploaded.append(recording.get("recording_id"))
    worker = None
    if run_worker and uploaded:
        import subprocess

        # One batch per call; enough calls to drain every upload.
        for _ in range(len(uploaded) // 4 + 2):
            worker = subprocess.run([sys.executable, "-m", "radreport.workers", "--once", "--kinds", "run_pipeline", "--concurrency", "4"], capture_output=True, text=True, timeout=600).returncode
    return {"radiologists": len(radiologists), "recordings_uploaded": len(uploaded), "worker_exit": worker}


def review(s: Session) -> dict[str, Any]:
    """Drafts edited, signed, amended, graded and marked; alerts acknowledged."""
    print("4/6 review, sign, grade")
    queue = s.call("radiologist", "GET", "/review/queue") or []
    signed, edited, graded, addenda, acked, useless = 0, 0, 0, 0, 0, 0
    for index, item in enumerate(queue):
        draft_id = item["draft_id"]
        draft = s.call("radiologist", "GET", f"/review/drafts/{draft_id}") or {}
        for alert in (draft.get("signing") or {}).get("unacknowledged_alerts", []):
            alert_id = alert.get("alert_id") if isinstance(alert, dict) else alert
            if alert_id and s.call("radiologist", "POST", f"/review/alerts/{alert_id}/acknowledge", json={"outcome": "true_positive"}):
                acked += 1
        if index % 5 == 4:
            if s.call("radiologist", "POST", f"/review/drafts/{draft_id}/usefulness", json={"was_useless": True, "reason": "wrong template picked"}):
                useless += 1
            continue
        text = (draft.get("rendered_text") or draft.get("draft_text") or "Report.") + ("\n" + NEW_TERM_EDITS[index % len(NEW_TERM_EDITS)] if index % 2 == 0 else "")
        who = "transcriptionist" if index % 3 == 0 else "radiologist"
        # Edit a text field the way a radiologist would, adding wording the lexicon may not know yet.
        editable = [f for f in draft.get("fields", []) if f.get("value_text") is not None or f.get("value_enum") is None]
        edits = [{"field_value_id": editable[0]["field_value_id"], "value_text": ((editable[0].get("value_text") or "") + " " + NEW_TERM_EDITS[index % len(NEW_TERM_EDITS)]).strip()}] if editable and index % 2 == 0 else []
        if s.call(who, "POST", f"/review/drafts/{draft_id}/revisions", json={"edits": edits, "rendered_text": text, "active_edit_seconds": 40 + index, "wall_clock_seconds": 90 + index}):
            edited += 1
        if index % 4 == 3:
            continue  # left in the queue, edited but unsigned
        final = s.call("radiologist", "POST", f"/review/drafts/{draft_id}/sign")
        if not final:
            continue
        signed += 1
        report_id = final["final_report_id"]
        if s.call("radiologist", "POST", f"/review/reports/{report_id}/grade", json={"grade": ("G0", "G1", "G2", "G0", "G3")[index % 5], "note": "demo grade"}):
            graded += 1
        if index % 6 == 0 and s.call("radiologist", "POST", f"/review/reports/{report_id}/addendum", json={"rendered_text": text + "\nADDENDUM: Clinical correlation advised.", "reason": "clarification after referrer call"}):
            addenda += 1
    return {"queue": len(queue), "edited": edited, "signed": signed, "graded": graded, "addenda": addenda, "alerts_acknowledged": acked, "marked_useless": useless}


def lexicon(s: Session, lab: str) -> dict[str, Any]:
    """Verbatim transcripts, sound-alike mining, the new-terms scan, and a radiologist's answers."""
    print("5/6 transcripts, sound-alikes and new terms")
    waiting = s.call("transcriptionist", "GET", "/onboarding/verbatim/queue") or {}
    queue = [item for item in (waiting.get("current") or []) + (waiting.get("legacy") or []) if not item.get("has_verbatim")]
    transcripts = 0
    heard = ["Liver shows increased echo texture. Plural effusion on the right. Tree and bud nodules in the right upper lobe.", "The CBT measures 6 mm. Hepato spleno megaly noted. Echo texture of the liver is coarse.", "Ground glass opacity with plural effusion. Gall bladder is distended. Cardio thoracic ratio is normal."]
    for index, item in enumerate(queue[:12]):
        recording_id = item.get("recording_id") or item.get("id")
        if recording_id and s.call("transcriptionist", "POST", "/onboarding/verbatim", json={"recording_id": recording_id, "text": heard[index % len(heard)], "includes_disfluencies": True}):
            transcripts += 1
    mined = s.call("admin", "POST", f"/admin/api/labs/{lab}/onboarding/steps/mine-variants", json={"min_frequency": 1}) or {}
    scan = s.call("radiologist", "POST", "/lexicon/scan") or {}
    candidates = s.call("radiologist", "GET", "/lexicon/candidates", params={"page_size": 50}) or []
    approved = s.call("radiologist", "POST", "/lexicon/candidates/approve", json={"ids": [c["id"] for c in candidates[:2]]}) if len(candidates) >= 2 else None
    pending = s.call("radiologist", "GET", "/lexicon/variants") or []
    answered = 0
    for variant in pending[:1]:
        if s.call("radiologist", "POST", f"/lexicon/variants/{variant['id']}/decide", json={"answer": "same"}):
            answered += 1
    return {"transcripts": transcripts, "variants": {k: v for k, v in mined.items() if not isinstance(v, (list, dict))}, "scan": scan, "new_terms_waiting": len(candidates) - (2 if approved else 0), "new_lexicon_version": (approved or {}).get("version"), "variants_answered": answered, "variants_still_waiting": len(pending) - answered}


def settings(s: Session, lab: str) -> dict[str, Any]:
    """Lab settings a demo shows off: Hindi terms on, and the threshold experiment."""
    print("6/6 lab settings")
    out = {}
    for key, value in (("languages.hi", 1), ("languages.hi_latin", 1), ("lexicon.ab_test", 1)):
        out[key] = (s.call("admin", "POST", f"/admin/api/ops/config/{key}", json={"value": value, "tenant_id": lab}) or {}).get("value")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--lab", default="sunrise")
    parser.add_argument("--credentials", type=Path, default=CREDENTIALS)
    parser.add_argument("--dictations", type=int, default=16, help="studies and recordings to create")
    parser.add_argument("--no-worker", action="store_true", help="queue the pipeline jobs but do not run a worker")
    parser.add_argument("--force", action="store_true", help="run even if the lab already has templates")
    args = parser.parse_args(argv)
    s = Session(base_url=args.base_url.rstrip("/"), lab_slug=args.lab, creds=_credentials(args.credentials))
    labs = s.call("admin", "GET", "/admin/api/labs", params={"page_size": 100})
    lab = next((x["id"] for x in labs or [] if x["slug"] == args.lab), None)
    if lab is None:
        raise SystemExit(f"no lab {args.lab!r} on {args.base_url}")
    readiness = s.call("admin", "GET", f"/admin/api/labs/{lab}/readiness") or {}
    already = any(c.get("check_id") == "template_library_ready" and (c.get("measured_value") or 0) > 0 for c in readiness.get("checks", []))
    if already and not args.force:
        raise SystemExit(f"{args.lab} already has live templates; pass --force to add another round of demo data")
    summary = {"onboarding": onboard(s, lab), "corpus": corpus(s, lab)}
    summary["capture"] = capture(s, lab, count=args.dictations, run_worker=not args.no_worker)
    summary["review"] = review(s)
    summary["lexicon"] = lexicon(s, lab)
    summary["settings"] = settings(s, lab)
    print(json.dumps(summary, indent=1, default=str))
    print(f"\n{sum(s.calls.values())} calls over {len(s.calls)} routes; {len(s.failures)} failed")
    return 1 if s.failures else 0


if __name__ == "__main__":
    sys.exit(main())
