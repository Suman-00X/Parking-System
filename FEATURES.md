# RadReport

**Speak the report. Get it back finished, checked and ready to sign.**

RadReport turns a radiologist's spoken dictation into a complete, structured medical report. It listens, writes, double-checks its own work, flags anything risky, and hands the doctor a draft that only needs a quick review and a signature.

<!-- tab: features | Features -->

## The problem

When a radiologist reads an X-ray, CT or MRI scan, they usually speak their findings into a recorder. Someone then types it up, fits it into the hospital's report format, and sends it back for checking. That takes hours, typos creep in, and an urgent finding can sit unnoticed in a queue.

Speech-to-text tools help with typing, but they create new risks. They mishear medical words, they sometimes "hear" words nobody said, and they don't understand when a doctor corrects themselves mid-sentence ("left lung — sorry, right lung").

## What RadReport does

1. **The doctor speaks.** They dictate the way they always have.
2. **RadReport writes the report.** It fills in the lab's own report template, field by field.
3. **RadReport checks itself.** Every statement is traced back to the exact moment in the audio where it was said. Anything it can't back up is flagged instead of guessed.
4. **The doctor reviews and signs.** Risky or uncertain parts are shown first, and urgent findings raise an alert straight away.
5. **The report goes to the hospital system** in the standard formats hospitals already use.

## By the numbers

| | |
|---|---|
| **17** | automated steps between "doctor speaks" and "report ready" |
| **11** | AI-powered steps that can each run on a different model, chosen per lab |
| **8** | guided steps to bring a new lab on board |
| **68** | database tables, each lab's rows walled off |
| **1,100+** | automated tests, including tests that try to read one lab's data from another |
| **9 of 9** | deliberate crashes handled in the [crash test](FEATURES.md#crash-test) |
| **500** | users at once in the load test, 99.99% of requests served |
| **2** | hospital data standards supported for sending reports (HL7 v2 and FHIR R4) |
| **3** | languages besides English for medical terms (Hindi, French, Spanish) |
| **~31,000** | lines of code |

## Features

### For radiologists

**Speak, don't type**
Dictate naturally. RadReport turns speech into a finished report in the lab's own format, with sections, measurements and standard phrases in the right places.

> **Why:** Radiologists already dictate, and asking them to change habits is the quickest way to lose them. The product fits around the way they work instead of asking them to fill in forms.

**Understands self-corrections**
Doctors correct themselves all the time: "left — sorry, right". RadReport removes the mistake and keeps the correction, so the report says what the doctor meant, not what they said first.

> **Why:** Ordinary speech-to-text writes down both the slip and the fix. In radiology the slip is often the side of the body, the most dangerous error a report can carry. The fix is applied backwards to the words being taken back and forwards to the new ones; doing it the other way round would turn "left — sorry, right" into the wrong side.

**Click any sentence to hear it**
Every line in the draft is linked to the exact second of audio it came from. One click plays that moment, so checking a line takes seconds instead of replaying the whole recording.

> **Why:** A draft only saves time if checking it is faster than writing it. Linking each line to its audio turns "replay the recording" into a one-second listen, and that is where the time saving comes from.

**Urgent findings can't hide**
If the dictation mentions something dangerous, such as a collapsed lung or a bleed on the brain, RadReport raises an alert immediately and starts a response clock. A report with an unacknowledged alert can't be signed. It also understands "no" — "no fracture" does not trigger a fracture alert.

> **Why:** A missed urgent finding is the worst outcome in radiology, so this check uses written rules, not AI: it behaves the same way every time and can be tested. "No" is read within its own sentence and by position, because "no fracture; large pneumothorax" must still raise the alarm for the pneumothorax.

**The risky parts come first**
Each field gets a confidence score. Low-confidence and critical fields are put at the top and clearly marked, so the doctor's attention goes where it matters.

> **Why:** The overall score multiplies the weakest critical field by the average of all fields, instead of just averaging. An average would hide one shaky critical field among thirty solid ones, and that one field is exactly what the score is there to catch.

**Anything the system added is clearly marked**
If a standard "normal" sentence was filled in automatically rather than dictated, it is visibly tagged, so nobody signs something they didn't actually say.

> **Why:** A signature means "I said this". If filled-in text looked like dictated text, a doctor could sign a sentence they never spoke. Storing a standard phrase and choosing to use it are kept as two separate decisions, and automatic filling is off unless a lab turns it on.

**Mark a draft as useless**
If a draft is wrong, one button says so and why. That feedback goes straight into measuring and improving quality.

> **Why:** A draft that wasted the doctor's time is the failure that is easiest to miss. One click turns it into a number the quality reports count, instead of a silent workaround.

### Safety built in

**It never makes things up**
Every value in the report must point to the words in the dictation that support it. If it can't, the value is dropped and flagged, never quietly kept.

> **Why:** AI models can write fluent text that isn't in the source. Each value must quote the exact span of the transcript it came from, and the quote is checked against that span, not searched for anywhere. A phrase that appears somewhere in the dictation proves nothing about the spot the model pointed at.

**Several "ears" vote on every word**
RadReport can run more than one speech-recognition engine and let them vote word by word. A word that only one engine "heard" loses the vote. This removes the most dangerous kind of error: findings that were never said.

> **Why:** In the vote, "heard nothing here" counts as an answer. So a word one engine invented and two did not hear loses two to one. Agreement between engines counts for more than any engine's own confidence, because engines that invent words usually do it confidently.

**A second AI checks the first**
A separate reviewer model reads each draft and challenges anything that doesn't hold up, before a human ever sees it.

> **Why:** Fixed rules catch the mistakes someone thought to write down. The checker reads the finished draft against the dictation the way a reviewer would, and catches contradictions no rule anticipated. If the checker itself fails, the draft still goes to a person rather than being blocked: a safety net must not become a single point of failure.

**Two-person sign-off**
An assistant can edit a draft, but only a radiologist can sign it. Every edit and every signature is recorded.

> **Why:** Assistants make review cheaper, but the clinical and legal responsibility stays with the radiologist. The rule is enforced in the access policy every request passes through, so no screen and no API call can let an assistant sign.

**Autonomy is earned, and taken back automatically**
For routine report types, RadReport can earn the right to send reports without review, but only after a long, measured track record. A small sample is always still checked by a person, and if quality slips, autonomy switches off automatically. Turning it on is hard. Turning it off is instant.

> **Why:** Skipping review is the biggest saving and the biggest risk. Granting needs statistical evidence, enough volume and the radiologist's own consent. Revoking is automatic, driven by a running quality monitor. The checked sample is never released unreviewed, because it is what the monitor watches: release it and the monitor would freeze while still looking switched on.

### For labs and hospitals

**Guided lab onboarding in 8 steps**
A checklist walks a new lab through everything RadReport needs: the staff list (with consent), report templates, past reports, the lab's own medical vocabulary, sample recordings, standard "normal" phrases, and the rules for urgent findings. A final readiness check says exactly what is still missing before the lab goes live.

> **Why:** Most of an AI system's quality comes from how it is set up. A final readiness gate means a lab cannot go live with a missing template or no urgent-finding rules, and everyone can see exactly what is left to do.

**Learns each lab's language**
Every lab has its own terms, abbreviations and habits. RadReport learns them from the lab's templates and past reports, and catches words that sound alike but mean different things (for example two body parts with near-identical names) before they cause a mistake.

> **Why:** General speech recognition stumbles on each lab's own words. The sound-alike check runs before a template change is applied, and a change that would add a clashing pair is refused whole, rather than applied and flagged afterwards.

**Shorthand understood**
Labs can upload their abbreviation sheets ("LLL = left lower lobe"), and RadReport learns to recognise the shorthand in speech as well as the full terms.

> **Why:** Radiologists speak in abbreviations. Without a shorthand list, a speech engine hears "LLL" as "eel ell ell", and the finding never reaches the report.

**Matches medical synonyms**
Different doctors say the same thing different ways. RadReport links them through a curated set of radiology synonyms, so "hepatic steatosis" and "fatty liver" count as the same term, and it can look terms up in RadLex, the radiology vocabulary standard.

> **Why:** Counting "fatty liver" and "hepatic steatosis" as different terms splits the statistics and misses matches. A curated list keeps every equivalence reviewable, instead of letting a model guess which terms mean the same.

**Spots new words during daily use**
Every night RadReport reads what doctors typed into their reports, finds the medical terms the lab's vocabulary doesn't have yet ("ground glass opacity", used 5 times), and lists them for a radiologist. One click adds them as a new version of the vocabulary.

> **Why:** A vocabulary goes stale the day it is written. Doctors' own corrections are the best sign of what is missing, and a radiologist approves every new term, so the vocabulary keeps up without anyone maintaining it by hand and without unreviewed changes.

**Only asks when it isn't sure**
When a heard phrase sounds like a known term, RadReport scores how sure it is. Confident matches are used straight away and logged; unsure ones are put to a radiologist as a simple question ("Is 'plural effusion' the same as 'pleural effusion'?"), and weak ones are ignored. Abbreviations need a higher score, because one letter can change the finding. When a radiologist overrules an automatic match, that is counted, so the thresholds can be tuned.

> **Why:** Asking about everything wastes radiologists' time; asking about nothing lets mistakes in. Three bands (use, ask, ignore) spend people's attention only where the machine is unsure, and counting overrides turns tuning the thresholds into a measurement instead of a guess.

**Works with messy templates too**
Neatly structured templates are read instantly. When the reader is unsure (prose, bullet lists, tables), a small AI model the lab chooses, which can run on the lab's own server, reads it as well. It may only add fields the document actually names, and a radiologist checks every field before the template goes live.

> **Why:** A fast, free rule-based reader handles most templates; only the hard ones cost an AI call. The model may only add fields the document names, so it cannot invent structure, and running it on the lab's own server keeps documents in the building.

**Hindi, French and Spanish**
Doctors can say or type medical terms in Hindi (in Devanagari, or in Latin letters if the lab turns that on), French or Spanish, and RadReport maps them to the right English term ("गुर्दे की पथरी" becomes renal calculus). Each lab picks its languages, and adding another one is a word list.

> **Why:** Many doctors move between languages as they speak. Mapping every term to one English medical term keeps a single structured report whatever language a word was said in, and making a language a word list rather than code means a lab can be added without an engineering release.

**Template history with one-click rollback**
Every change to a report template is kept as a new version. If a change causes problems, the lab can roll back.

> **Why:** A bad template change affects every report after it. Versions are never edited, only added, so any change can be undone and every past report still points at the exact template it was written against.

**Sends reports to hospital systems**
Finished reports are produced in HL7 v2 and FHIR R4, the two standards most hospital record systems accept.

> **Why:** Hospitals don't change their systems for a vendor; speaking the standards they already use is the price of entry. The export refuses to send a message rather than put a placeholder where the hospital's case number should be.

**Recordings are checked on arrival**
Audio that is too quiet, too short, too noisy or in a lossy format is turned away at upload with a clear reason, instead of producing a poor report later. The same recording uploaded twice is recognised and not processed again.

> **Why:** A bad recording turns into a bad report hours later, when it is expensive to fix. Refusing it at upload, with the reason, lets the doctor re-record straight away. Lossy formats are refused because compression throws detail away for good.

### For the people running the platform

**One admin panel**
Product admins create and manage labs, staff accounts and permissions from one web panel that works on a phone as well as a desktop, in light or dark mode. Every sensitive action is written to a tamper-proof audit log.

> **Why:** "Who changed this, and when?" is the first thing a healthcare customer or an auditor asks. The log can be added to but never edited, even by the account that writes it.

**Pick the AI model for every step, per lab**
Each of the 11 AI-powered steps can be given its own model, separately for each lab: a top-tier cloud model for the high-stakes steps, a cheaper or locally hosted one for simple ones. No model can go live until it has passed a test against a set of known-correct reports.

> **Why:** Not every step needs the most expensive model, but the high-stakes ones refuse a small local model outright. The test gate means switching to a cheaper model can never quietly lower quality: a model with no passing test result cannot serve real reports.

**Cost under control**
Every report has a spending cap. Repeated parts of AI requests are cached so they cost a fraction of the price, and identical AI calls are answered from a cache instead of being paid for twice. A per-lab dashboard shows cost per report, per step, and over time, and flags sudden spikes.

> **Why:** AI spend grows with every report. A hard cap stops a runaway report, and every prompt puts its unchanging part first so the provider can cache it. That ordering is built into how prompts are written, because a prompt that changes its opening on every call pays full price while still looking cached.

**Quality you can measure**
Signed reports are graded on a five-level scale from "no change needed" to "clinically significant error". Quality tracking runs continuously, and an early-warning monitor flags when incoming dictations start to look different from what the system was tuned on.

> **Why:** "It seems fine" does not convince a hospital. A graded error scale, compared against the lab's own error rate before RadReport, turns quality into a number that can be tracked and defended. The early-warning monitor raises a flag but never switches anything off by itself.

**Fair tests for speech engines**
A built-in "bake-off" compares speech-recognition engines on the lab's own recordings and ranks them, punishing any engine that adds words that weren't said, however good its overall score.

> **Why:** The engine with the best overall accuracy can still be the one that invents findings, so added words are scored on their own. Only recordings from the lab's current microphones count: results from old equipment would pick the engine that was best on hardware being replaced.

**Tunable without code changes**
Thresholds are changed from the admin panel's settings page, for all labs or for one: when the system may adapt to a lab's voices, how sure a match must be before it is used, when a template goes to the AI reader, which languages a lab uses, and how long old data stays attached. Every change is logged.

> **Why:** A pilot lab needs different settings from a mature one. Moving the thresholds into logged settings lets the operations team tune them without an engineering release, and every change can be traced to a person and a time.

**Learns from approvals, with consent**
Labs that agree can share their approved templates and decisions to train a better template reader. Only blank templates and the decisions leave; report text and patient data never do.

> **Why:** Shared data makes the reader better for everyone, but medical data needs explicit permission. The question is asked when a lab signs up, because that is the one moment the answer is known, and it is far harder to add after contracts are signed.

**See inside the running system**
Live dashboards show how fast every page and every step of report-writing is, what the AI is costing, how much work is waiting in line, and how often the cache helps. Every request can be followed step by step, and errors are reported the moment they happen.

> **Why:** A system you cannot see into fails quietly. Nothing on the dashboards is labelled by lab or carries patient data: a label per lab would grow without limit and name customers, and error reports are stripped of request bodies, messages and passwords before they leave the server.

**Secure sign-in for everyone**
Admins and lab staff each sign in with their own account. Passwords are stored with strong one-way hashing, sessions expire, and an admin can sign a user out everywhere at once.

> **Why:** Lab access tokens last minutes, not days, which limits the damage of a stolen one. Sign-in attempts are rate-limited, and a wrong password and an unknown email get the same answer, so nobody can probe which accounts exist.

**Try it without signing up**
The login page has a **Test credentials** tab with a ready-to-use read-only demo account. One click fills in the form. On real deployments the tab doesn't appear.

> **Why:** Recruiters and evaluators should not need an account. The demo accounts are read-only, so a visitor can open every screen and change nothing, and they don't exist unless a deployment configures them.

## Privacy and security

- **Every lab is walled off.** Each lab's data is separated inside the database itself, not just by the app. Even a bug in the app can't show one lab's reports to another, and the test suite actively tries to break this wall.
- **Patient consent is tracked.** Consent for voice use and consent for training use are recorded separately, and with dates, so it's always possible to prove what was allowed and when.
- **Audio is encrypted at rest** in cloud storage, and played back through short-lived links that are never cached.
- **Secret keys never touch the database.** The system stores only the name of the place a key is kept, and reads the key when it needs it.
- **No real patient data on developer machines.** Development uses generated sample data only.
- **Every request is checked against an access policy** that lists, per role, exactly which actions are allowed, with rate limits to stop abuse.

## What's next

- Live connection to hospital radiology systems once each hospital's field mapping is agreed
- DICOM image-system integration, if labs need it
- A template reader fine-tuned on labs' approved templates (the export is ready; training waits for enough approvals)
- A real read replica and production measurements of cost and load

<!-- tab: in-action | For Recruiters -->

## For Recruiters

<!-- media: demo-sign-in | Signing in to the demo lab with the Test credentials tab, straight into the review queue -->

<!-- media: admin-panel | The read-only admin account: every lab on the platform, then cost and usage -->

<!-- tab: hld | System design -->

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
| **CDN** | Copies of the site's files kept close to visitors around the world. | Stylesheets and scripts are cached at the edge for a year; their address changes with every release, so nothing is ever stale. Pages, audio and lab data are never cached. |
| **Observability** | Measuring a running system from the outside: how fast, how busy, what failed. | Prometheus metrics, OpenTelemetry traces and Sentry error reports, with dashboards in Grafana. |

## Seeing inside it

| What | Tool | What it shows |
|---|---|---|
| **Metrics** | Prometheus + Grafana | Requests per second and slowest 5% per route, server errors, rate-limited requests, time per report-writing step, AI spend per hour, prompt-cache share, jobs waiting and their age, unsent events, cache hit rate |
| **Traces** | OpenTelemetry | One request followed through every database query and every report-writing step |
| **Errors** | Sentry | Every unhandled error, with request bodies, messages, cookies and passwords removed first |
| **Synthetic traffic** | k6 on a schedule | A light, steady load against the live demo, so the dashboards always show real numbers |

## Under the hood

**Language & framework:** Python, FastAPI
**Database:** PostgreSQL with pgvector (meaning-based search) and row-level security
**Storage:** S3-compatible object storage for audio
**AI:** Claude models (per-step choice), local open models, Whisper and Deepgram speech recognition
**Standards:** HL7 v2, FHIR R4
**Ops:** Docker, Alembic database migrations, PgBouncer, Redis, Redpanda/Kafka, background workers, Prometheus, Grafana, OpenTelemetry, Sentry, k6, automated test suite with 1,100+ tests

<!-- tab: crash-test | Crash test -->

## Crash test

Good systems are judged by what happens when things go wrong. The crash test breaks RadReport on purpose: it kills a worker in the middle of a job, crashes the message relay at the worst possible moment, takes the database and the cache away, and floods the site. It then records what actually happened. Nothing below is simulated in a slide; every number comes from the last real run.

<!-- include: docs/CRASH_TEST.md -->
