# Terms in other languages

Radiologists at some labs say or type radiology terms in Hindi, or in French or Spanish. The
lexicon stays in English; the language layer (`knowledge/languages.py`) recognises those terms and
gives their English, so the rest of the system works unchanged.

## Turning it on

Per lab, under **System settings → Languages** (all off by default):

| Setting | What it does |
|---|---|
| `languages.hi` | Hindi in Devanagari, e.g. "फेफड़े की सूजन" → pneumonia, "गुर्दे की पथरी" → renal calculus |
| `languages.hi_latin` | Hindi typed in Latin letters (Hinglish), e.g. "pathri" → calculus, "gurda" → kidney. Needs Hindi on |
| `languages.fr` | French, e.g. "épanchement pleural" → pleural effusion |
| `languages.es` | Spanish, e.g. "derrame pleural" → pleural effusion |
| `languages.online_translation` | Devanagari words the dictionaries do not know are sent to Google Translate |

## Where it applies

- **Term lookup** (`knowledge/term_lookup.find_term`): a phrase that is one known foreign term
  resolves to the lab's English term, with `how = "translated_hi"` (or `_fr`, `_es`) and
  confidence 0.9 × the English match. The new-terms watch uses the same lookup, so a Hindi
  spelling of a known term is not offered as "new".
- **The pipeline's normalise stage**: each foreign term in a transcript becomes a resolution
  beside the transcript. The extraction prompt's glossary then lists, for example,
  `पित्ताशय की पथरी = gallstone`, and the model reads the sentence with that in hand. The
  transcript itself is never rewritten.

## How matching works

- Bundled dictionaries in `knowledge/data/languages/<code>.csv` (`english,native,latin`; several
  spellings separated by `|`): 51 Hindi, 25 French and 26 Spanish entries covering common
  anatomy, findings and history words.
- Text is normalised before lookup: lower case, accents off for Latin script, chandrabindu folded
  into anusvara and the nukta dropped for Devanagari, so "गाँठ" and "गांठ", or "फेफड़े" typed
  without the dot, all match.
- The longest phrase wins at each position ("गुर्दे की पथरी" is renal calculus, not "stone").
- Latin-script forms that are also English words (`pet`, `sir`, `rate`, `normal`, …) are never
  matched, so English text is never "translated". Hinglish is opt-in for the same reason.
- Hindi grammar words (है, की, में, …) and the danda are skipped.

## Online translation and patient data

Transcripts are patient data. The online translator only ever receives single Devanagari words the
dictionaries did not know, never a sentence, and only for a lab that turned
`languages.online_translation` on, with `GOOGLE_TRANSLATE_API_KEY` set. Before enabling it, put a
data-processing agreement with Google in place for that lab. If the service fails, the dictionary
results stand. Online results are marked `how = "online"`.

## Adding a language

1. Add `knowledge/data/languages/<code>.csv`.
2. Add a `Language` to `SUPPORTED` and a `languages.<code>` setting in `core/system_config.py`.
3. Add any Latin-script entries that are English words to `ENGLISH_COLLISIONS`.

Marathi, Tamil, Bengali or other Indic scripts need their Unicode range in the tokeniser as well.

## Not yet done

The dictionaries were written from general medical vocabulary. They have not been reviewed by
Hindi-speaking radiologists at a pilot lab. Have one check `hi.csv`, and grow it from the terms
those radiologists actually use.
