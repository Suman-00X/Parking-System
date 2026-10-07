# RadReport

**Speak the report. Get it back finished, checked and ready to sign.**

RadReport turns a radiologist's spoken dictation into a complete, structured medical report. It listens, writes, double-checks its own work, flags anything risky, and hands the doctor a draft that only needs a quick review and a signature.

---

## The problem

When a radiologist reads an X-ray, CT or MRI scan, they usually speak their findings into a recorder. Someone then types it up, fits it into the hospital's report format, and sends it back for checking. That takes hours, typos creep in, and an urgent finding can sit unnoticed in a queue.

Speech-to-text tools help with typing, but they create new risks. They mishear medical words, they sometimes "hear" words nobody said, and they don't understand when a doctor corrects themselves mid-sentence ("left lung — sorry, right lung").

## What RadReport does

1. **The doctor speaks.** They dictate the way they always have.
2. **RadReport writes the report.** It fills in the lab's own report template, field by field.
3. **RadReport checks itself.** Every statement is traced back to the exact moment in the audio where it was said. Anything it can't back up is flagged instead of guessed.
4. **The doctor reviews and signs.** Risky or uncertain parts are shown first, and urgent findings raise an alert straight away.
5. **The report goes to the hospital system** in the standard formats hospitals already use.

---

## By the numbers

| | |
|---|---|
| **17** | automated steps between "doctor speaks" and "report ready" |
| **11** | AI-powered steps that can each run on a different model, chosen per lab |
| **8** | guided steps to bring a new lab on board |
| **68** | database tables, each lab's rows walled off |
| **1,050** | automated tests, including tests that try to read one lab's data from another |
| **2** | hospital data standards supported for sending reports (HL7 v2 and FHIR R4) |
| **3** | languages besides English for medical terms (Hindi, French, Spanish) |
| **~29,000** | lines of code |

---

## Features

### For radiologists

**Speak, don't type**
Dictate naturally. RadReport turns speech into a finished report in the lab's own format, with sections, measurements and standard phrases in the right places.

**Understands self-corrections**
Doctors correct themselves all the time: "left — sorry, right". RadReport removes the mistake and keeps the correction, so the report says what the doctor meant, not what they said first.

**Click any sentence to hear it**
Every line in the draft is linked to the exact second of audio it came from. One click plays that moment, so checking a line takes seconds instead of replaying the whole recording.

**Urgent findings can't hide**
If the dictation mentions something dangerous, such as a collapsed lung or a bleed on the brain, RadReport raises an alert immediately and starts a response clock. A report with an unacknowledged alert can't be signed. It also understands "no" — "no fracture" does not trigger a fracture alert.

**The risky parts come first**
Each field gets a confidence score. Low-confidence and critical fields are put at the top and clearly marked, so the doctor's attention goes where it matters.

**Anything the system added is clearly marked**
If a standard "normal" sentence was filled in automatically rather than dictated, it is visibly tagged, so nobody signs something they didn't actually say.

**Mark a draft as useless**
If a draft is wrong, one button says so and why. That feedback goes straight into measuring and improving quality.

### Safety built in

**It never makes things up**
Every value in the report must point to the words in the dictation that support it. If it can't, the value is dropped and flagged, never quietly kept.

**Several "ears" vote on every word**
RadReport can run more than one speech-recognition engine and let them vote word by word. A word that only one engine "heard" loses the vote. This removes the most dangerous kind of error: findings that were never said.

**A second AI checks the first**
A separate reviewer model reads each draft and challenges anything that doesn't hold up, before a human ever sees it.

**Two-person sign-off**
An assistant can edit a draft, but only a radiologist can sign it. Every edit and every signature is recorded.

**Autonomy is earned, and taken back automatically**
For routine report types, RadReport can earn the right to send reports without review, but only after a long, measured track record. A small sample is always still checked by a person, and if quality slips, autonomy switches off automatically. Turning it on is hard. Turning it off is instant.

### For labs and hospitals

**Guided lab onboarding in 8 steps**
A checklist walks a new lab through everything RadReport needs: the staff list (with consent), report templates, past reports, the lab's own medical vocabulary, sample recordings, standard "normal" phrases, and the rules for urgent findings. A final readiness check says exactly what is still missing before the lab goes live.

**Learns each lab's language**
Every lab has its own terms, abbreviations and habits. RadReport learns them from the lab's templates and past reports, and catches words that sound alike but mean different things (for example two body parts with near-identical names) before they cause a mistake.

**Shorthand understood**
Labs can upload their abbreviation sheets ("LLL = left lower lobe"), and RadReport learns to recognise the shorthand in speech as well as the full terms.

**Matches medical synonyms**
Different doctors say the same thing different ways. RadReport links them through a curated set of radiology synonyms, so "hepatic steatosis" and "fatty liver" count as the same term, and it can look terms up in RadLex, the radiology vocabulary standard.

**Spots new words during daily use**
Every night RadReport reads what doctors typed into their reports, finds the medical terms the lab's vocabulary doesn't have yet ("ground glass opacity", used 5 times), and lists them for a radiologist. One click adds them as a new version of the vocabulary.

**Only asks when it isn't sure**
When a heard phrase sounds like a known term, RadReport scores how sure it is. Confident matches are used straight away and logged; unsure ones are put to a radiologist as a simple question ("Is 'plural effusion' the same as 'pleural effusion'?"), and weak ones are ignored. Abbreviations need a higher score, because one letter can change the finding. When a radiologist overrules an automatic match, that is counted, so the thresholds can be tuned.

**Works with messy templates too**
Neatly structured templates are read instantly. When the reader is unsure (prose, bullet lists, tables), a small AI model the lab chooses, which can run on the lab's own server, reads it as well. It may only add fields the document actually names, and a radiologist checks every field before the template goes live.

**Hindi, French and Spanish**
Doctors can say or type medical terms in Hindi (in Devanagari, or in Latin letters if the lab turns that on), French or Spanish, and RadReport maps them to the right English term ("गुर्दे की पथरी" becomes renal calculus). Each lab picks its languages, and adding another one is a word list.

**Template history with one-click rollback**
Every change to a report template is kept as a new version. If a change causes problems, the lab can roll back.

**Sends reports to hospital systems**
Finished reports are produced in HL7 v2 and FHIR R4, the two standards most hospital record systems accept.

**Recordings are checked on arrival**
Audio that is too quiet, too short, too noisy or in a lossy format is turned away at upload with a clear reason, instead of producing a poor report later. The same recording uploaded twice is recognised and not processed again.

### For the people running the platform

**One admin panel**
Product admins create and manage labs, staff accounts and permissions from one web panel that works on a phone as well as a desktop, in light or dark mode. Every sensitive action is written to a tamper-proof audit log.

**Pick the AI model for every step, per lab**
Each of the 11 AI-powered steps can be given its own model, separately for each lab: a top-tier cloud model for the high-stakes steps, a cheaper or locally hosted one for simple ones. No model can go live until it has passed a test against a set of known-correct reports.

**Cost under control**
Every report has a spending cap. Repeated parts of AI requests are cached so they cost a fraction of the price, and identical AI calls are answered from a cache instead of being paid for twice. A per-lab dashboard shows cost per report, per step, and over time, and flags sudden spikes.

**Quality you can measure**
Signed reports are graded on a five-level scale from "no change needed" to "clinically significant error". Quality tracking runs continuously, and an early-warning monitor flags when incoming dictations start to look different from what the system was tuned on.

**Fair tests for speech engines**
A built-in "bake-off" compares speech-recognition engines on the lab's own recordings and ranks them, punishing any engine that adds words that weren't said, however good its overall score.

**Tunable without code changes**
Thresholds are changed from the admin panel's settings page, for all labs or for one: when the system may adapt to a lab's voices, how sure a match must be before it is used, when a template goes to the AI reader, which languages a lab uses, and how long old data stays attached. Every change is logged.

**Learns from approvals, with consent**
Labs that agree can share their approved templates and decisions to train a better template reader. Only blank templates and the decisions leave; report text and patient data never do.

**Secure sign-in for everyone**
Admins and lab staff each sign in with their own account. Passwords are stored with strong one-way hashing, sessions expire, and an admin can sign a user out everywhere at once.

**Try it without signing up**
The login page has a **Test credentials** tab with a ready-to-use read-only demo account. One click fills in the form. On real deployments the tab doesn't appear.

---

## Privacy and security

- **Every lab is walled off.** Each lab's data is separated inside the database itself, not just by the app. Even a bug in the app can't show one lab's reports to another, and the test suite actively tries to break this wall.
- **Patient consent is tracked.** Consent for voice use and consent for training use are recorded separately, and with dates, so it's always possible to prove what was allowed and when.
- **Audio is encrypted at rest** in cloud storage.
- **Secret keys never touch the database.** The system stores only the name of the place a key is kept, and reads the key when it needs it.
- **No real patient data on developer machines.** Development uses generated sample data only.
- **Every request is checked against an access policy** that lists, per role, exactly which actions are allowed, with rate limits to stop abuse.

---

## Built to scale

RadReport is designed to grow from one lab to hundreds without a rewrite. These are the techniques that make that possible, in plain words.

| Technique | In plain words | Where RadReport uses it |
|---|---|---|
| **Multi-tenancy** | Many customers share one system, but each only ever sees their own data. | Every lab is isolated inside the database with row-level security. |
| **Database sharding** | Splitting data across several databases so no single one gets overloaded. | Labs can be spread across database shards, and a big lab can be pinned to its own. |
| **Consistent hashing** | A way to decide which shard a lab lives on so that adding a new shard only moves a few labs. | Picks each lab's shard; adding capacity doesn't reshuffle everyone. |
| **Table partitioning** | Splitting one huge table into monthly pieces so searches only look at the months they need. | Speech segments, reviewer edits, the audit log and pipeline step records are split by month, and recordings by lab. New months are added ahead automatically; old months can be detached and archived. |
| **Caching** | Keeping answers to common questions close at hand so they don't have to be worked out again. | AI prompt caching, AI response caching, and per-request caching of lab settings. |
| **Distributed cache (Redis)** | A shared, very fast memory store that every server can read. | Shares lab settings, roles, model choices and admin sessions across all servers, and can hold cached AI answers. |
| **Bloom filter** | A tiny, very fast "have I seen this before?" check that is never wrong when it says "no". | Instantly rules out duplicate recordings and unknown vocabulary words before touching the database. |
| **Message queues** | A waiting line for work, so a burst of uploads is handled steadily instead of all at once. | Each new recording becomes a job that workers pick up when they're free. |
| **Event streaming (Kafka)** | A permanent, replayable log of everything that happened, which many services can read independently. | "Recording uploaded", "draft ready", "report signed" events feed alerts, billing, analytics and hospital export separately. |
| **Transactional outbox** | Making sure a database change and the message announcing it either both happen or neither does. | No signed report can be missed by the hospital export, even if a server crashes mid-way. |
| **Idempotency** | Doing the same thing twice has the same effect as doing it once. | Re-uploading a recording or retrying a failed step never creates duplicates. |
| **Rate limiting** | Capping how often someone can make requests. | Per-user and per-address limits on every route, set in the access policy. |
| **Circuit breaker** | If a service keeps failing, stop calling it for a while instead of piling on. | Stops calling an AI provider that is failing, then tests it again after a cool-down. |
| **Retries with backoff** | Try again after a failure, waiting a little longer each time. | Temporary AI provider errors are retried with growing, randomised waits. |
| **Backpressure** | Slowing down intake when the system downstream is busy. | Caps how many AI and speech calls run at once, per provider. |
| **Connection pooling** | Reusing a small set of database connections instead of opening a new one for every request. | A connection pool in the app plus PgBouncer in front of the database; tested with 500 users at once. |
| **Read replicas** | Copies of the database that handle read-only traffic. | Dashboards, lists and exports read from a replica when one is set up; writes, and anything read right after a write, go to the main database. |
| **Materialized views** | Saving the result of an expensive report query and refreshing it on a schedule. | The evaluation set is pre-computed and refreshed daily. |
| **Vector search** | Finding things by meaning, not exact words. | Matches similar vocabulary terms and routes reports to the right template. |
| **Append-only audit log** | A record that can be added to but never edited. | Every sensitive action is logged; the logging account can't change or delete entries. |
| **Health and readiness checks** | Letting the load balancer know which servers are fit to take traffic. | `/health` says the server is alive and reports each dependency and the server's id; `/ready` checks the database, every shard and the schema version. |
| **Asynchronous processing** | Doing slow work without making anyone wait for it. | AI calls run in parallel, uploads hand the slow work to background workers, and part of the database layer is non-blocking. |

---

## Under the hood

**Language & framework:** Python, FastAPI
**Database:** PostgreSQL with pgvector (meaning-based search) and row-level security
**Storage:** S3-compatible object storage for audio
**AI:** Claude models (per-step choice), local open models, Whisper and Deepgram speech recognition
**Standards:** HL7 v2, FHIR R4
**Ops:** Docker, Alembic database migrations, PgBouncer, Redis, Redpanda/Kafka, background workers, automated test suite with 1,050 tests

---

## What's next

- Live connection to hospital radiology systems once each hospital's field mapping is agreed
- DICOM image-system integration, if labs need it
- A template reader fine-tuned on labs' approved templates (the export is ready; training waits for enough approvals)
- A real read replica and production measurements of cost and load
