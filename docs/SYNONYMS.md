# Radiology synonyms the lexicon treats as one finding

Generated from `radreport/knowledge/data/synonyms.csv` by `python -m radreport.knowledge.synonyms`; edit the CSV, then regenerate.
Two phrasings in one row match with confidence 0.95 (`knowledge/synonyms.py`). British spellings (haemorrhage, oedema, oesophagus, ischaemic, faecal, tumour) are folded to the American form before comparing.

No SNOMED CT or RadLex codes are stored here: an invented code is worse than none. RadLex identifiers are filled onto `lexicon_term.radlex_id` only from RadLex itself, by the onboarding step *Look terms up in RadLex* (needs `BIOPORTAL_API_KEY`).

| Concept | Preferred | Also written as |
|---|---|---|
| `appendicolith` | appendicolith | appendiceal calculus, fecalith |
| `ascites` | ascites | free fluid in the abdomen, intraperitoneal fluid, peritoneal fluid |
| `atelectasis` | atelectasis | lung collapse, plate atelectasis, subsegmental collapse |
| `bronchiectasis` | bronchiectasis | bronchial dilatation, dilated bronchi |
| `calcification` | calcification | calcific focus, calcified focus |
| `cardiomegaly` | cardiomegaly | cardiac enlargement, enlarged heart |
| `cerebral_infarct` | cerebral infarct | brain infarct, cerebral infarction, ischemic stroke |
| `choledocholithiasis` | choledocholithiasis | cbd calculus, cbd stone, common bile duct stone |
| `cholelithiasis` | cholelithiasis | gall stones, gallbladder calculi, gallstones |
| `copd` | chronic obstructive pulmonary disease | chronic obstructive airway disease, copd |
| `cirrhosis` | cirrhosis | cirrhotic liver, hepatic cirrhosis, liver cirrhosis |
| `consolidation` | consolidation | airspace disease, airspace opacity, alveolar opacity, infiltrate |
| `deep_vein_thrombosis` | deep vein thrombosis | deep venous thrombosis, dvt |
| `disc_herniation` | disc herniation | disc prolapse, herniated disc, pivd, prolapsed intervertebral disc |
| `emphysema` | emphysema | emphysematous changes |
| `gallbladder_wall_thickening` | gallbladder wall thickening | thickened gallbladder wall |
| `ground_glass_opacity` | ground glass opacity | ggo, ground glass attenuation, ground glass opacification |
| `hemangioma` | hemangioma | vascular malformation of the liver |
| `hepatic_cyst` | hepatic cyst | liver cyst |
| `hepatic_steatosis` | hepatic steatosis | fatty infiltration of the liver, fatty liver, steatosis |
| `hepatomegaly` | hepatomegaly | enlarged liver, liver enlargement |
| `hepatosplenomegaly` | hepatosplenomegaly | enlarged liver and spleen |
| `hiatal_hernia` | hiatal hernia | hiatus hernia |
| `hydronephrosis` | hydronephrosis | dilated pelvicalyceal system, pelvicaliectasis, pelvicalyceal dilatation |
| `interstitial_lung_disease` | interstitial lung disease | diffuse parenchymal lung disease, ild |
| `intracranial_hemorrhage` | intracranial hemorrhage | brain hemorrhage, intracranial bleed |
| `leiomyoma` | leiomyoma | fibroid, myoma, uterine fibroid |
| `lymphadenopathy` | lymphadenopathy | adenopathy, enlarged lymph nodes, lymph node enlargement |
| `nephrolithiasis` | nephrolithiasis | kidney stone, renal calculi, renal calculus, renal stone |
| `osteophyte` | osteophyte | bone spur, osteophytic lipping |
| `pericardial_effusion` | pericardial effusion | pericardial fluid |
| `pleural_effusion` | pleural effusion | fluid in the pleural space, pleural fluid |
| `pneumoperitoneum` | pneumoperitoneum | free air under the diaphragm, free gas under the diaphragm, free intraperitoneal air |
| `prostatomegaly` | prostatomegaly | enlarged prostate, prostatic enlargement |
| `pulmonary_edema` | pulmonary edema | fluid in the lungs, lung edema |
| `pulmonary_embolism` | pulmonary embolism | pulmonary thromboembolism |
| `renal_cyst` | renal cyst | kidney cyst |
| `small_bowel_obstruction` | small bowel obstruction | sbo, small intestinal obstruction |
| `splenomegaly` | splenomegaly | enlarged spleen, splenic enlargement |
| `subdural_hematoma` | subdural hematoma | subdural collection of blood, subdural hemorrhage |
| `tree_in_bud` | tree in bud | tree and bud, tree in bud nodularity |
| `ventricular_dilatation` | ventricular dilatation | dilated ventricles |
| `widened_mediastinum` | widened mediastinum | mediastinal widening |

