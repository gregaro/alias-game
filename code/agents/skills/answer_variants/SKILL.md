# Answer Variants Skill

## Purpose
A live Armenian word-guessing game scores chat messages by EXACT match against
a list of accepted answers. A spelling that is not on the list scores zero for
a viewer who knew the word. Your job is to list the spellings real viewers
will actually type, so none of them is lost.

You are given the target words, each with its hints (so you know which sense
of the word is meant) and the answers already accepted. Propose ONLY new ones.

## What viewers type
- **Latin transliteration** of the Armenian, typed on a phone: `varsavir`,
  and the usual alternates (`kh`/`x`, `ts`/`c`, `j`/`zh`, `gh`/`r`).
- **The English name**, if the word has an everyday one: `barber`, `grapes`.
- **Russian in Cyrillic**, if Armenians commonly say it that way: `парикмахер`.
- **Colloquial / dialect / loanword forms** of the same thing.
- **Definite or plural forms** (`վարսավիրը`, `վարսավիրներ`) — chat often
  types the word with the article.
- **A true synonym** that the hints point at equally well.
- For a person: surname alone, the first name alone if that is how people
  say it, a well-known nickname, the Latin and Cyrillic spelling.

## What never goes in
- A word that merely RELATES to the answer (a category, a part, a rival).
  `ծառ` is not an answer for «տոնածառ». A wrong answer scored as right is
  worse than a missed one.
- Anything already in the accepted list, or a one-letter typo of it — typos
  of long answers are forgiven by the scorer already.
- A shorter or longer word that is a different word in its own right: `Խաղ`
  (game) is not an answer for «Խաղող» (grapes), and a stem cut out of the
  answer is not a variant.
- Padding. Five good variants beat fifteen guesses. If a word has no real
  variants beyond what is listed, return an empty list for it.

## Output format
Respond ONLY with valid JSON, no markdown, no commentary:

{"words": [{"word": "<exactly as given>", "variants": ["...", "..."]}]}

One entry per word, in the order given, `word` copied verbatim.
