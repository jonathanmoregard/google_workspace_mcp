"""Base documents for the stress corpus -- real prose, written by hand.

The stress corpus exists to measure a *review* workload, so the documents
under review have to look like the ones the client actually run their web
pages out of: 1,500-4,000 words of research-backed career and policy
writing, with headings, numbered claims, bulleted lists, inline citations,
links, and the occasional table rendered as text.

Nothing here is generated. No lorem ipsum, no Markov chains, no shuffled
sentences: a benchmark whose base text is noise measures an agent's ability
to review noise, and the failure modes of reviewing noise are not the
failure modes of reviewing prose. Each document is deliberately written as a
*draft that has not been copyedited yet* -- British and American spellings
mixed, hedges left in, a couple of terms used two ways, wordy constructions
intact, numbers formatted inconsistently. That is what gives the edit
sampler (:mod:`.edits`) real triggers to find, and it is what a real
reviewer's suggestions actually attach to.

Each :class:`Document` also carries the material a generic scanner cannot
invent: the section headings, the term pairs this document is inconsistent
about, and a set of whole-sentence rewrites of the kind a subject expert
makes. Those rewrites are hand-written per document for the same reason the
base text is: a machine-composed "rewrite" reads like a machine composed it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mockdocs.graphemes import split_graphemes


@dataclass(frozen=True)
class Document:
    """One base document plus the per-document material the sampler needs."""

    key: str
    title: str
    #: Exact heading lines, in document order. Used to derive section ranges.
    headings: tuple[str, ...]
    text: str
    #: ``(inconsistent_variant, house_style)`` -- terminology the draft uses
    #: two ways. A copyeditor normalises these.
    term_pairs: tuple[tuple[str, str], ...] = ()
    #: ``(sentence_as_written, expert_rewrite)`` -- whole-claim rewrites of
    #: the kind a subject expert makes. The left side must appear verbatim.
    rewrites: tuple[tuple[str, str], ...] = ()
    #: Sentences a manager would cut wholesale (verbatim, incl. trailing space
    #: handling done by the sampler).
    cuttable: tuple[str, ...] = field(default_factory=tuple)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def section_of(self, index: int) -> str:
        """Heading that governs grapheme ``index`` in the base text."""
        current = ""
        for heading, (start, _end) in self.section_ranges().items():
            if start <= index:
                current = heading
            else:
                break
        return current

    def section_ranges(self) -> dict[str, tuple[int, int]]:
        """``heading -> [start, end)`` over the base text, in order.

        A section runs from the first character of its heading line to the
        first character of the next heading line, which is how a reviewer
        reads it: the heading belongs to the section it introduces.
        """
        starts: list[tuple[str, int]] = []
        for heading in self.headings:
            needle = heading + "\n"
            at = self.text.find(needle)
            if at < 0:
                raise ValueError(f"{self.key}: heading {heading!r} is not in the text")
            starts.append((heading, at))
        starts.sort(key=lambda pair: pair[1])
        ranges: dict[str, tuple[int, int]] = {}
        for i, (heading, at) in enumerate(starts):
            end = starts[i + 1][1] if i + 1 < len(starts) else len(self.text)
            ranges[heading] = (at, end)
        return ranges

    def validate(self) -> None:
        """Cheap structural checks; a broken document must fail at import."""
        if not self.text.endswith("\n"):
            raise ValueError(f"{self.key}: text must end with a newline")
        if len(split_graphemes(self.text)) != len(self.text):
            raise ValueError(
                f"{self.key}: base text must be one grapheme per code point so "
                f"regex offsets are model offsets (no emoji/combining marks)"
            )
        if not 1500 <= self.word_count <= 4000:
            raise ValueError(f"{self.key}: {self.word_count} words, want 1500-4000")
        self.section_ranges()
        for original, _rewrite in self.rewrites:
            if self.text.count(original) != 1:
                raise ValueError(
                    f"{self.key}: rewrite target {original[:40]!r} appears "
                    f"{self.text.count(original)} times, want exactly 1"
                )
        for sentence in self.cuttable:
            if self.text.count(sentence) != 1:
                raise ValueError(
                    f"{self.key}: cuttable {sentence[:40]!r} appears "
                    f"{self.text.count(sentence)} times, want exactly 1"
                )
        for variant, house in self.term_pairs:
            if variant not in self.text:
                raise ValueError(f"{self.key}: term variant {variant!r} not present")
            if house == variant:
                raise ValueError(f"{self.key}: term pair {variant!r} is a no-op")


# ---------------------------------------------------------------------------
# 1. Career review
# ---------------------------------------------------------------------------

COMPUTE_GOVERNANCE = Document(
    key="compute-governance",
    title="Career review: compute governance",
    headings=(
        "In a nutshell",
        "Why we think this problem is pressing",
        "What the work actually involves",
        "Who is a good fit",
        "How to enter the field",
        "What we might be getting wrong",
        "Our take",
    ),
    term_pairs=(
        ("hardware governance", "compute governance"),
        ("cutting-edge model", "frontier model"),
    ),
    rewrites=(
        (
            "The compute used in the largest training runs has grown by roughly 4-5x per year for over a decade.",
            "Estimates of the growth rate in training compute cluster around 4x per year since 2010, though the series is noisy and the last two years are consistent with a slowdown.",
        ),
        (
            "Concentration of this kind is rare, and it is what makes compute governable at all.",
            "Concentration at every layer of the stack is what makes compute governable at all, and it is also the thing most likely to erode as other suppliers mature.",
        ),
        (
            "They have clearly slowed some actors down.",
            "The best public estimates suggest the controls delayed some actors by one to two years, at the cost of accelerating domestic substitution.",
        ),
        (
            "We do not think compute governance solves the alignment problem.",
            "Compute governance does not solve the alignment problem, and treating it as though it does is the most common mistake we see newcomers make.",
        ),
        (
            "Salaries in this space vary enormously.",
            "Compensation ranges from roughly $70,000 in a small nonprofit to well above $300,000 at a frontier lab, and the highest-paying roles are rarely the highest-impact ones.",
        ),
        (
            "That is good news for governance and bad news for competition.",
            "Fewer frontier actors makes monitoring easier and makes market concentration worse, and reasonable people weight those two effects differently.",
        ),
    ),
    cuttable=(
        "We have written about this at greater length elsewhere.",
        "This is a fast-moving area and some of the details below will date quickly.",
        "None of this is investment advice.",
    ),
    text="""Career review: compute governance
Last updated March 2025. Reading time: about 14 minutes.
In a nutshell
Compute governance is the practice of treating the physical supply chain behind advanced AI systems as a lever for policy. Training a frontier model still requires tens of thousands of specialised chips, those chips are made by a handful of firms in a handful of countries, and the resulting bottleneck is one of the very few inputs to AI development that a government can actually observe. We think this makes it an unusually tractable place to work on reducing risks from advanced AI, and we expect demand for people who understand both the technology and the policy to grow substantially over the next decade.
Recommended if you have a background in semiconductor engineering, export control law, economics, or national security policy, and you are comfortable working in organisations where the right answer is contested and the evidence is thin.
It is worth noting that this path is much less established than the adjacent careers in AI research or in traditional arms control, so early entrants are still writing the job descriptions.
Why we think this problem is pressing
1. The hardware supply chain is extremely concentrated. A single Dutch firm produces every extreme ultraviolet lithography machine in the world. One Taiwanese foundry fabricates the vast majority of leading-edge logic. Three firms design almost all of the accelerators used to train frontier models. Concentration of this kind is rare, and it is what makes compute governable at all.
2. Compute is measurable in a way that algorithms are not. You can count chips, meter the power draw of a data centre, and audit a cloud provider's customer records. You cannot count ideas. Policy that attaches to compute therefore has a verification story, and verification is the part where most arms control regimes get stuck.
3. Training runs keep getting larger. The compute used in the largest training runs has grown by roughly 4-5x per year for over a decade. If that trend continues, the capital requirements for frontier development will keep rising and the number of actors able to reach the frontier will stay small. That is good news for governance and bad news for competition.
4. The controls that exist are already leaky. The export controls introduced in October 2022, and tightened repeatedly since, are the largest natural experiment we have. They have clearly slowed some actors down. They have also produced smuggling networks, chips redesigned to sit just below the thresholds, and a great deal of diplomatic friction with allies who host parts of the supply chain.
We do not think compute governance solves the alignment problem. It buys time, it creates enforcement points, and it makes agreements verifiable. Those things are worth a lot, but they are instrumental, and it is important to remember that a well-monitored race is still a race.
What the work actually involves
Technical analysis. Estimating the compute required for a given capability, tracking performance per dollar across chip generations, and modelling how quickly a restricted actor could substitute older hardware for newer.
Policy design. Writing the actual text of a threshold, deciding whether it should be denominated in FLOP, in chip count, or in interconnect bandwidth, and thinking carefully about how each choice will be gamed.
Verification and enforcement. Designing on-chip mechanisms, data centre inspection regimes, and know-your-customer requirements for cloud providers, all of which have to survive contact with commercial reality.
Diplomacy. Persuading allied governments to align their control lists, because a unilateral control on a globally traded good mostly relocates the trade rather than stopping it.
Industry engagement. Working with firms who reasonably object to being turned into instruments of foreign policy, and who know more about their own supply chains than any regulator does.
A typical week at a policy think tank might be two days of research, one day of writing, and two days of meetings with officials, journalists and company staff. In government the ratio shifts sharply towards meetings, and the writing that survives is much shorter. At a frontier lab the work is closer to product engineering than to policy, because the mechanisms have to ship.
The roles themselves sit in five broad places. Think tanks and academic centres, which offer the most intellectual freedom and the least direct influence. Government, where the influence is real but arrives slowly and is heavily mediated by whoever holds the relevant office. Frontier laboratories, where the compensation is high and the independence is limited. Chip firms and cloud providers, where you will spend a lot of time explaining to colleagues why a rule that costs them money is nonetheless reasonable. And a small number of nonprofits doing technical standards work, which is unglamorous and possibly the highest-leverage option of the lot.
We would encourage people to think about these as a sequence rather than a choice. Almost everyone we would point to as unusually effective in this area has worked in at least two of the five, and the combination of an industry stint with a policy role appears to be particularly valuable, because it is very hard to write an implementable rule about a process you have never seen operate.
Who is a good fit
People who do well here tend to share a few traits. They are comfortable being the only person in the room who understands both the silicon and the statute. They can write a two-page memo that a busy official will actually read. They tolerate working on questions where the honest answer is that nobody knows, and they do not need public credit, because the best outcomes in this field are invisible.
You do not need a PhD. Roughly half the people we know doing this work well came from law, economics or journalism and learned the technical material on the job. You do need enough numeracy to sanity-check a FLOP estimate, and enough patience to read a 200-page rulemaking document without skimming.
It is probably a bad fit if you want fast feedback loops, if you find political ambiguity draining, or if you would be unhappy spending several years building credibility before anyone asks your opinion. It is also a bad fit if you need to be certain that your work is helping, because a plausible reading of the last three years is that the controls have accelerated exactly the domestic capability they were meant to slow.
One trait we have started to weight more heavily is a tolerance for being disliked by people you respect. Compute governance sits between a research community that values openness and a security community that values control, and anyone doing the work seriously will at some point be told by thoughtful colleagues that they are on the wrong side. People who need consensus to feel comfortable tend to leave within two years.
How to enter the field
1. Build a public track record. Write two or three short analyses of a live policy question and publish them. This is the single highest-return thing an outsider can do, and it is how a large fraction of current practitioners were first noticed.
2. Get one deep technical or legal competence. Semiconductor process technology, export control law, cloud infrastructure, or the economics of general purpose technologies. Breadth is easy to acquire later, and depth is what gets you hired.
3. Take an entry role adjacent to the field. Congressional staff, a national laboratory, a chip firm's policy team, or a research assistant position at a think tank. Each of these gives you either the technical exposure or the institutional access, and you can pick up the other.
4. Move towards the bottleneck. Once inside, the highest-leverage roles are the ones nobody wants: the interagency coordination job, the standards committee, the team that has to write the implementing guidance. This is a fast-moving area and some of the details below will date quickly.
5. Learn the institutions, not just the topic. A great deal of what determines whether a proposal goes anywhere is procedural: which committee has jurisdiction, which agency writes the guidance, which trade association will be consulted, and how long each of those steps takes. This knowledge is boring, it is not taught anywhere, and it is a large part of what separates people who publish from people who change things.
Salaries in this space vary enormously. A research analyst at a mid-sized think tank might start near the bottom of that range, while a policy lead at a large laboratory sits near the top. None of this is investment advice.
What we might be getting wrong
We may be overrating tractability. Export controls are a blunt instrument, and the historical record for technology controls on dual-use goods is mixed at best. It could be that the entire approach buys eighteen months and a worse diplomatic environment.
We may be underrating the substitution problem. Algorithmic efficiency has been improving quickly, and if the compute needed for a given capability keeps falling, then hardware governance becomes a control on a moving target.
We may be wrong about who matters. Most of our attention has gone to training compute, but inference is now a large and growing share of total spending, and the governance story for inference is much weaker. We have written about this at greater length elsewhere.
Finally, we are aware that our sources skew towards a small community in Washington, London and the Bay Area, which is exactly the sort of thing that produces confident consensus on a question that is genuinely open.
Our take
We also want to flag a disagreement inside our own team. One view is that the value of this work comes almost entirely from the possibility of a future international agreement, and that everything happening now is preparation for a negotiation that may never occur. The other view is that the day-to-day work of writing better thresholds and closing enforcement gaps is valuable on its own terms, regardless of whether any grand bargain materialises. We have not resolved this, and where you land on it should probably affect which of the five settings above you choose.
We rate compute governance as one of the more promising paths for people who want to reduce risks from advanced AI and who are not going to become machine learning researchers. The field is small, the demand is real, and the skills transfer well if the strategic picture changes. We would encourage more people with hard technical backgrounds to consider it, particularly people who already understand semiconductors, since that expertise is genuinely scarce and cannot be picked up quickly.
If you are considering this path, our advising team would like to hear from you, and we can usually introduce promising candidates to people already working on cutting-edge model policy.
""",
)


# ---------------------------------------------------------------------------
# 2. Research summary
# ---------------------------------------------------------------------------

FORECASTING_EVIDENCE = Document(
    key="forecasting-evidence",
    title="Does forecasting research actually improve decisions?",
    headings=(
        "Summary",
        "The claim we are evaluating",
        "What the tournament evidence shows",
        "What happens inside organisations",
        "A rough quantitative model",
        "Limitations of this evidence",
        "What would change our mind",
        "References",
    ),
    term_pairs=(
        ("superforecaster", "expert forecaster"),
        ("calibration training", "probability training"),
    ),
    rewrites=(
        (
            "Aggregating and extremising a group of trained forecasters beat the intelligence community's classified baseline by a wide margin.",
            "The often-quoted margin over the classified baseline comes from a single programme with a small number of question sets, and the comparison was never designed as a controlled trial.",
        ),
        (
            "This is one of the most robust findings in the judgement literature.",
            "The finding replicates across several tournaments, though almost all of them share a question-selection process that favours resolvable geopolitical events.",
        ),
        (
            "We estimate that a well-run internal forecasting programme costs about 0.2 percent of the budget it informs.",
            "Our cost estimate of roughly 0.2% of the informed budget is built from four organisations that agreed to share figures, and the spread between them was more than threefold.",
        ),
        (
            "Forecasting tournaments select for a particular kind of question.",
            "Tournament questions are chosen because they resolve cleanly within a year, which systematically excludes the slow, structural questions that most institutional decisions actually turn on.",
        ),
        (
            "The effect sizes reported in the literature are large.",
            "Reported effect sizes are large, but the studies that report them are mostly small, unblinded, and run by researchers with a stake in the result.",
        ),
        (
            "We think the case for funding more of this work is strong.",
            "On balance we think the case for funding more of this work is moderately strong, and weaker than the enthusiasm of its advocates suggests.",
        ),
    ),
    cuttable=(
        "The full dataset is available on request.",
        "We are grateful to several reviewers for comments on an earlier draft.",
        "A longer version of this section appeared in our 2023 annual review.",
    ),
    text="""Does forecasting research actually improve decisions?
A summary of the evidence, written for people deciding whether to fund or work on it.
Summary
The evidence that structured forecasting improves the accuracy of predictions is strong. The evidence that it improves the quality of decisions is much weaker, and the gap between those two claims is where most of the disagreement in this field lives. We reviewed 34 studies, 4 large tournaments, and interviewed 11 practitioners inside government and philanthropy. We conclude that the accuracy findings are real and replicated, that the decision-quality findings rest on a handful of case studies, and that the main bottleneck is institutional rather than methodological.
This piece is aimed at people choosing between research agendas, not at forecasters looking to improve their scores.
The claim we are evaluating
The strong version of the claim goes something like this: organisations make consequential decisions under uncertainty, they currently do so using unquantified expert judgement, that judgement is poorly calibrated, and replacing it with aggregated probabilistic forecasts would clearly produce better outcomes. Each of the four steps is doing real work, and it is important to remember that the last one is the only one anybody actually cares about.
We separate the claim into three testable pieces:
1. Trained forecasters are more accurate than untrained ones on resolvable questions.
2. Aggregation methods improve accuracy beyond what any individual achieves.
3. Organisations that adopt these methods make measurably better decisions.
The first two are well supported. The third is not, and in our view it is the one worth working on.
What the tournament evidence shows
The forecasting tournaments run between 2011 and 2015 remain the cleanest evidence available. Aggregating and extremising a group of trained forecasters beat the intelligence community's classified baseline by a wide margin. Individual accuracy also improved with a short course in probability training, typically an hour of material, which is a remarkably cheap intervention for the effect reported.
This is one of the most robust findings in the judgement literature. It has been replicated in at least three subsequent tournaments, including two run outside the United States and one restricted to epidemiological questions during 2020.
A few details are worth stating precisely, because they are frequently misquoted:
The improvement from calibration training is roughly 10 percent in Brier score terms, not 10 percentage points of accuracy.
The superforecaster effect is partly selection and partly training, and the published decompositions disagree about the split.
Team effects are large and are usually reported as if they were individual effects.
The effect sizes reported in the literature are large. Forecasting tournaments select for a particular kind of question. This matters more than it might appear, and we return to it below.
There is a further result that gets less attention and that we think is more useful to funders. Accuracy improvements from aggregation are largely independent of accuracy improvements from training, which means the two interventions stack. An organisation that does neither can get most of the available benefit by doing the cheaper one first, and the cheaper one is almost always aggregation, because it requires no behaviour change from the people being aggregated.
The counterpoint, raised by several of the practitioners we interviewed, is that aggregation without training produces confident-looking numbers that nobody in the organisation understands well enough to argue with. Whether that is better or worse than unquantified judgement is genuinely unclear to us, and we could not find a study that addresses it.
What happens inside organisations
This is where the evidence thins out dramatically. We identified nine organisations that have run an internal forecasting programme for at least two years: three government agencies, four philanthropic funders, and two firms. Of those, only two have attempted any evaluation of whether the forecasts changed a decision, and neither evaluation was blinded.
The practitioners we interviewed were consistent about the failure modes. Forecasts arrive after the decision has effectively been made. The questions that get asked are the ones that are easy to resolve rather than the ones that are load-bearing. Senior staff treat a probability as a hedge rather than as information. A number of respondents said that the main value they had observed was cultural: teams that forecast argue more precisely, even when nobody looks at the numbers afterwards.
That cultural claim is plausible and almost entirely unevidenced. A longer version of this section appeared in our 2023 annual review.
The two organisations that did attempt an evaluation are worth describing, because their designs illustrate how hard this is. The first compared decisions made in quarters when the forecasting team was active against quarters when it was not, and found no detectable difference, which the authors attribute to the questions being unrelated to the decisions actually taken. The second surveyed decision-makers about whether a forecast had changed their view, and found that 60 percent said yes, which tells us about recall and self-image rather than about decisions.
We want to be careful not to overclaim in the other direction. Absence of evidence here is genuinely weak evidence of absence, because the studies that would detect an effect have mostly not been run, and running them requires an organisation willing to randomise something it cares about. Very few organisations are willing to do that, and the ones that are tend to be unusual in ways that limit what you can generalise from them.
A rough quantitative model
To make the comparison concrete we built a simple model of the value of an internal programme. The inputs are deliberately crude and we encourage readers to substitute their own.
Annual cost of a programme, 5 staff, fully loaded: $750,000
Decisions informed per year: 40
Budget governed by those decisions: $400M
Improvement in expected value per decision, central estimate: 1.5 percent
Implied annual value created: $6M
We estimate that a well-run internal forecasting programme costs about 0.2 percent of the budget it informs. The central estimate for value created is therefore roughly eight times the cost, but the confidence interval spans zero, because the improvement-per-decision term is not measured anywhere. We are effectively assuming the conclusion.
The honest summary of this model is that it tells you what you would need to believe, not what is true. We include it because several funders asked for a number, and a transparent bad number is more useful than a private one.
Limitations of this evidence
Publication bias is a serious concern here. The field is small, the researchers are often the same people who run the tournaments, and negative results about a method you developed are hard to publish and harder to fund.
Question selection is the deeper problem. Tournament questions resolve within twelve months, have unambiguous resolution criteria, and concern events that a well-read generalist can reason about. The decisions that matter most to the organisations we studied have none of those properties. Whether a research programme will pay off over fifteen years is not a question a tournament can score.
Finally, almost all of this work measures accuracy against outcomes rather than decision quality against counterfactuals. A perfectly calibrated forecast attached to a decision nobody revisits creates no value at all.
A fourth limitation is that we have very little evidence about persistence. Nearly every programme we looked at was under five years old, and the two that are older have both been through a period of reduced activity. Whether an internal forecasting function survives a change of leadership, a budget cut, or a high-profile miss is an empirical question with essentially no data behind it, and it bears directly on how a funder should discount future value.
We should also note our own position. Two of the authors have previously received funding from an organisation that runs forecasting tournaments, and one has served as a paid question writer. We do not think this has changed our conclusions, which is exactly what someone in our position would say, so we mention it and let readers apply their own discount.
What would change our mind
We would substantially raise our estimate if we saw a preregistered trial in which comparable teams made comparable decisions with and without forecast input, and the forecast-informed teams did measurably better on an outcome specified in advance.
We would substantially lower it if a serious replication effort found that the tournament effects shrink under stricter question selection, or if the two organisations currently attempting internal evaluations report null results.
We would also update on cheaper evidence than that. If someone published a careful account of a decision that was demonstrably changed by a forecast, with the counterfactual documented at the time rather than reconstructed afterwards, we would treat it as a meaningful data point even though it is a single case. The reason is that the theory currently has no worked example at all, and one worked example moves you a surprising distance when the prior is that far from zero.
Conversely, we would become considerably more pessimistic if the pattern reported by our interviewees turns out to be universal: that questions are selected for resolvability rather than importance, and that this is not a start-up problem but a stable equilibrium, because the people choosing questions are rewarded for a clean scoreboard rather than a useful one.
We think the case for funding more of this work is strong. The full dataset is available on request.
References
Tetlock and Gardner, Superforecasting, 2015. The popular treatment; the underlying papers are more careful than the book.
Mellers et al., Psychological Science, 2014. The main tournament result, including the training effect.
Karvetski et al., Decision Analysis, 2021. Aggregation methods compared on a common dataset.
Our own interview notes, anonymised, 2024. We are grateful to several reviewers for comments on an earlier draft.
""",
)


# ---------------------------------------------------------------------------
# 3. Policy brief
# ---------------------------------------------------------------------------

SYNTHESIS_SCREENING = Document(
    key="synthesis-screening",
    title="Policy brief: screening requirements for synthetic DNA",
    headings=(
        "Summary",
        "Background",
        "The gap in current practice",
        "Recommendations",
        "Costs and objections",
        "Implementation timeline",
        "Annex: definitions",
    ),
    term_pairs=(
        ("gene synthesis provider", "synthesis provider"),
        ("sequences of concern", "hazardous sequences"),
    ),
    rewrites=(
        (
            "Screening is currently voluntary and coverage is incomplete.",
            "Screening is voluntary, and the best available survey suggests that providers representing around 80% of global commercial volume screen orders, which leaves a long tail that does not.",
        ),
        (
            "A determined actor can order fragments below the screening threshold and assemble them.",
            "Fragment-level ordering defeats length-based screening entirely, and the assembly step it requires is now within reach of a competent graduate student.",
        ),
        (
            "Benchtop synthesisers change the picture substantially.",
            "Benchtop synthesisers move the control point from the provider to the device, which is a different regulatory problem and needs a different instrument.",
        ),
        (
            "This has been demonstrated in the open literature more than once, and no serious participant in the debate disputes it.",
            "Fragment assembly has been demonstrated in the open literature at least three times since 2018, and we are not aware of any serious participant in the debate who disputes that it works.",
        ),
        (
            "The compliance cost per order is small.",
            "Providers report compliance costs of roughly $2 to $8 per order at current volumes, which is small relative to order value but not relative to margin on the cheapest products.",
        ),
        (
            "Enforcement is the weakest part of this proposal.",
            "Enforcement is the weakest part of this proposal, and any version of it that lacks an audit mechanism will be complied with on paper and ignored in practice.",
        ),
    ),
    cuttable=(
        "The authors have no financial interest in any company named in this brief.",
        "An earlier draft of this brief was circulated for comment in November.",
        "Footnotes have been kept deliberately sparse.",
    ),
    text="""Policy brief: screening requirements for synthetic DNA
Prepared for a policy audience. Approximately 8 pages in the printed version.
Summary
Commercial providers of synthetic DNA can, at modest cost, screen incoming orders against a list of sequences associated with dangerous pathogens and toxins, and can screen the customers placing those orders. Screening is currently voluntary and coverage is incomplete. We recommend that screening be made a condition of federal funding, that a public reference database of hazardous sequences be maintained by a designated agency, and that benchtop synthesis devices be brought within the same regime within three years. The compliance cost per order is small. Enforcement is the weakest part of this proposal.
Background
Synthetic DNA is now a routine laboratory input. A researcher specifies a sequence, a provider manufactures it, and the physical material arrives in the post within days. Prices have fallen by more than two orders of magnitude since 2005, and the market has grown accordingly. This has been enormously beneficial for biomedical research, and we want to be clear at the outset that the policy question is how to preserve that benefit while closing a narrow and specific gap.
The gap is that the same capability that lets a laboratory order a gene lets someone order the genome of a dangerous pathogen. Several agents of serious concern have published sequences and are small enough to assemble from commercially available fragments. The barrier is no longer access to the sequence; it is the tacit knowledge required to turn a sequence into a functioning organism, and that barrier has been eroding.
An international consortium of providers has operated a voluntary screening protocol since 2009. Participating firms check orders against a database of sequences of concern, verify the identity and institutional affiliation of customers, and refer anomalies to law enforcement. By the consortium's own account this has caught a small number of genuinely concerning orders. The authors have no financial interest in any company named in this brief.
The gap in current practice
Three gaps matter.
First, participation is voluntary and self-reported. There is no audit, no penalty for non-participation, and no reliable public figure for what share of global volume is screened. Providers who screen bear a cost that providers who do not screen avoid, which is exactly the wrong incentive gradient.
Second, the screening threshold is length-based. Most protocols screen orders above 200 base pairs. A determined actor can order fragments below the screening threshold and assemble them. This has been demonstrated in the open literature more than once, and no serious participant in the debate disputes it.
Third, the regime does not cover devices. Benchtop synthesisers change the picture substantially. A device sitting in a private laboratory, synthesising sequences to order with no external party in the loop, is outside the reach of any provider-side control. Manufacturers have begun to build screening into the firmware, which is encouraging, and entirely voluntary.
There is a fourth gap that is harder to write policy about. Screening compares an ordered sequence against a list, and a list is a description of the past. Sequences that are functionally equivalent to a listed agent but differ enough to evade a similarity threshold are not a hypothetical problem, and the tools for generating them have improved quickly. Any screening standard written today needs a mechanism for updating its matching criteria, not only its list, and none of the current voluntary protocols has one that we would call adequate.
We flag this without a recommendation attached, because the honest position is that the technical community has not agreed on what a robust matching criterion looks like, and legislating a specific one now would freeze the weakest available answer into statute.
Recommendations
1. Make screening a funding condition. Any research funded by a federal agency should be required to procure synthetic nucleic acids only from providers that attest to a recognised screening standard. This uses an existing lever, requires no new legislation, and would cover a large fraction of legitimate demand within one budget cycle.
2. Designate a custodian for the reference database. The list of hazardous sequences needs an owner with the authority to update it, the technical capacity to curate it, and a clear process for handling the genuinely difficult question of what to publish. We suggest this sit with a civilian agency, not an intelligence one, because provider cooperation depends on it not looking like surveillance.
3. Fund the screening infrastructure directly. Screening algorithms, the curated database, and the customer verification service are public goods, and asking each provider to build them separately is both wasteful and a barrier to entry for small firms.
4. Extend the regime to benchtop devices within three years. New devices should ship with screening enabled by default and should require a signed update to disable it. Retrofitting the installed base is harder and will need a separate instrument.
5. Build an audit function. Attestation without verification is a paperwork exercise. A modest programme of test orders, run by a designated body against participating providers, would establish whether the controls actually operate.
6. Protect the people who report. Screening only works if the staff who notice an anomalous order are willing to escalate it, and at present a provider employee who raises a concern about a paying customer has no protection at all. A narrow whistleblower provision covering synthesis orders would cost nothing and would meaningfully change the incentives inside firms.
The first three recommendations are, in our judgement, achievable within the current legal framework and the current political appetite. Recommendation 4 will require either new authority or a voluntary commitment from a small number of manufacturers, and recommendation 5 will require money that nobody has yet allocated. We list them together because a regime with attestation and no audit is worse than no regime at all, in the specific sense that it creates a false assurance that displaces other controls.
Costs and objections
The most common objection is that screening will not stop a sophisticated state actor. This is correct and not to the point. The regime is aimed at raising the floor, and the floor is where the marginal risk sits, because the number of actors with modest resources is very large and growing.
A second objection is commercial. Providers argue, with some justification, that a patchwork of national requirements would impose real costs and would push orders towards jurisdictions with weaker rules. This is a genuine problem and it is the reason recommendation 2 emphasises international alignment.
A third objection concerns research friction. Any screening system produces false positives, and a researcher whose legitimate order is delayed by a week has been taxed by the policy. Current false positive rates are reported at under 2 percent, and most flagged orders clear within one working day. We think this is an acceptable cost, though we would want it monitored rather than assumed.
Finally, there is a genuine information hazard question about the reference database itself. A public list of hazardous sequences is a shopping list. The consortium's current approach, in which the list is shared with verified providers rather than published, seems right to us, and it does make independent evaluation of screening quality much harder.
A related objection, raised most often by academic researchers, is that a funding condition effectively deputises universities as enforcement agents for a security policy they had no part in setting. We take this seriously. The mitigation we prefer is to place the obligation on procurement rather than on individual investigators, so that compliance is handled by an administrative office rather than by a graduate student ordering a plasmid at eleven at night.
There is also a question of who bears the cost of false negatives. Under the current arrangement, a provider who screens and misses something faces reputational and possibly legal exposure, while a provider who does not screen at all faces neither. Any regime that raises the standard of care without also clarifying liability will push cautious firms out of the market and leave the incautious ones in it, which is the opposite of what anybody wants.
Implementation timeline
Months 0 to 6: designate the custodian agency, publish the draft standard, and open a comment period. An earlier draft of this brief was circulated for comment in November.
Months 6 to 18: funding condition takes effect for new grants. The audit programme begins with volunteer participants.
Months 18 to 36: device requirements enter into force for newly manufactured benchtop synthesisers. International negotiation on mutual recognition concludes or is abandoned.
Annex: definitions
Sequence of concern: a nucleic acid sequence whose possession or synthesis is judged to present a meaningful risk of misuse, as listed in the reference database.
Screening: the comparison of an ordered sequence against the reference database, together with verification of the customer's identity and institutional affiliation. Both halves are required; a provider that checks sequences but not customers is not screening in the sense used here.
Benchtop synthesiser: a device capable of synthesising nucleic acids on site, without an order being placed with an external gene synthesis provider. Footnotes have been kept deliberately sparse.
""",
)


# ---------------------------------------------------------------------------
# 4. FAQ
# ---------------------------------------------------------------------------

ADVICE_FAQ = Document(
    key="advice-faq",
    title="Frequently asked questions about our career advice",
    headings=(
        "Do I have to work on the problems you rank highest?",
        "Is it too late to change career?",
        "What if I am not technical?",
        "Should I earn to give?",
        "How much does personal fit matter?",
        "What about jobs that are good but not on your list?",
        "How confident are you in any of this?",
        "How do I get in touch?",
    ),
    term_pairs=(
        ("high-impact career", "impactful career"),
        ("career capital", "transferable skills"),
    ),
    rewrites=(
        (
            "Personal fit is the single biggest factor in how much good you do.",
            "Personal fit dominates within a problem area and matters much less across problem areas, and conflating those two claims leads people to justify staying where they are.",
        ),
        (
            "Most people who change career successfully do it in two steps rather than one.",
            "In our advising data, people who switched successfully almost always moved through an intermediate role that shared either the skill or the sector with their target, rather than changing both at once.",
        ),
        (
            "Earning to give is a good fit for a minority of people.",
            "Earning to give makes sense when your expected earnings are unusually high, when the roles you would otherwise take are well supplied with talent, and when you are confident you will keep giving.",
        ),
        (
            "We revise our rankings roughly once a year.",
            "We revise the problem rankings roughly once a year, and the ordering within the top five has changed twice in the last four years, which should tell you something about how much weight to put on it.",
        ),
        (
            "Our advice is aimed at a narrow audience and we know it.",
            "Our advice is calibrated for people with a lot of career flexibility, and if that does not describe you then large parts of it will be actively misleading.",
        ),
        (
            "The list is a starting point for your own thinking.",
            "Treat the list as a prompt for your own analysis rather than a ranking to be obeyed, because we are working from public information and you know your own situation.",
        ),
    ),
    cuttable=(
        "We update this page periodically.",
        "Several of these answers are adapted from our podcast.",
        "You can skip this section if you have read our key ideas series.",
    ),
    text="""Frequently asked questions about our career advice
These are the questions our advisors get most often from readers planning a high-impact career. We update this page periodically.
Do I have to work on the problems you rank highest?
No, and we would gently push back on the framing. Our problem rankings are an attempt to answer a general question: if you knew nothing about a person, which problems would you point them at? You know a great deal about yourself, and that information should usually dominate.
The ranking is most useful as a way of noticing options you had not considered. It is least useful as a tiebreaker between two roles you are already excited about, because at that point the differences in personal fit are almost certainly larger than the differences in problem score. The list is a starting point for your own thinking.
There is also a supply consideration that we do not emphasise enough. If a problem is highly ranked and widely publicised, it may already be attracting more people than it can absorb, and the marginal value of one more person falls accordingly. We try to account for this, and we do not always succeed.
Is it too late to change career?
Almost certainly not, though the honest answer depends on what you are changing to and how much runway you have.
Most people who change career successfully do it in two steps rather than one. Someone moving from corporate law to policy work rarely lands in a policy role directly; they take a role that uses the legal training in a new sector, then move again. Each step is small enough to be credible to the person hiring.
The main constraints we see are financial rather than reputational. People with dependants, visa requirements, or debt have much less room to take a pay cut or an entry-level role, and advice that ignores this is not advice, it is a description of a lifestyle. If that is your situation, the useful question is which moves preserve your options rather than which move is optimal.
Age itself matters less than people expect. We have advised people who made substantial transitions in their fifties. What changes with age is not capability but the cost of being a beginner again, and that cost is mostly social.
What if I am not technical?
A great deal of the most valuable work is not technical, and we think our own materials have historically overcorrected towards research roles.
Operations, communications, management, grantmaking, and policy all sit on the critical path for most of the problems we care about, and all of them are chronically undersupplied. Organisations working on these problems routinely tell us that their binding constraint is a good operations lead, not another researcher.
There is a real caveat. Some of these roles require you to understand the technical work well enough to evaluate it, even if you never do it yourself. A grantmaker in AI safety who cannot read a paper is at a serious disadvantage. The requirement is comprehension, not production, and that is a much lower bar than people assume.
The other thing we would say to people who describe themselves as non-technical is that the label is often doing more work than it deserves. Plenty of people who say it mean that they did not study a technical subject at university, which is a fact about their past rather than a constraint on their future. If you can read a technical argument carefully and notice when it does not follow, you have the part that is hard to teach.
Should I earn to give?
Earning to give is a good fit for a minority of people. It was more central to our advice ten years ago than it is now, for two reasons: the organisations we care about became less funding-constrained and more talent-constrained, and the range of directly impactful roles widened considerably.
It still makes sense in several situations. If you have unusually high earning potential, if your alternative roles are ones where you would be readily replaced, if you have obligations that make a low salary impossible, or if you want to build career capital in a lucrative field while you decide, then it can be an excellent choice.
The failure mode we see most often is people who plan to earn to give, succeed at the earning, and quietly stop giving. Making the commitment public and automatic is the standard remedy and it works better than willpower.
A second failure mode is subtler and we have only recently started warning people about it. Someone takes a lucrative role intending to give, finds that the role also builds career capital, and then discovers five years later that the skills they built are not the ones any of the organisations they admire actually need. Earning to give is a plan about money, and it is easy to let it quietly become a plan about a career without ever deciding to.
How much does personal fit matter?
Personal fit is the single biggest factor in how much good you do. The distribution of outcomes within almost any career path is wide, and the difference between being excellent at something and being adequate at it is usually larger than the difference between two adjacent paths.
Testing fit is cheaper than most people think. A short project, a summer internship, a volunteer stint, or a serious conversation with three people already doing the job will tell you more than a year of deliberation. Several of these answers are adapted from our podcast.
We would add one caution. Enjoyment and fit are correlated but not identical. Enjoying the day-to-day is a strong signal, and so is being unusually good at the thing relative to your peers, and those two signals sometimes point in different directions.
People also underrate how much fit depends on the specific team rather than the abstract job. Two research roles with identical descriptions at two organisations can differ enormously in how much autonomy you get, how quickly you receive feedback, and whether your manager has time for you. When people tell us a path did not work for them, the reasons they give are usually about a particular workplace, not about the work.
If you are early in your career and genuinely cannot tell, the practical advice is to optimise for learning rate over the first few years. A role where you are visibly improving every month builds career capital that transfers, and it also gives you much better information about what you are suited to than any amount of reflection will.
What about jobs that are good but not on your list?
Take them, if they are the best option available to you.
An impactful career does not have to be a legible one, and our list is built from public information about a few dozen paths, so it necessarily misses most of what exists. There are thousands of roles that contribute meaningfully to solving important problems and that we have never written about, either because the path is idiosyncratic or because nobody on our team happened to look at it.
The test we would apply is not whether a role appears on our list. It is whether the work addresses a problem that is large, neglected and tractable, whether you will be good at it, and whether it builds transferable skills for the next step. A role that passes those three tests and is absent from our list is a role we have not got to yet.
How confident are you in any of this?
Less than the tone of our writing sometimes suggests, and we are trying to fix that.
We revise our rankings roughly once a year. Our track record on specific predictions is mixed. We were early on some things and slow on others, and several positions we held confidently five years ago now look wrong to us.
Our advice is aimed at a narrow audience and we know it. It assumes a degree of flexibility, mobility and financial cushion that most people in the world do not have. Within that audience we think the advice is good. Outside it, please treat it as a set of considerations rather than a plan.
The single thing we would most like readers to take away is a habit rather than a conclusion: ask what problem you are contributing to, ask how much difference your particular contribution makes, and revisit both answers as the world changes. You can skip this section if you have read our key ideas series.
How do I get in touch?
Our one-on-one advising is free and is aimed at people who are seriously considering a change in the next year or two. We prioritise applications where we think a conversation would actually change what someone does, which in practice means people with a specific decision in front of them.
If advising is not a fit, our job board, our podcast, and our newsletter are all free, and none of them require an application. We read replies to the newsletter and we do respond, though it can take a few weeks.
""",
)


DOCUMENTS: tuple[Document, ...] = (
    COMPUTE_GOVERNANCE,
    FORECASTING_EVIDENCE,
    SYNTHESIS_SCREENING,
    ADVICE_FAQ,
)

BY_KEY: dict[str, Document] = {d.key: d for d in DOCUMENTS}

for _document in DOCUMENTS:
    _document.validate()
