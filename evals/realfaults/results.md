| Bug | Function(s) | Fault | Faulty lines executed at P | Score at P / gate | Survivor on faulty line | Fix is a survivor | Score at F / gate | Coverage-only pipeline | Full pipeline |
|---|---|---|---|---|---|---|---|---|---|
| more-itertools-bucket-falsey-validator | | | | not run in the time budget: a mutmut run takes about 8 minut... | | | | | |
| more-itertools-one-only-falsy-exception | | | | not run in the time budget: a mutmut run takes about 8 minut... | | | | | |
| more-itertools-seekable-peek-maxlen0 | | | | not run in the time budget: a mutmut run takes about 8 minut... | | | | | |
| boltons-pearson-zero-denominator | Stats._calc_pearson_type | omission | NO (50.0%) | 38.96% BLOCK (47 surv.) | yes | no | 64.71% BLOCK | flags | flags |
| boltons-indexedset-update | IndexedSet.update | modified | NO (0.0%) | 60% BLOCK (4 surv.) | yes | no | 80% PASS | flags | flags |
| boltons-complementset-issuperset-typo | _ComplementSet.__le__, _ComplementSet.__ge__ | modified | NO (0.0%) | 0% BLOCK (22 surv.) | yes | no | 36.36% BLOCK | flags | flags |
| boltons-bits-length | Bits.__init__ | modified | yes (100.0%) | 70.77% BLOCK (19 surv.) | yes | yes | 73.85% BLOCK | misses | flags |
| toolz-interpose-empty | interpose | modified | yes (100.0%) | 100% PASS (0 surv.) | no | no | 100% PASS | misses | misses |
| toolz-partition-all-bad-len | partition_all | modified | yes (100.0%) | 92.31% PASS (2 surv.) | yes | no | 86.11% PASS | misses | misses |
| tabulate-asciidoc-trailing-whitespace | _asciidoc_row | modified | yes (100.0%) | 80.88% PASS (13 surv.) | yes | no | 80% PASS | misses | misses |
| tabulate-wrap-wide-char-hang | _CustomTextWrap._handle_long_word | omission | yes (100.0%) | 88.89% PASS (6 surv.) | no | no | 91.67% PASS | misses | misses |
| tabulate-empty-table-maxheadercolwidths | | | | not run in the time budget: the buggy function is the 300-li... | | | | | |
| semver-next-version-legacy-counter | Version.next_version | modified | yes (100.0%) | 91.57% PASS (7 surv.) | no | no | 90.7% PASS | misses | misses |
| semver-next-version-token-switch | Version.next_version | modified | yes (100.0%) | 92.5% PASS (6 surv.) | no | no | 91.57% PASS | misses | misses |

- bugs evaluated: 10 (tool errors: 0)
- coverage-only pipeline (diff coverage) flags the faulty code: 3/10 (30%)
- full pipeline (diff coverage + mutation) flags it: 4/10 (40%)
- faulty lines all executed at P (coverage blind spot): 7/10 (70%)
- diff-coverage gate passes the faulty lines: 7/10 (70%)
- mutation gate blocks at P: 4/10 (40%)
- ... without docstring mutants: 4/10 (40%)
- a surviving mutant on a faulty line at P: 6/10 (60%)
- ... excluding docstring mutants: 6/10 (60%)
- the fix itself is a surviving mutant at P: 1/10 (10%)
- score higher at F than at P: 5/10 (50%)
- score equal at F and P: 1/10 (10%)
- score lower at F than at P: 4/10 (40%)
- mutation gate blocks at F: 3/10 (30%)
- coverage blind AND mutation gate blocks at P: 1/10 (10%)
- coverage blind AND survivor on a faulty line: 3/10 (30%)
- coverage blind AND mutation gate passes at P: 6/10 (60%)
- median score: 84.9 at P, 83.1 at F
