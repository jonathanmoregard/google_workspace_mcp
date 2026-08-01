"""Where an edit can go, and what kind of edit it is.

Two jobs, kept in one module because they are the same idea seen from two
sides.

**Structure.** :func:`paragraphs`, :func:`sentences` and :func:`words` cut
the base text at linguistic boundaries. Every candidate edit is anchored to
one of those boundaries -- a word, a phrase, a clause, a whole sentence --
and never to an arbitrary character offset. That is not a stylistic
preference: a benchmark whose suggestions start mid-word measures index
arithmetic, and index arithmetic is not what breaks at 120 suggestions.

**Kinds.** :data:`KINDS` is a catalogue of the edits copyeditors and subject
experts actually make -- weakening a hedge, cutting throat-clearing,
normalising a spelling, splitting a run-on, inserting a citation, rewriting
a claim. Each kind knows how to *find its own targets* by scanning the
prose, so the sampler never has to invent a location: it picks a kind
according to a realistic frequency distribution and then picks one of the
places that kind genuinely applies.

All offsets are grapheme indexes into the **base** text. The documents are
one grapheme per code point (enforced in :mod:`.prose`), so regex offsets
are model offsets; the walk maps base offsets to live document offsets as
insertions accumulate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from llmux.scenarios.stressgen.prose import Document

# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

#: Sentence openers safe to lowercase when a transition is prepended. Kept as
#: a whitelist rather than a rule so no proper noun is ever downcased.
SAFE_OPENERS = frozenset(
    """The This That These Those We It There They A An In On At For But And If Our
    Your You Most Some Each Both Any Many Few One Two Three Screening Providers
    Forecasts Compute Policy Training Research People Teams Firms Government
    Age Prices Enjoyment Concentration Aggregating Participating Publication
    Question Senior Manufacturers Earning Operations Testing Take Almost
    Nearly Nothing Every Under Second Third Finally Conversely Whether
    Accuracy Reported Fragment Attestation New Retrofitting Months Both""".split()
)


def paragraphs(text: str) -> list[tuple[int, int]]:
    """``[start, end)`` of each newline-delimited block, newline excluded."""
    out: list[tuple[int, int]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch == "\n":
            if i > start:
                out.append((start, i))
            start = i + 1
    if start < len(text):
        out.append((start, len(text)))
    return out


_SPLIT = re.compile(r'(?<=[.!?])["”]?\s+')
_LIST_MARKER = re.compile(r"^\d+\.$")


def sentences(text: str, span: tuple[int, int]) -> list[tuple[int, int]]:
    """``[start, end)`` of each sentence inside one paragraph ``span``.

    Splits on terminal punctuation followed by whitespace, with two guards
    that matter for this corpus: a decimal point is never a boundary (there
    is no space after it), and a numbered-list marker (``1.``) does not open
    a one-token sentence.
    """
    start, end = span
    out: list[tuple[int, int]] = []
    cursor = start
    for match in _SPLIT.finditer(text, start, end):
        left = text[cursor : match.start()].strip()
        if not left or _LIST_MARKER.match(left):
            continue
        out.append((cursor, match.start()))
        cursor = match.end()
    if cursor < end and text[cursor:end].strip():
        out.append((cursor, end))
    return out


def all_sentences(text: str) -> list[tuple[int, int]]:
    """Every sentence in the document, paragraph by paragraph.

    Sentence detection is deliberately per-paragraph: a heading carries no
    terminal punctuation, and letting it run into the paragraph below would
    put every following edit in the wrong sentence.
    """
    out: list[tuple[int, int]] = []
    for block in paragraphs(text):
        out.extend(sentences(text, block))
    return out


_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


def words(text: str, span: tuple[int, int]) -> list[tuple[int, int]]:
    """``[start, end)`` of each word token inside ``span``."""
    return [(m.start(), m.end()) for m in _WORD.finditer(text, *span)]


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One editorial edit that could be made, located on a real boundary."""

    kind: str
    op: str  # insert | delete | replace
    start: int  # base grapheme index
    end: int  # base grapheme index; == start for an insertion
    text: str  # inserted / replacement text; "" for a deletion
    section: str  # heading governing ``start``
    sentence: tuple[int, int]  # enclosing sentence, for density control

    @property
    def size(self) -> int:
        """Characters touched -- struck plus added. Drives the size profile."""
        return (self.end - self.start) + len(self.text)


Finder = Callable[[Document, str], Iterator[tuple[int, int, str]]]


@dataclass(frozen=True)
class Kind:
    """One editorial move: how often it happens and where it applies.

    ``weight`` is a *frequency* in a real review, not a difficulty. Small
    copyedits are common; whole-claim rewrites are rare. Getting this
    distribution roughly right is what stops the corpus from being 120
    identical operations.
    """

    name: str
    op: str
    weight: float
    finder: Finder
    #: Human phrasing used in briefs and reports.
    blurb: str


def anchored(literal: str) -> str:
    """``literal`` as a regex that cannot match half a word.

    Word boundaries are added only at ends that are alphanumeric, so a
    phrase written with its trailing space (``"We would add one caution. "``)
    still matches. Without this, ``centre -> center`` fires inside
    ``centred`` and produces ``centerd``.
    """
    prefix = r"\b" if literal[:1].isalnum() else ""
    suffix = r"\b" if literal[-1:].isalnum() else ""
    return prefix + re.escape(literal) + suffix


def _phrase_finder(pairs: tuple[tuple[str, str], ...]) -> Finder:
    """Literal ``old -> new`` replacements (deletion when ``new`` is empty)."""

    def find(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
        for old, new in pairs:
            for match in re.finditer(anchored(old), text):
                yield (match.start(), match.end(), new)

    return find


def _regex_finder(
    pattern: str,
    build: Callable[[re.Match[str]], Optional[tuple[int, int, str]]],
) -> Finder:
    compiled = re.compile(pattern)

    def find(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
        for match in compiled.finditer(text):
            built = build(match)
            if built is not None:
                yield built

    return find


# -- the phrase tables ------------------------------------------------------

HEDGE_WEAKENING = (
    ("will keep rising", "may keep rising"),
    ("will keep growing", "may keep growing"),
    ("will stay small", "is likely to stay small"),
    ("will not stop", "may not stop"),
    ("will require", "is likely to require"),
    ("will tell you more", "usually tells you more"),
    ("will need", "may need"),
    ("shows that", "suggests that"),
    ("demonstrates", "suggests"),
    ("proves", "indicates"),
    ("is correct", "seems correct to us"),
    ("cannot be picked up quickly", "is hard to pick up quickly"),
    (
        "This is correct and not to the point",
        "We think this is correct but beside the point",
    ),
)

HEDGE_STRENGTHENING = (
    ("we think the advice is good", "the advice is good"),
    ("seems right to us", "is right"),
    ("we would gently push back", "we would push back"),
)

INTENSIFIERS = (
    "clearly",
    "obviously",
    "very",
    "really",
    "quite",
    "genuinely",
    "enormously",
    "substantially",
    "deliberately",
    "actually",
    "simply",
    "entirely",
    "highly",
    "particularly",
    "extremely",
    "remarkably",
    "considerably",
)

WORDINESS = (
    ("in order to", "to"),
    ("In order to", "To"),
    ("a number of", "several"),
    ("A number of", "Several"),
    ("due to the fact that", "because"),
    ("at this point in time", "now"),
    ("the vast majority of", "most"),
    ("a large fraction of", "much of"),
    ("a great deal of", "much"),
    ("A great deal of", "Much"),
    ("in the sense that", "because"),
    ("with respect to", "on"),
    ("prior to", "before"),
    ("is able to", "can"),
    ("are able to", "can"),
    ("has the ability to", "can"),
    ("in terms of", "in"),
    ("more than people expect", "less than people expect"),
)

BRITISH_TO_AMERICAN = (
    ("specialised", "specialized"),
    ("organisations", "organizations"),
    ("organisation", "organization"),
    ("modelling", "modeling"),
    ("centre", "center"),
    ("programmes", "programs"),
    ("programme", "program"),
    ("prioritise", "prioritize"),
    ("emphasise", "emphasize"),
    ("emphasises", "emphasizes"),
    ("normalise", "normalize"),
    ("normalises", "normalizes"),
    ("randomise", "randomize"),
    ("recognised", "recognized"),
    ("publicised", "publicized"),
    ("anonymised", "anonymized"),
    ("synthesiser", "synthesizer"),
    ("synthesisers", "synthesizers"),
    ("behaviour", "behavior"),
    ("judgement", "judgment"),
    ("labour", "labor"),
    ("defence", "defense"),
    ("licence", "license"),
    ("practise", "practice"),
    ("towards", "toward"),
    ("whilst", "while"),
    ("amongst", "among"),
    ("analyse", "analyze"),
    ("analyses", "analyzes"),
    ("catalogue", "catalog"),
)

JARGON = (
    ("load-bearing", "essential"),
    ("binding constraint", "main bottleneck"),
    ("tacit knowledge", "hands-on know-how"),
    ("counterfactual", "difference-making"),
    ("marginal value", "added value"),
    ("marginal risk", "added risk"),
    ("information hazard", "risk of publishing harmful detail"),
    ("incentive gradient", "set of incentives"),
    ("critical path", "list of things that must happen"),
    ("runway", "savings"),
    ("failure mode", "way this goes wrong"),
    ("failure modes", "ways this goes wrong"),
    ("natural experiment", "real-world test"),
)

PASSIVE_TO_ACTIVE = (
    ("It has been replicated", "Researchers have replicated it"),
    (
        "has been demonstrated in the open literature",
        "researchers have demonstrated in published work",
    ),
    ("is judged to present", "presents"),
    ("are reported at", "sit at"),
    ("is reported", "we report"),
    ("has been enormously beneficial", "has enormously benefited"),
    ("are chosen because", "the organisers choose because"),
    ("is heavily mediated by", "depends heavily on"),
    ("was never designed as", "nobody designed it as"),
)

NUMBER_FORMAT = (
    ("10 percent", "10%"),
    ("2 percent", "2%"),
    ("1.5 percent", "1.5%"),
    ("0.2 percent", "0.2%"),
    ("60 percent", "60%"),
    ("$400M", "$400 million"),
    ("$6M", "$6 million"),
    ("eighteen months", "18 months"),
    ("twelve months", "12 months"),
    ("two orders of magnitude", "100-fold"),
    ("4-5x per year", "4 to 5 times per year"),
    ("200 base pairs", "200 base pairs (bp)"),
    ("34 studies", "34 studies (see references)"),
    ("11 practitioners", "eleven practitioners"),
    ("9 organisations", "nine organisations"),
)

THROAT_CLEARING = (
    ("It is worth noting that this path", "This path"),
    ("it is important to remember that a well-monitored race", "a well-monitored race"),
    ("We want to be careful not to overclaim in the other direction. ", ""),
    ("We would add one caution. ", ""),
    (
        "We flag this without a recommendation attached, because ",
        "We do not attach a recommendation, because ",
    ),
    ("The honest summary of this model is that it", "This model"),
    ("The other thing we would say to people", "To people"),
    ("We would gently push back on the framing", "We would push back on the framing"),
)

TRANSITIONS = (
    "That said, ",
    "In practice, ",
    "By contrast, ",
    "Even so, ",
    "For what it is worth, ",
)

CITATIONS = (
    " (Mellers et al., 2014)",
    " (Epoch AI, 2024)",
    " (see the references at the end)",
    " [citation needed]",
    " (source: our 2024 practitioner interviews)",
)

#: Hedges a reviewer drops in after an auxiliary verb ("is" -> "is probably").
#: The commonest substantive note on a research draft, and the one that
#: produces the most argument.
HEDGE_INSERTS = (
    " probably",
    " arguably",
    " in our view",
    " we think",
    " on balance",
)

_AUXILIARY = re.compile(
    r"\b(?:is|are|was|were|has|have|had|can|will|would|should|may|might) (?=[a-z])"
)


def _hedge_insert_finder(
    document: Document, text: str
) -> Iterator[tuple[int, int, str]]:
    for match in _AUXILIARY.finditer(text):
        at = match.end() - 1  # the space after the verb
        for hedge in HEDGE_INSERTS:
            yield (at, at, hedge)


CAVEATS = (
    ", though the underlying estimate is uncertain",
    ", though we are not confident in this figure",
    ", on the best public numbers we could find",
    ", and the spread across sources is wide",
)


#: Single-word swaps of the kind a copyeditor makes on a first pass. One
#: direction only, so no pair can cycle with another.
WORD_CHOICE = (
    ("enormously", "greatly"),
    ("chronically", "persistently"),
    ("routinely", "regularly"),
    ("meaningfully", "measurably"),
    ("plausible", "credible"),
    ("crude", "rough"),
    ("blunt", "imprecise"),
    ("robust", "well-supported"),
    ("modest", "small"),
    ("sharply", "markedly"),
    ("broadly", "roughly"),
    ("largely", "mostly"),
    ("unusually", "exceptionally"),
    ("frequently", "often"),
    ("necessarily", "inevitably"),
    ("effectively", "in effect"),
    ("primarily", "mainly"),
    ("essentially", "in effect"),
    ("accordingly", "as a result"),
    ("nonetheless", "even so"),
    ("consequently", "as a result"),
    ("thereafter", "afterwards"),
    ("approximately", "about"),
    ("numerous", "many"),
    ("utilise", "use"),
    ("commence", "start"),
    ("terminate", "end"),
    ("ascertain", "find out"),
    ("endeavour", "try"),
    ("comprise", "make up"),
    ("obtain", "get"),
    ("purchase", "buy"),
    ("sufficient", "enough"),
    ("additional", "more"),
    ("subsequent", "later"),
    ("initial", "first"),
    ("assist", "help"),
    ("require", "need"),
    ("requires", "needs"),
    ("attempt", "try"),
    ("demonstrate", "show"),
    ("indicate", "show"),
    ("facilitate", "help"),
    ("implement", "carry out"),
    ("methodology", "method"),
    ("individuals", "people"),
    ("currently", "now"),
    ("presently", "now"),
    ("previously", "before"),
    ("regarding", "about"),
    ("concerning", "about"),
    ("via", "through"),
)

#: Fillers a reviewer strikes without changing the meaning.
FILLERS = (
    " at all",
    " of course",
    " in fact",
    " as it happens",
    " if anything",
    " to be clear",
    " on balance",
    " in general",
    " for what it is worth",
    " it turns out",
    " in the end",
    " after all",
)

_OPTIONAL_THAT = re.compile(
    r"\b(said|says|think|thinks|thought|know|knows|found|finds|suggests|suggest|"
    r"shows|show|argues|argue|reported|report|note|noted|noticed|believe|believes|"
    r"agree|agrees|assume|assumes|mean|means|conclude|concluded|acknowledge|"
    r"expect|expects|hope|hopes|worry|worried|claim|claims) that "
)


def _optional_that_finder(
    document: Document, text: str
) -> Iterator[tuple[int, int, str]]:
    """Strike the optional ``that`` after a reporting verb."""
    for match in _OPTIONAL_THAT.finditer(text):
        yield (match.end() - 5, match.end(), "")


def _that_which_finder(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
    """Restrictive ``which`` -> ``that`` (only where no comma precedes it)."""
    for match in re.finditer(r"(?<![,;]) which ", text):
        yield (match.start(), match.end(), " that ")


def _filler_finder(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
    for filler in FILLERS:
        for match in re.finditer(anchored(filler) + r"(?=[ ,.;])", text):
            yield (match.start(), match.end(), "")


def _word_choice_finder(
    document: Document, text: str
) -> Iterator[tuple[int, int, str]]:
    for old, new in WORD_CHOICE:
        for match in re.finditer(r"\b" + re.escape(old) + r"\b", text):
            yield (match.start(), match.end(), new)


def _intensifier_finder(
    document: Document, text: str
) -> Iterator[tuple[int, int, str]]:
    """Delete an intensifier and the space after it, mid-sentence only."""
    pattern = r"\b(?:" + "|".join(INTENSIFIERS) + r") "
    for match in re.finditer(pattern, text):
        before = text[match.start() - 2 : match.start()]
        if match.start() == 0 or before.endswith(("\n", ". ", "? ")):
            continue  # sentence-initial: deleting it would break the capital
        yield (match.start(), match.end(), "")


def _run_on_finder(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
    """``..., and the ...`` -> ``.... The ...`` -- split a run-on sentence.

    Only coordinating conjunctions: cutting at a relative ``which`` would
    leave a fragment, which is a different (and worse) edit.
    """
    for match in re.finditer(r", (?:and|but) ([a-z][a-z']*)\b", text):
        word = match.group(1)
        yield (match.start(), match.end(), ". " + word[0].upper() + word[1:])


def _transition_finder(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
    """Prepend a connective to a non-initial sentence, downcasing its opener."""
    for match in re.finditer(r"(?<=[.!?] )([A-Z])([a-z]+)\b", text):
        if match.group(1) + match.group(2) not in SAFE_OPENERS:
            continue
        if re.search(r"\d\. $", text[max(0, match.start() - 4) : match.start()]):
            continue  # opens a numbered list item, not a following sentence
        opener = match.group(0)
        for transition in TRANSITIONS:
            yield (
                match.start(),
                match.end(),
                transition + opener[0].lower() + opener[1:],
            )


def _numeric_sentence_ends(text: str) -> Iterator[int]:
    """Index of the final period of every sentence carrying a number."""
    for start, end in all_sentences(text):
        body = text[start:end]
        if not re.search(r"\d", body):
            continue
        if not body.endswith("."):
            continue
        yield end - 1


def _citation_finder(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
    for at in _numeric_sentence_ends(text):
        for citation in CITATIONS:
            yield (at, at, citation)


def _caveat_finder(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
    for at in _numeric_sentence_ends(text):
        for caveat in CAVEATS:
            yield (at, at, caveat)


def _number_finder(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
    """Number/unit fixes, anchored so ``2 percent`` cannot match inside
    ``0.2 percent`` and produce a second, redundant candidate."""
    for old, new in NUMBER_FORMAT:
        for match in re.finditer(r"(?<![\d.$])" + anchored(old), text):
            yield (match.start(), match.end(), new)


def _terminology_finder(
    document: Document, text: str
) -> Iterator[tuple[int, int, str]]:
    for variant, house in document.term_pairs:
        for match in re.finditer(anchored(variant), text):
            yield (match.start(), match.end(), house)


def _rewrite_finder(document: Document, text: str) -> Iterator[tuple[int, int, str]]:
    for original, rewritten in document.rewrites:
        at = text.index(original)
        yield (at, at + len(original), rewritten)


def _cut_sentence_finder(
    document: Document, text: str
) -> Iterator[tuple[int, int, str]]:
    """Cut a whole sentence, taking the space that joins it to its neighbour."""
    for sentence in document.cuttable:
        at = text.index(sentence)
        start = at - 1 if at > 0 and text[at - 1] == " " else at
        yield (start, at + len(sentence), "")


KINDS: tuple[Kind, ...] = (
    Kind(
        "hedge_weaken",
        "replace",
        9.0,
        _phrase_finder(HEDGE_WEAKENING),
        "softens a claim (will -> may, shows -> suggests)",
    ),
    Kind(
        "hedge_strengthen",
        "replace",
        2.0,
        _phrase_finder(HEDGE_STRENGTHENING),
        "removes a hedge the author did not need",
    ),
    Kind(
        "cut_intensifier",
        "delete",
        9.0,
        _intensifier_finder,
        "cuts an intensifier (clearly, very, genuinely)",
    ),
    Kind(
        "tighten_wordy",
        "replace",
        10.0,
        _phrase_finder(WORDINESS),
        "tightens a wordy construction (in order to -> to)",
    ),
    Kind(
        "uk_us_spelling",
        "replace",
        9.0,
        _phrase_finder(BRITISH_TO_AMERICAN),
        "normalises British spelling to house style",
    ),
    Kind(
        "number_format",
        "replace",
        5.0,
        _number_finder,
        "fixes number and unit formatting",
    ),
    Kind(
        "jargon_replace",
        "replace",
        5.0,
        _phrase_finder(JARGON),
        "replaces jargon with plain wording",
    ),
    Kind(
        "terminology",
        "replace",
        4.0,
        _terminology_finder,
        "makes a term consistent with the rest of the piece",
    ),
    Kind(
        "passive_to_active",
        "replace",
        4.0,
        _phrase_finder(PASSIVE_TO_ACTIVE),
        "turns a passive construction active",
    ),
    Kind(
        "cut_throat_clearing",
        "replace",
        4.0,
        _phrase_finder(THROAT_CLEARING),
        "cuts throat-clearing at the start of a sentence",
    ),
    Kind(
        "split_run_on",
        "replace",
        4.0,
        _run_on_finder,
        "splits a run-on sentence in two",
    ),
    Kind(
        "word_choice",
        "replace",
        8.0,
        _word_choice_finder,
        "swaps a word for a plainer one",
    ),
    Kind(
        "cut_optional_that",
        "delete",
        6.0,
        _optional_that_finder,
        "strikes the optional 'that' after a reporting verb",
    ),
    Kind(
        "that_which",
        "replace",
        5.0,
        _that_which_finder,
        "turns a restrictive 'which' into 'that'",
    ),
    Kind("cut_filler", "delete", 5.0, _filler_finder, "strikes a filler phrase"),
    Kind(
        "insert_citation",
        "insert",
        5.0,
        _citation_finder,
        "adds a citation to a numeric claim",
    ),
    Kind(
        "insert_caveat",
        "insert",
        4.0,
        _caveat_finder,
        "adds a caveat to a numeric claim",
    ),
    Kind(
        "insert_hedge",
        "insert",
        5.0,
        _hedge_insert_finder,
        "hedges a claim in place (is -> is probably)",
    ),
    Kind(
        "add_transition",
        "insert",
        3.0,
        _transition_finder,
        "adds a connective between two sentences",
    ),
    Kind("cut_sentence", "delete", 2.0, _cut_sentence_finder, "cuts a whole sentence"),
    Kind("rewrite_claim", "replace", 2.0, _rewrite_finder, "rewrites a whole claim"),
)

KIND_BY_NAME = {k.name: k for k in KINDS}


def _heading_spans(document: Document) -> list[tuple[int, int]]:
    """Heading lines, plus the title and standfirst, which are off limits.

    Editing a heading is a real thing reviewers do; excluding it here keeps
    section attribution unambiguous, which is what the task predicates are
    built on.
    """
    text = document.text
    spans: list[tuple[int, int]] = []
    for heading in document.headings:
        at = text.find(heading + "\n")
        spans.append((at, at + len(heading)))
    first_break = text.find("\n")
    second_break = text.find("\n", first_break + 1)
    spans.append((0, second_break if second_break > 0 else first_break))
    return spans


def candidates(document: Document) -> list[Candidate]:
    """Every editorial edit this document admits, deduplicated and located.

    Deduplication is by ``(op, start, end, text)``: several kinds can find
    the same fix, and the sampler must not be able to draw it twice.
    """
    text = document.text
    headings = _heading_spans(document)
    sentence_spans = all_sentences(text)
    sections = document.section_ranges()

    def enclosing_sentence(index: int) -> tuple[int, int]:
        for span in sentence_spans:
            if span[0] <= index < span[1]:
                return span
        return (index, index)

    def section_of(index: int) -> str:
        for heading, (start, end) in sections.items():
            if start <= index < end:
                return heading
        return ""

    seen: set[tuple[str, int, int, str]] = set()
    out: list[Candidate] = []
    for kind in KINDS:
        for start, end, replacement in kind.finder(document, text):
            if end < start or (kind.op != "insert" and end == start):
                continue
            if any(hs < end and start < he for hs, he in headings):
                continue
            key = (kind.op, start, end, replacement)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Candidate(
                    kind=kind.name,
                    op=kind.op,
                    start=start,
                    end=end,
                    text=replacement,
                    section=section_of(start),
                    sentence=enclosing_sentence(start),
                )
            )
    out.sort(key=lambda c: (c.start, c.end, c.kind, c.text))
    return out
